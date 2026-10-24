"""Hook latency, measured the way Claude Code runs the hooks: a new python3 process for each event,
with the payload on stdin.

    python3 tools/bench.py [--runs N] [--transcript-mb MB] [--check]

Everything runs in a temporary folder: CONTEXTRAIL_HOME, a git repository and a transcript of about
MB megabytes. The real ~/.contextrail is never touched, and no model is called. --check exits 1 when
a hook's p95 is over its budget. The first row is the interpreter starting and exiting, which every
hook pays before ContextRail's own work begins."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
CR = ROOT / "contextrail" / "cr.py"
GIT = ["git", "-c", "user.name=Bench", "-c", "user.email=bench@example.com", "-c", "commit.gpgsign=false"]
# p95 budgets in milliseconds, including the interpreter's start.
ORDINARY_MS = 150
SESSION_START_MS = 500
BODY = """## Goal
Measure the hooks.

## User's rules
- None stated

## Done
- `pytest -q` passed in `tests/test_app.py`.

## In progress
- None

## Next steps
1. Run `python3 tools/bench.py` in `tools/`.

## Decisions
- None

## Dead ends
- None

## Known facts
- None

## Hypotheses
- None

## How to run
- `python3 tools/bench.py`

## Open questions
- None

## Verify first
- `git status`

## Resume prompt
Run the benchmark again."""


def percentile(values: List[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)]


def assistant(tokens: int, content: Optional[list] = None) -> dict:
    return {
        "type": "assistant", "isSidechain": False, "version": "2.1.310", "timestamp": "2026-10-24T08:00:00Z",
        "message": {
            "role": "assistant", "model": "claude-sonnet-5-5",
            "content": content or [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 2, "cache_creation_input_tokens": 1_000, "cache_read_input_tokens": tokens - 1_002},
        },
    }


def write_transcript(path: Path, megabytes: float, tokens: int) -> None:
    """A transcript with the shape of a real one: tool calls, their results, replies."""
    padding = "x" * 4_000
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "start"}}) + "\n")
        f.write(json.dumps(assistant(20_000)) + "\n")
        n = 0
        while f.tell() < megabytes * 1_000_000:
            n += 1
            command = "pytest -q" if n % 5 == 0 else f"cat src/file{n % 50}.py"
            f.write(json.dumps(assistant(20_000 + n, [{"type": "tool_use", "id": f"t{n}", "name": "Bash", "input": {"command": command}}])) + "\n")
            result = {"type": "tool_result", "tool_use_id": f"t{n}", "content": f"{padding}\n3 passed", "is_error": False}
            f.write(json.dumps({"type": "user", "isSidechain": False, "message": {"role": "user", "content": [result]}}) + "\n")
            if n % 7 == 0:
                edit = {"type": "tool_use", "id": f"e{n}", "name": "Edit", "input": {"file_path": f"src/file{n % 50}.py"}}
                f.write(json.dumps(assistant(20_000 + n, [edit])) + "\n")
        f.write(json.dumps(assistant(tokens)) + "\n")


def make_repo(path: Path) -> None:
    (path / "src").mkdir(parents=True)
    for n in range(50):
        (path / "src" / f"file{n}.py").write_text(f"VALUE = {n}\n" * 40)
    subprocess.run([*GIT, "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run([*GIT, "add", "."], cwd=path, check=True)
    subprocess.run([*GIT, "commit", "-q", "-m", "initial"], cwd=path, check=True)
    for n in range(5):
        (path / "src" / f"file{n}.py").write_text("VALUE = 'changed'\n")


class Bench:
    def __init__(self, tmp: Path, megabytes: float):
        self.repo = tmp / "repo"
        self.home = tmp / "home"
        make_repo(self.repo)
        self.quiet = tmp / "quiet.jsonl"  # below the handoff point
        self.full = tmp / "full.jsonl"  # past it
        write_transcript(self.quiet, megabytes, 50_000)
        write_transcript(self.full, megabytes, 120_000)
        self.env = dict(
            os.environ, CONTEXTRAIL_HOME=str(self.home), CONTEXTRAIL_COPY_RESUME_PROMPT="0",
            CLAUDE_CONFIG_DIR=str(tmp / "claude"), CLAUDE_CODE_SESSION_ATTENDED="1",
        )
        self.env.pop("CONTEXTRAIL_DISABLE", None)
        self.run_number = 0

    def payload(self, event: str, transcript: Path, **extra: object) -> dict:
        return {"session_id": "bench", "transcript_path": str(transcript), "cwd": str(self.repo), "hook_event_name": event, **extra}

    def hook(self, command: str, payload: dict) -> float:
        started = time.perf_counter()
        result = subprocess.run([sys.executable, str(CR), command], input=json.dumps(payload).encode(),
                                env=self.env, capture_output=True, timeout=30)
        took = (time.perf_counter() - started) * 1000
        if result.returncode != 0:
            raise SystemExit(f"{command} exited {result.returncode}: {result.stderr.decode()[-500:]}")
        return took

    def reset_state(self) -> None:
        for path in (self.home / "projects").glob("*/state.json"):
            path.unlink()

    def cases(self) -> List[Tuple[str, int, Callable[[], float]]]:
        def interpreter() -> float:
            started = time.perf_counter()
            subprocess.run([sys.executable, "-c", "import json, sys"], env=self.env, check=True)
            return (time.perf_counter() - started) * 1000

        def asks() -> float:
            self.reset_state()  # each run is a session reaching the handoff point
            return self.hook("stop", self.payload("Stop", self.full, last_assistant_message="ok"))

        def saves() -> float:
            self.run_number += 1  # a different handoff each time: the same event twice saves once
            body = BODY.replace("Measure the hooks.", f"Measure the hooks, run {self.run_number}.")
            message = f"<contextrail-handoff>\n{body}\n</contextrail-handoff>"
            return self.hook("stop", self.payload("Stop", self.full, last_assistant_message=message))

        def starts() -> float:
            self.run_number += 1  # a new session each time: a repeated start loads nothing
            return self.hook("session-start", self.payload("SessionStart", self.quiet, source="startup", session_id=f"s{self.run_number}"))

        return [
            ("python3 start-up (baseline)", 0, interpreter),
            ("Stop, below the handoff point", ORDINARY_MS, lambda: self.hook("stop", self.payload("Stop", self.quiet, last_assistant_message="ok"))),
            ("PostToolUse, every tool call", ORDINARY_MS, lambda: self.hook("post-tool", self.payload("PostToolUse", self.quiet))),
            ("Stop, asks for a handoff", ORDINARY_MS, asks),
            ("Stop, saves a handoff", SESSION_START_MS, saves),
            ("SessionStart, loads the handoff", SESSION_START_MS, starts),
            ("StopFailure", ORDINARY_MS, lambda: self.hook("stop-failure", self.payload("StopFailure", self.quiet, error="rate_limit"))),
            ("PreCompact, saves the facts", SESSION_START_MS, lambda: self.hook("pre-compact", self.payload("PreCompact", self.full))),
        ]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Measure ContextRail's hook latency.")
    parser.add_argument("--runs", type=int, default=50)
    parser.add_argument("--transcript-mb", type=float, default=5.0)
    parser.add_argument("--check", action="store_true", help="exit 1 when a p95 is over its budget")
    args = parser.parse_args(argv)
    tmp = Path(tempfile.mkdtemp(prefix="contextrail-bench-"))
    try:
        bench = Bench(tmp, args.transcript_mb)
        print(f"Python {sys.version.split()[0]} at {sys.executable}, {args.runs} runs, transcript {args.transcript_mb:g} MB")
        print(f"{'':34} {'p50':>7} {'p95':>7} {'max':>7}  budget (p95)")
        over: Dict[str, float] = {}
        for name, budget, case in bench.cases():
            case()  # warm the file cache
            times = [case() for _ in range(args.runs)]
            p95 = percentile(times, 0.95)
            verdict = f"{budget} ms {'ok' if p95 <= budget else 'OVER'}" if budget else ""
            print(f"{name:34} {percentile(times, 0.5):6.0f}ms {p95:6.0f}ms {max(times):6.0f}ms  {verdict}")
            if budget and p95 > budget:
                over[name] = p95
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if over and args.check:
        print("Over budget: " + ", ".join(f"{name} ({p95:.0f} ms)" for name, p95 in over.items()))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
