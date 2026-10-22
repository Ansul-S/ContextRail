"""Current context usage, read from the end of a Claude Code transcript.

Claude Code counts context as input + cache_creation + cache_read tokens of
the latest main-thread assistant message. Hook payloads do not carry the
window size, so it comes from config or is inferred.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from .store import slug

DEFAULT_WINDOW = 200_000
LARGE_WINDOW = 1_000_000
CHUNK = 256 * 1024
MAX_SCAN = 16 * 1024 * 1024


@dataclass(frozen=True)
class Usage:
    tokens: int
    model: str
    window: int
    window_source: str  # config | model | observed | assumed

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
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"


def transcripts(cwd: Path) -> List[Path]:
    """Claude Code transcripts of sessions started in cwd, newest first."""
    found = set()
    for folder in {cwd, cwd.resolve()}:
        found.update((claude_projects() / slug(folder)).glob("*.jsonl"))
    return sorted(found, key=lambda path: path.stat().st_mtime, reverse=True)


def resolve_window(model: str, tokens: int, configured: Optional[int]) -> Tuple[int, str]:
    if configured:
        return configured, "config"
    if "[1m]" in model.lower():
        return LARGE_WINDOW, "model"
    if tokens > DEFAULT_WINDOW:
        return LARGE_WINDOW, "observed"
    return DEFAULT_WINDOW, "assumed"


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
            if not isinstance(entry, dict) or entry.get("type") != "assistant" or entry.get("isSidechain"):
                continue
            message = entry.get("message") or {}
            usage = message.get("usage") or {}
            tokens = sum(
                int(usage.get(key) or 0)
                for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
            )
            if tokens <= 0:
                continue  # synthetic or error messages carry no usage
            model = str(message.get("model") or "")
            window, source = resolve_window(model, tokens, configured_window)
            return Usage(tokens=tokens, model=model, window=window, window_source=source)
    except OSError:
        return None
    return None


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
