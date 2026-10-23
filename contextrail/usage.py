"""Current context usage, read from the end of a Claude Code transcript.

Claude Code counts context as input + cache_creation + cache_read tokens of
the latest main-thread assistant message. Hook payloads do not carry the
window size, so it comes from config, from what the optional status-line
wrapper recorded, or is inferred.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, List, Optional, Tuple

from . import config
from .store import read_json, slug

DEFAULT_WINDOW = 200_000
LARGE_WINDOW = 1_000_000
CHUNK = 256 * 1024
MAX_SCAN = 16 * 1024 * 1024
REPORT_FRESH = 24 * 3600
SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
# Transcript attachments in which Claude Code lists skills, agents, MCP instructions and deferred tools.
LISTINGS = ("skill_listing", "agent_listing_delta", "mcp_instructions_delta", "deferred_tools_delta")
_LISTING_BYTES = tuple(f'"{name}"'.encode() for name in LISTINGS)
# Claude Code writes the reply that ends a turn to the transcript around the time the Stop hook
# runs: before it, about 0.06 s after it starts, or only once it returns (all measured, 2.1.309).
# Read before it, the context is the one of the reply before.
REPLY_WAIT = 0.5
REPLY_POLL = 0.02
REPLY_TAIL = 500


@dataclass(frozen=True)
class Usage:
    tokens: int
    model: str
    window: int
    window_source: str  # config | statusline | model | observed | assumed

    @property
    def pct(self) -> float:
        return 100.0 * self.tokens / self.window

    def describe(self) -> str:
        window = f"{self.window // 1000}k" if self.window < LARGE_WINDOW else "1M"
        note = ", assumed" if self.window_source == "assumed" else ""
        return f"{self.tokens:,} tokens of context (≈{self.pct:.0f}% of a {window} window{note})"


def reverse_lines(path: str, max_bytes: int = MAX_SCAN) -> Iterator[bytes]:
    """Yield the non-empty lines of a file, last line first."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        start = max(0, pos - max_bytes)
        tail = []  # pieces of the line being assembled, latest piece first
        while pos > start:
            step = min(CHUNK, pos - start)
            pos -= step
            f.seek(pos)
            parts = f.read(step).split(b"\n")
            tail.append(parts[-1])
            if len(parts) > 1:
                line = b"".join(reversed(tail))
                if line.strip():
                    yield line
                for middle in reversed(parts[1:-1]):
                    if middle.strip():
                        yield middle
                tail = [parts[0]]
        if pos == 0:
            line = b"".join(reversed(tail))
            if line.strip():
                yield line


def claude_projects() -> Path:
    """The folder where Claude Code keeps a transcript folder per working directory."""
    return config.claude_dir() / "projects"


def session_report(session_id: str) -> Optional[Path]:
    """Where the status-line wrapper records a session's window size, or None
    for an id that is not safe as a file name."""
    if not SESSION_ID_RE.fullmatch(session_id):
        return None
    return config.home() / "sessions" / f"{session_id}.json"


def reported_window(session_id: str) -> Optional[int]:
    """The window size the status line reported for this session in the last 24 hours."""
    path = session_report(session_id)
    report = read_json(path) if path else {}
    window, stamp = report.get("window"), report.get("ts")
    if not isinstance(window, int) or window <= 0 or not isinstance(stamp, (int, float)):
        return None
    return window if time.time() - stamp < REPORT_FRESH else None


def transcripts(cwd: Path) -> List[Path]:
    """Claude Code transcripts of sessions started in cwd, newest first."""
    found = set()
    for folder in {cwd, cwd.resolve()}:
        found.update((claude_projects() / slug(folder)).glob("*.jsonl"))
    return sorted(found, key=lambda path: path.stat().st_mtime, reverse=True)


def resolve_window(model: str, tokens: int, configured: Optional[int], reported: Optional[int] = None) -> Tuple[int, str]:
    if configured:
        return configured, "config"
    if reported:
        return reported, "statusline"
    if "[1m]" in model.lower():
        return LARGE_WINDOW, "model"
    if tokens > DEFAULT_WINDOW:
        return LARGE_WINDOW, "observed"
    return DEFAULT_WINDOW, "assumed"


def _context_tokens(entry: Any) -> int:
    """The context of a main-thread assistant message in a transcript, or 0 for any other entry."""
    if not isinstance(entry, dict) or entry.get("type") != "assistant" or entry.get("isSidechain"):
        return 0
    usage = (entry.get("message") or {}).get("usage") or {}
    return sum(
        int(usage.get(key) or 0) for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )


def last_usage(transcript_path: Optional[str], configured_window: Optional[int] = None) -> Optional[Usage]:
    if not transcript_path:
        return None
    try:
        for raw in reverse_lines(transcript_path):
            if b'"usage"' not in raw or b'"assistant"' not in raw:
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            tokens = _context_tokens(entry)
            if tokens <= 0:
                continue  # synthetic or error messages carry no usage
            model = str((entry.get("message") or {}).get("model") or "")
            # Transcripts are named <session id>.jsonl, the id the status line reports under.
            reported = None if configured_window else reported_window(Path(transcript_path).stem)
            window, source = resolve_window(model, tokens, configured_window, reported)
            return Usage(tokens=tokens, model=model, window=window, window_source=source)
    except OSError:
        return None
    return None


def start_tokens(transcript_path: Optional[str]) -> Optional[int]:
    """What the session holds before any work, mostly Claude Code's own system prompt,
    tools and listings. None until there is a reply to read it from."""
    return session_start(transcript_path)[0]


def _parse(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _reply_id(entry: Any) -> Any:
    return (entry.get("message") or {}).get("id") if isinstance(entry, dict) else None


def session_start(transcript_path: Optional[str]) -> Tuple[Optional[int], bool]:
    """The context at the session's first main-thread reply, or at the first one after the
    latest compaction, and whether that is final: it is once the next reply exists.

    Claude Code usually sends its listings of skills, agents, MCP instructions and deferred
    tools with the first request. In sessions started by a plugin's prompt it can send them
    with the second (measured: about 30k tokens), so then the start is the second reply's
    context, which also holds the first step's tool results."""
    if not transcript_path:
        return None, False
    first = first_id = start = None
    late = False
    try:
        with open(transcript_path, "rb") as f:
            for raw in f:
                if b'"compact_boundary"' in raw and _is_boundary(_parse(raw)):
                    first = first_id = start = None
                    late = False
                elif start is not None:
                    continue
                elif first is not None and any(name in raw for name in _LISTING_BYTES) and _is_listing(_parse(raw)):
                    late = True
                elif b'"usage"' in raw:
                    entry = _parse(raw)
                    tokens = _context_tokens(entry)
                    if tokens and first is None:
                        first, first_id = tokens, _reply_id(entry)
                    elif tokens and (first_id is None or _reply_id(entry) != first_id):
                        start = tokens if late else first
    except OSError:
        return None, False
    return (start, True) if start is not None else (first, False)


def _is_boundary(entry: Any) -> bool:
    return isinstance(entry, dict) and entry.get("type") == "system" and entry.get("subtype") == "compact_boundary"


def _is_listing(entry: Any) -> bool:
    if not isinstance(entry, dict) or entry.get("type") != "attachment" or entry.get("isSidechain"):
        return False
    return (entry.get("attachment") or {}).get("type") in LISTINGS


def last_assistant_text(transcript_path: Optional[str]) -> str:
    """Text of the latest main-thread assistant message.

    Fallback for Claude Code versions whose Stop payload lacks
    last_assistant_message. Each content block is its own transcript line,
    so this joins the text blocks that share the latest message id.
    """
    if not transcript_path:
        return ""
    texts = []
    message_id = None
    try:
        for raw in reverse_lines(transcript_path):
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("isSidechain"):
                continue
            kind = entry.get("type")
            if kind == "user" and message_id is not None:
                break
            if kind != "assistant":
                continue
            message = entry.get("message") or {}
            current = message.get("id")
            if message_id is not None and current != message_id:
                break
            blocks = [
                block.get("text", "")
                for block in message.get("content") or []
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            if message_id is None and not blocks:
                continue
            message_id = current
            texts.extend(reversed(blocks))
    except OSError:
        return ""
    return "\n".join(reversed(texts))


def _squash(text: str) -> str:
    return "".join(text.split())


def await_reply(transcript_path: Optional[str], message: Optional[str], timeout: Optional[float] = None) -> bool:
    """Wait until the transcript's latest main-thread reply is `message`, the Stop payload's
    last_assistant_message, so the context read next is the one at the end of the turn.

    Whitespace is left out of the comparison, and only the end of the message is compared.
    True once the reply is there; False with nothing to compare, or after the wait."""
    tail = _squash(message or "")[-REPLY_TAIL:]
    if not transcript_path or not tail:
        return False
    deadline = time.monotonic() + (REPLY_WAIT if timeout is None else timeout)
    while True:
        if _squash(last_assistant_text(transcript_path)).endswith(tail):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(REPLY_POLL)
