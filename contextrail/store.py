"""Per-project files under ~/.contextrail/projects/<slug>/, outside every
repository."""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from . import config

SESSION_TTL = 14 * 24 * 3600
MAX_SESSIONS = 50


def slug(root: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(root))


class Project:
    def __init__(self, root: Path, base: Optional[Path] = None):
        self.root = root
        self.dir = (base or config.home()) / "projects" / slug(root)
        self.handoff = self.dir / "HANDOFF.md"
        self.meta = self.dir / "meta.json"
        self.resume = self.dir / "RESUME_PROMPT.txt"
        self.history = self.dir / "history"
        self.state = self.dir / "state.json"
        self.lock = self.dir / ".lock"


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@contextlib.contextmanager
def locked(project: Project) -> Iterator[None]:
    """Serialize state changes between sessions open in the same project."""
    project.dir.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:  # Windows: no advisory locks, accept the small race
        yield
        return
    with open(project.lock, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def load_state(project: Project) -> Dict[str, Any]:
    state = read_json(project.state)
    if not isinstance(state.get("sessions"), dict):
        state["sessions"] = {}
    return state


def session(state: Dict[str, Any], session_id: str) -> Dict[str, Any]:
    entry = state["sessions"].setdefault(session_id, {})
    entry.setdefault("attempts", 0)
    entry.setdefault("retries", 0)
    entry.setdefault("saved_tokens", None)
    entry.setdefault("warned_tokens", None)
    entry.setdefault("gave_up", False)
    return entry


def save_state(project: Project, state: Dict[str, Any]) -> None:
    now = time.time()
    sessions = state["sessions"]
    for entry in sessions.values():
        entry.setdefault("updated", now)
    fresh = {sid: e for sid, e in sessions.items() if now - e["updated"] < SESSION_TTL}
    newest = sorted(fresh.items(), key=lambda item: item[1]["updated"], reverse=True)[:MAX_SESSIONS]
    state["sessions"] = dict(newest)
    write_atomic(project.state, json.dumps(state, indent=2))


def save_handoff(project: Project, document: str, meta: Dict[str, Any], resume: str, keep: int) -> None:
    if project.handoff.exists():
        project.history.mkdir(parents=True, exist_ok=True)
        stamp = re.sub(r"[^0-9T]", "-", str(read_json(project.meta).get("saved_at", "")))[:19]
        if not stamp:
            stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime(project.handoff.stat().st_mtime))
        target = project.history / f"{stamp}.md"
        counter = 1
        while target.exists():
            target = project.history / f"{stamp}-{counter}.md"
            counter += 1
        os.replace(project.handoff, target)
        archived = sorted(project.history.glob("*.md"))
        for old in archived[: max(0, len(archived) - keep)]:
            old.unlink()
    write_atomic(project.handoff, document)
    write_atomic(project.meta, json.dumps(meta, indent=2))
    if resume:
        write_atomic(project.resume, resume + "\n")
