"""The model calls of an evaluation run: regenerate a case's handoff under a cap, list
what session B needed, and judge two handoffs against those needs. Each call is one
`claude -p` that saves no transcript, and each writes one JSON file in the case's folder."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from contextrail import handoff, prompts

from . import cases
from .cases import Case

# The variant for the handoff A wrote and saved; the others are named by their cap.
SAVED = "saved"
VERDICTS = ("covered", "partial", "missing", "wrong")
KINDS = ("used", "rechecked", "reexplained", "mistake")
MAX_NEEDS = 25
# How much of B the needs call reads, keeping the start.
CONDENSED_CHARS = 120_000
MESSAGE_CHARS = 2_000
INPUT_CHARS = 300
RESULT_CHARS = 400
TIMEOUT = 1_800
CLAUDE = ["claude", "-p", "--output-format", "json", "--no-session-persistence", "--strict-mcp-config"]
FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)

NEEDS_PROMPT = """You are helping evaluate handoff documents. Session A wrote a handoff when its context filled up, and session B started from it. Below are the handoff as B loaded it, and B's transcript up to B's own handoff, condensed: tool inputs and results are cut.

List what B needed from the previous session to do its work, at most {max_needs} items, the most important first. Each item is one of these kinds:
- used: information from the handoff that B relied on.
- rechecked: state B worked out again by reading files or running commands, which a handoff could have told it.
- reexplained: something the user had to tell B again, or correct.
- mistake: a wrong step B took that a handoff could have prevented.

Leave out what nobody could have known when A stopped, such as the results of B's own new work. Write each need as the information itself, specific enough to check a handoff against (paths, names, numbers, the user's words), not as where B found it.

Reply with JSON only, no other text:
{{"needs": [{{"id": 1, "kind": "used", "need": "...", "evidence": "where in B this shows, in a few words"}}]}}

<loaded-handoff>
{loaded}
</loaded-handoff>

<session-b>
{condensed}
</session-b>"""

JUDGE_PROMPT = """You are judging two handoff documents. Both were written at the same point of the same coding session, for the next session to continue from. The next session also gets the recorded facts below, the same with either handoff.

For each need in the list, judge each handoff, read together with the facts:
- covered: it gives the information, specific enough to act on.
- partial: it gives part of it, or only vaguely.
- missing: it doesn't give it.
- wrong: it says something that contradicts it.
Then say which handoff would serve the next session better: "1", "2" or "tie". Judge the content, not the length.

Reply with JSON only, no other text:
{{"verdicts": [{{"id": 1, "h1": "covered", "h2": "missing"}}], "better": "1"}}

<facts>
{facts}
</facts>

<handoff-1>
{first}
</handoff-1>

<handoff-2>
{second}
</handoff-2>

<needs>
{needs}
</needs>"""

# (argv, stdin, cwd, env) -> what the command printed.
Runner = Callable[[List[str], str, Path, Dict[str, str]], str]


class EvalError(Exception):
    pass


def run_process(argv: List[str], stdin: str, cwd: Path, env: Dict[str, str]) -> str:
    try:
        done = subprocess.run(argv, input=stdin, cwd=cwd, env=env, capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise EvalError(f"{' '.join(argv[:2])} did not finish: {error}") from error
    if not done.stdout.strip():
        raise EvalError(f"{argv[0]} exited {done.returncode} without output: {done.stderr.strip()[-500:]}")
    return done.stdout


@dataclass
class Harness:
    root: Path  # cases, results and the sandbox
    judge_model: str = "opus"
    runner: Runner = run_process

    @property
    def sandbox(self) -> Path:
        """CONTEXTRAIL_HOME for the calls, so they never touch the real one."""
        return self.root / "sandbox"

    @property
    def work(self) -> Path:
        """A neutral working folder for the needs and judge calls."""
        return self.sandbox / "work"

    def case_dir(self, case: Case) -> Path:
        return self.root / "cases" / case.key

    def env(self) -> Dict[str, str]:
        env = dict(os.environ)
        env.update(CONTEXTRAIL_DISABLE="1", CONTEXTRAIL_HOME=str(self.sandbox), CONTEXTRAIL_COPY_RESUME_PROMPT="0")
        # Today's auto-memory knows what came after A's handoff; the fork must not.
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        return env

    def claude(self, args: List[str], stdin: str, cwd: Path) -> Dict[str, Any]:
        """One `claude -p` call: its result message, or EvalError."""
        cwd.mkdir(parents=True, exist_ok=True)
        printed = self.runner(CLAUDE + args, stdin, cwd, self.env())
        try:
            data = json.loads(printed)
        except ValueError:
            raise EvalError(f"claude printed something other than JSON: {printed[:300]}") from None
        if isinstance(data, list):  # verbose output: every message, the result last
            data = next((item for item in reversed(data) if isinstance(item, dict) and item.get("type") == "result"), None)
        if not isinstance(data, dict) or data.get("is_error") or not isinstance(data.get("result"), str):
            detail = data.get("result") or data.get("subtype") if isinstance(data, dict) else data
            raise EvalError(f"claude failed: {str(detail)[:500]}")
        return data


def _meta(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "duration_ms": data.get("duration_ms"),
        "cost_usd": data.get("total_cost_usd"),
        "turns": data.get("num_turns"),
        "usage": data.get("usage"),
    }


def write_json(path: Path, data: Dict[str, Any]) -> Dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
    return data


def _fail(path: Path, reply: str, message: str) -> EvalError:
    """Keep the reply that could not be used next to where its result would have gone."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_suffix(".raw.txt").write_text(reply, encoding="utf-8")
    return EvalError(f"{message} (reply kept in {path.with_suffix('.raw.txt')})")


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_json(reply: str) -> Any:
    """The JSON in a model's reply: the whole reply, a fenced block, or the outermost braces."""
    candidates = [reply.strip()] + [block.strip() for block in FENCE_RE.findall(reply)]
    start, end = reply.find("{"), reply.rfind("}")
    if 0 <= start < end:
        candidates.append(reply[start : end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    raise EvalError("no JSON in the reply")


def variants(caps: List[int]) -> List[str]:
    return [SAVED] + [str(cap) for cap in caps]


def gen_path(h: Harness, case: Case, cap: int) -> Path:
    return h.case_dir(case) / f"gen-{cap}.json"


def needs_path(h: Harness, case: Case) -> Path:
    return h.case_dir(case) / "needs.json"


def judge_path(h: Harness, case: Case, first: str, second: str) -> Path:
    return h.case_dir(case) / f"judge-{first}-{second}.json"


def body(h: Harness, case: Case, variant: str) -> str:
    if variant == SAVED:
        return case.saved_body
    return str(read_json(gen_path(h, case, int(variant)))["body"])


def generate(h: Harness, case: Case, cap: int) -> Dict[str, Any]:
    """A's handoff again, from the reply before its request, with the request at this cap."""
    if not Path(case.cwd).is_dir():
        raise EvalError(f"{case.key}: the session's folder {case.cwd} is gone")
    args = ["--resume", case.a, "--resume-session-at", case.fork_uuid, "--fork-session", "--model", case.model]
    if case.effort:
        args += ["--effort", case.effort]
    stdin = cases.FEEDBACK + prompts.request(case.usage_line, cap, case.refresh)
    data = h.claude(args, stdin, Path(case.cwd))
    path = gen_path(h, case, cap)
    text = handoff.extract(data["result"])
    if text is None:
        raise _fail(path, data["result"], f"{case.key}: no handoff block in the reply at cap {cap}")
    return write_json(path, {"cap": cap, "body": text, "chars": len(text), "reply": data["result"], **_meta(data)})


def _content(entry: cases.Entry) -> List[Dict[str, Any]]:
    content = (entry.get("message") or {}).get("content")
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def _cut(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + f" [… {len(text) - limit:,} more]"


def _result_text(block: Dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, list):
        return "\n".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return str(content or "")


def condense(path: Path, limit: int = CONDENSED_CHARS) -> str:
    """B's main thread up to its own handoff: messages, the agent's text, tool calls and
    results, each cut short, and the whole cut to `limit` characters from the start."""
    lines: List[str] = []
    for entry in cases.read(path):
        if cases.is_request(entry) or cases.has_handoff(entry):
            break
        if cases.is_message(entry):
            lines.append("USER: " + _cut(cases.text(entry), MESSAGE_CHARS))
        elif cases.is_prompt(entry):
            lines.append("EVENT: " + _cut(cases.text(entry), RESULT_CHARS))
        elif cases.main_line(entry, "user"):
            for block in _content(entry):
                if block.get("type") == "tool_result":
                    failed = " (error)" if block.get("is_error") else ""
                    lines.append(f"RESULT{failed}: " + _cut(_result_text(block), RESULT_CHARS))
        elif cases.main_line(entry, "assistant"):
            for block in _content(entry):
                if block.get("type") == "text" and str(block.get("text") or "").strip():
                    lines.append("AGENT: " + _cut(str(block["text"]), MESSAGE_CHARS))
                elif block.get("type") == "tool_use":
                    args = json.dumps(block.get("input"), ensure_ascii=False)
                    lines.append(f"TOOL {block.get('name')}: " + _cut(args, INPUT_CHARS))
    text = "\n".join(lines)
    if len(text) > limit:
        text = text[:limit] + f"\n[… the rest of the session, {len(text) - limit:,} characters, is cut]"
    return text


def _needs(reply: str) -> List[Dict[str, Any]]:
    data = parse_json(reply)
    items = data.get("needs") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise EvalError("the reply has no needs list")
    found = []
    for item in items[:MAX_NEEDS]:
        if not isinstance(item, dict) or not str(item.get("need") or "").strip():
            raise EvalError(f"a need without text: {item!r}"[:300])
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in KINDS:
            raise EvalError(f"a need of unknown kind {kind!r}")
        found.append({"id": len(found) + 1, "kind": kind, "need": str(item["need"]).strip(),
                      "evidence": str(item.get("evidence") or "").strip()})
    if not found:
        raise EvalError("the reply lists no needs")
    return found


def needs(h: Harness, case: Case) -> Dict[str, Any]:
    """What B needed from the previous session, from the handoff it loaded and its transcript."""
    condensed = condense(Path(case.b_path))
    stdin = NEEDS_PROMPT.format(max_needs=MAX_NEEDS, loaded=case.loaded.strip(), condensed=condensed)
    data = h.claude(["--tools", "", "--model", h.judge_model], stdin, h.work)
    path = needs_path(h, case)
    try:
        found = _needs(data["result"])
    except EvalError as error:
        raise _fail(path, data["result"], f"{case.key}: {error}") from None
    return write_json(path, {"needs": found, "condensed_chars": len(condensed), "model": h.judge_model, **_meta(data)})


def _verdicts(reply: str, ids: List[int]) -> Dict[str, Any]:
    data = parse_json(reply)
    if not isinstance(data, dict) or not isinstance(data.get("verdicts"), list):
        raise EvalError("the reply has no verdicts list")
    by_id: Dict[int, Dict[str, str]] = {}
    for item in data["verdicts"]:
        if not isinstance(item, dict):
            continue
        try:
            number = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        pair = {side: str(item.get(side) or "").strip().lower() for side in ("h1", "h2")}
        if all(value in VERDICTS for value in pair.values()):
            by_id[number] = pair
    lacking = [number for number in ids if number not in by_id]
    if lacking:
        raise EvalError(f"no valid verdicts for needs {lacking}")
    better = str(data.get("better") or "").strip().lower()
    if better not in ("1", "2", "tie"):
        raise EvalError(f"'better' is {better!r}, not 1, 2 or tie")
    return {"by_id": by_id, "better": better}


def judge(h: Harness, case: Case, first: str, second: str) -> Dict[str, Any]:
    """Each need judged in both handoffs, `first` shown as handoff 1."""
    listed = read_json(needs_path(h, case))["needs"]
    stdin = JUDGE_PROMPT.format(
        facts=cases.facts_part(case.loaded) or "None recorded.",
        first=body(h, case, first),
        second=body(h, case, second),
        needs="\n".join(f"{item['id']}. {item['need']}" for item in listed),
    )
    data = h.claude(["--tools", "", "--model", h.judge_model], stdin, h.work)
    path = judge_path(h, case, first, second)
    try:
        found = _verdicts(data["result"], [item["id"] for item in listed])
    except EvalError as error:
        raise _fail(path, data["result"], f"{case.key}, {first} vs {second}: {error}") from None
    names = {"1": first, "2": second, "tie": "tie"}
    verdicts = [
        {"id": item["id"], first: found["by_id"][item["id"]]["h1"], second: found["by_id"][item["id"]]["h2"]}
        for item in listed
    ]
    return write_json(path, {"first": first, "second": second, "verdicts": verdicts,
                         "better": names[found["better"]], "model": h.judge_model, **_meta(data)})


def claude_version(h: Harness) -> Optional[str]:
    try:
        return h.runner(["claude", "--version"], "", h.root, h.env()).strip() or None
    except EvalError:
        return None
