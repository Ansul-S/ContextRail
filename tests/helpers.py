"""Builders for synthetic transcripts, git repositories and hook payloads."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
CR = ROOT / "contextrail" / "cr.py"

GIT = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false"]

# Context at a test session's first reply, which the token limits count from. Small,
# so limits in tests read as plain token counts.
START = 2_000

VALID_BODY = """## Goal
Add rate limiting to the public API so one client cannot starve the rest.

## User's rules
- "Don't add new dependencies without asking me."
- "Ask me before you change anything clients can see in a response."
- "Keep the limit configurable, ops tune it per environment."

## Done
- `TokenBucket` in `api/limits.py`; `pytest -q tests/test_limits.py` passed (12 tests).

## In progress
- Wiring the limiter into `create_app()` in `api/app.py`: middleware registered, `RATE_LIMIT_PER_MIN` not read yet. Unverified: no test covers it.

## Next steps
1. Latest request, "wire the limiter in and make the limit configurable": read `RATE_LIMIT_PER_MIN` from `Settings` in `api/settings.py` and pass it to `TokenBucket` in `create_app()`.
2. Register the limiter after the auth middleware, so `test_requires_key` passes again.
3. Add an integration test for the 429 response in `tests/test_app.py`.

## Decisions
- Per-API-key buckets, not per-IP: the user's clients sit behind one NAT.

## Dead ends
- Redis-backed counter: too slow in tests; in-memory is enough for one instance.

## Known facts
- 3 routes need the limiter: `/v1/invoices`, `/v1/customers`, `/v1/payments`. `/healthz` stays unlimited.
- The spec's default is 600 requests per minute per key (ticket BILL-482).
- `pytest -q` fails only in `test_requires_key`: the limiter runs before auth, so a request without a key gets 429 instead of 401. Unconfirmed whether `/healthz` is affected too.

## How to run
- Tests: `pytest -q` from the repository root with `BILLING_ENV=test`; the default `dev` needs a running database.
- The pre-commit hook runs `ruff check`. Fix what it reports; don't use `--no-verify`.

## Open questions
- Should a 429 carry a `Retry-After` header? The spec doesn't say, and clients retry at once today. (a) Add it: clients can back off, but the response changes, which needs the user's approval. (b) Leave it out: nothing changes for clients. Recommendation: (a).

## Verify first
- `pytest -q` (expect only `test_requires_key` to fail)

## Resume prompt
Continue the API rate limiting from the handoff: read RATE_LIMIT_PER_MIN into create_app(), fix the limiter-before-auth order, then add the 429 test. Ask me about Retry-After first."""


def assistant(tokens: int, model: str = "claude-sonnet-5-5", sidechain: bool = False,
              content: Optional[List[Dict[str, Any]]] = None, message_id: Optional[str] = None) -> Dict[str, Any]:
    return {
        "type": "assistant",
        "isSidechain": sidechain,
        "message": {
            "id": message_id or f"msg_{uuid.uuid4().hex[:12]}",
            "role": "assistant",
            "model": model,
            "content": content if content is not None else [{"type": "text", "text": "ok"}],
            "usage": {
                "input_tokens": 2,
                "cache_creation_input_tokens": 1_000,
                "cache_read_input_tokens": max(0, tokens - 1_002),
                "output_tokens": 50,
            },
        },
    }


def user(text: str = "go on") -> Dict[str, Any]:
    return {"type": "user", "isSidechain": False, "message": {"role": "user", "content": text}}


def tool_use(tool_id: str, name: str, args: Dict[str, Any], tokens: int = 10_000) -> Dict[str, Any]:
    return assistant(tokens, content=[{"type": "tool_use", "id": tool_id, "name": name, "input": args}])


def tool_result(tool_id: str, is_error: bool = False, text: Any = None) -> Dict[str, Any]:
    """A tool result; text may be a string or a list of content blocks. Bash
    starts the text of a non-zero exit with "Exit code N"."""
    if text is None:
        text = "Exit code 1\nerror" if is_error else "output"
    return {
        "type": "user",
        "isSidechain": False,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": text, "is_error": is_error}],
        },
    }


def write_transcript(path: Path, entries: List[Dict[str, Any]]) -> str:
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return str(path)


def git(repo: Path, *args: str) -> str:
    return subprocess.run([*GIT, *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    (path / "app.py").write_text("print('hello')\n")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "initial commit")
    return path


class TempHome(unittest.TestCase):
    """Runs each test with its own CONTEXTRAIL_HOME, Claude config folder and a
    fresh git repo."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.home = self.tmp / "contextrail-home"
        self.claude = self.tmp / "claude-config"
        self.repo = make_repo(self.tmp / "repo")
        self.transcript = self.tmp / "session.jsonl"
        env = {
            "CONTEXTRAIL_HOME": str(self.home),
            "CONTEXTRAIL_COPY_RESUME_PROMPT": "0",
            "CLAUDE_CONFIG_DIR": str(self.claude),
        }
        self._env = mock.patch.dict(os.environ, env)  # restores the whole environment on stop
        self._env.start()
        for key in [k for k in os.environ if k.startswith("CONTEXTRAIL_") and k not in env]:
            del os.environ[key]
        os.environ.pop("CLAUDE_CODE_SESSION_ATTENDED", None)  # set when the tests run inside Claude Code

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def payload(self, event: str, **extra: Any) -> Dict[str, Any]:
        data = {
            "session_id": "session-1",
            "transcript_path": str(self.transcript),
            "cwd": str(self.repo),
            "hook_event_name": event,
        }
        data.update(extra)
        return data

    def at_tokens(self, tokens: int, model: str = "claude-sonnet-5-5", start: int = START) -> None:
        write_transcript(self.transcript, [user("start"), assistant(start, model=model), assistant(tokens, model=model)])
