"""ContextRail in real Claude Code: one `claude -p` session is asked for a handoff and saves it, and
the next session in the same folder starts with it loaded.

    python3 tools/integration.py [--claude PATH] [--model haiku] [--keep]

This calls the model: two short sessions, three or four requests in all (Haiku by default), so it
needs Claude Code to be logged in or ANTHROPIC_API_KEY to be set, and it uses a little quota. The
sessions run without tools or MCP servers, in a new git repository in a temporary folder, with
ContextRail from this checkout (--plugin-dir) and its data in that folder too. Claude Code's
transcripts of the two sessions are deleted afterwards unless --keep is given.

Checks: a handoff request is made and reaches the agent as hook feedback on Claude Code 2.1.163 and
later (a blocked stop before), the handoff is saved with its facts, and the next session's agent can
answer from it. Exits 0 when every check passes."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from contextrail import config, store  # noqa: E402
from contextrail.handlers import FEEDBACK_SINCE  # noqa: E402
from contextrail.usage import claude_version  # noqa: E402

GIT = ["git", "-c", "user.name=Integration", "-c", "user.email=integration@example.com", "-c", "commit.gpgsign=false"]
REQUEST_START = "ContextRail: this session is at"
# Leaves out the other copy of ContextRail's companion, if it is installed: it would /clear after a handoff.
SETTINGS = json.dumps({"enabledPlugins": {"contextrail-continue@contextrail": False}})


class Checks:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, ok: bool, what: str, detail: str = "") -> bool:
        print(f"  {'PASS' if ok else 'FAIL'}  {what}" + (f": {detail}" if detail and not ok else ""))
        self.failed += 0 if ok else 1
        return ok


def run_claude(claude: str, model: str, prompt: str, cwd: Path, env: Dict[str, str]) -> Tuple[Dict[str, Any], str]:
    argv = [
        claude, "-p", prompt, "--model", model, "--output-format", "json", "--plugin-dir", str(ROOT),
        "--settings", SETTINGS, "--strict-mcp-config", "--tools", "",
    ]
    result = subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=600)
    try:
        output = json.loads(result.stdout)
    except ValueError:
        output = {}
    return (output if isinstance(output, dict) else {}), result.stderr


def transcript_entries(path: Path) -> List[Dict[str, Any]]:
    entries = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def how_requested(entries: List[Dict[str, Any]]) -> List[str]:
    """How each of ContextRail's requests reached the agent: the attachment type Claude Code stored it
    as. hook_success, the record of the hook's raw output, is left out."""
    found = []
    for entry in entries:
        attachment = entry.get("attachment")
        if entry.get("type") != "attachment" or not isinstance(attachment, dict) or attachment.get("type") == "hook_success":
            continue
        if REQUEST_START in json.dumps(attachment):
            found.append(str(attachment.get("type")))
    return found


def hook_errors(entries: List[Dict[str, Any]]) -> List[str]:
    """What Claude Code reported as Stop hook errors, from its stop_hook_summary entries."""
    errors = []
    for entry in entries:
        if entry.get("type") == "system" and entry.get("subtype") == "stop_hook_summary":
            listed = entry.get("hookErrors")
            errors += [str(error) for error in listed] if isinstance(listed, list) else []
    return errors


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Check ContextRail in real Claude Code (calls the model).")
    parser.add_argument("--claude", default="claude")
    parser.add_argument("--model", default="haiku")
    parser.add_argument("--keep", action="store_true", help="keep the temporary folder and the transcripts")
    args = parser.parse_args(argv)

    tmp = Path(tempfile.mkdtemp(prefix="contextrail-integration-")).resolve()
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run([*GIT, "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "notes.txt").write_text("integration check\n")
    subprocess.run([*GIT, "add", "."], cwd=repo, check=True)
    subprocess.run([*GIT, "commit", "-q", "-m", "initial"], cwd=repo, check=True)
    word = f"PELICAN-{secrets.randbelow(9000) + 1000}"
    home = tmp / "contextrail-home"
    base = {key: value for key, value in os.environ.items() if not key.startswith("CONTEXTRAIL_")}
    base.update(
        CONTEXTRAIL_HOME=str(home), CONTEXTRAIL_UNATTENDED="1", CONTEXTRAIL_COPY_RESUME_PROMPT="0",
        # A claude started inside a Claude Code session saves no transcript without this.
        CLAUDE_CODE_FORCE_SESSION_PERSISTENCE="1", CLAUDE_CODE_DISABLE_AUTO_MEMORY="1",
    )
    # Ask at the first stop: 1% and 2% of the window are below what any session starts with.
    first_env = dict(base, CONTEXTRAIL_SOFT_PCT="1", CONTEXTRAIL_HARD_PCT="2")
    # Load the handoff, but ask for nothing.
    second_env = dict(base, CONTEXTRAIL_SOFT_TOKENS="800000", CONTEXTRAIL_HARD_TOKENS="900000",
                      CONTEXTRAIL_SOFT_PCT="99", CONTEXTRAIL_HARD_PCT="100")
    transcripts = config.claude_dir() / "projects" / store.claude_slug(repo)
    checks = Checks()
    print(f"ContextRail integration check in {tmp}")
    try:
        print(f"Session 1 ({args.model}): a handoff is requested and saved")
        out, errors = run_claude(args.claude, args.model, f"Remember this code word for later: {word}. Reply with one short sentence.", repo, first_env)
        session = out.get("session_id")
        if not checks.check(isinstance(session, str), "the session ran", errors[-500:] or json.dumps(out)[:500]):
            return 1
        path = transcripts / f"{session}.jsonl"
        version = claude_version(str(path))
        print(f"  Claude Code {'.'.join(map(str, version)) if version else 'version unknown'}")
        entries = transcript_entries(path) if path.exists() else []
        styles = how_requested(entries)
        checks.check(bool(styles), "ContextRail asked for a handoff", f"no request in {path}")
        feedback = version is not None and version >= FEEDBACK_SINCE
        expected = "hook_additional_context" if feedback else "hook_blocking_error"
        checks.check(bool(styles) and set(styles) == {expected}, f"the request reached the agent as {expected}", f"got {styles}")
        if feedback:
            errors = hook_errors(entries)
            checks.check(not errors, "Claude Code reported no Stop hook error", f"got {errors}")
        project = store.Project(repo, base=home)
        document, meta, problems = store.read_handoff(project)
        checks.check(document is not None and not problems, "the handoff was saved", f"{problems} in {project.dir}")
        checks.check(document is not None and word in document, "it holds the code word")
        checks.check(meta.get("session_id") == session and meta.get("head") is not None, "its meta records the session and the commit", json.dumps(meta)[:300])

        print(f"Session 2 ({args.model}): the handoff is loaded")
        out, errors = run_claude(
            args.claude, args.model,
            "A handoff from the previous session was loaded at the start. What code word does it mention? Answer with the word only.",
            repo, second_env,
        )
        answer = str(out.get("result") or "")
        checks.check(re.search(re.escape(word), answer) is not None, f"the next session answered {word}", f"answered {answer[:200]!r}")
        second = out.get("session_id")
        if isinstance(second, str) and (transcripts / f"{second}.jsonl").exists():
            again = how_requested(transcript_entries(transcripts / f"{second}.jsonl"))
            checks.check(not again, "no request in the second session", f"got {again}")
    finally:
        if args.keep:
            print(f"Kept {tmp} and {transcripts}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
            if transcripts.parent == config.claude_dir() / "projects" and transcripts.name == store.claude_slug(repo):
                shutil.rmtree(transcripts, ignore_errors=True)
    print("All checks passed." if not checks.failed else f"{checks.failed} check(s) failed.")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    sys.exit(main())
