"""Per-project files under ~/.contextrail/projects/<slug>/, outside every
repository."""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

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
    entry.setdefault("deferred_notice", False)
    entry.setdefault("start_tokens", None)
    # "checkpoint" or "final": the kind of the latest request, and of the handoff saved for it.
    entry.setdefault("asked_kind", None)
    entry.setdefault("saved_kind", None)
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


def archive_order(path: Path) -> Tuple[str, int]:
    """Oldest first: archives are named <stamp>.md, or <stamp>-<n>.md for later
    ones saved in the same second (the stamp is 19 characters)."""
    stem = path.stem
    counter = stem[20:] if stem[19:20] == "-" else ""
    return stem[:19], int(counter) if counter.isdigit() else 0


def archived(project: Project) -> List[Path]:
    """The archived handoffs, oldest first."""
    return sorted(project.history.glob("*.md"), key=archive_order) if project.history.exists() else []


def save_handoff(project: Project, document: str, meta: Dict[str, Any], resume: str, keep: int) -> None:
    if project.handoff.exists():
        project.history.mkdir(parents=True, exist_ok=True)
        stamp = re.sub(r"[^0-9T]", "-", str(read_json(project.meta).get("saved_at", "")))[:19]
        if not stamp:
            stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime(project.handoff.stat().st_mtime))
        # Past every archive of the same second, even one pruned below it, so names keep their order.
        same = [archive_order(path)[1] for path in project.history.glob(f"{stamp}*.md") if path.stem[:19] == stamp]
        target = project.history / (f"{stamp}-{max(same) + 1}.md" if same else f"{stamp}.md")
        os.replace(project.handoff, target)
        if project.meta.exists():  # kept beside it, so a restored handoff gets its drift check
            os.replace(project.meta, target.with_suffix(".json"))
        older = archived(project)
        for old in older[: max(0, len(older) - keep)]:
            old.unlink()
            old.with_suffix(".json").unlink(missing_ok=True)
    write_atomic(project.handoff, document)
    write_atomic(project.meta, json.dumps(meta, indent=2))
    if resume:
        write_atomic(project.resume, resume + "\n")
