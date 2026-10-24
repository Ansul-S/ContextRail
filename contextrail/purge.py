"""`cr.py purge [WHAT] [--yes] [DIR]`: delete what ContextRail keeps.

Without --yes it lists what would go and deletes nothing. WHAT is one of:

  (nothing)       this project's handoffs: the current one, its archive, resume prompt and session state
  --archives      only this project's archived handoffs (history/)
  --legacy        this project's folder from ContextRail 0.6 or earlier, if it was left in place
  --stale DAYS    every project folder not written to in DAYS days, and old status-line reports
  --everything    all of ContextRail's data, for every project, except the settings in config.json

A project's own config.json is kept unless --everything is given. Claude Code's transcripts are
Claude Code's, under ~/.claude, and are never touched.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import time
from pathlib import Path
from typing import List, Optional, Tuple

from . import config, facts, store
from .config import tilde

USAGE = "usage: cr.py purge [--archives | --legacy | --stale DAYS | --everything] [--yes] [DIR]"
# Files of a project folder that hold handoffs or session state. config.json (settings) stays.
PROJECT_DATA = ("HANDOFF.md", "meta.json", "RESUME_PROMPT.txt", "state.json", "history")
# Files under ContextRail's home that are data rather than settings or the program's own.
HOME_DATA = ("projects", "sessions", "error.log", "heartbeat.json", "eval")
# An error log nobody wrote to for this long is deleted at a session start.
ERROR_LOG_DAYS = 30


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file()) if path.is_dir() else 0


def _delete(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _last_write(folder: Path) -> float:
    stamps = [item.stat().st_mtime for item in folder.rglob("*") if item.is_file()]
    return max(stamps) if stamps else folder.stat().st_mtime


def targets(what: str, project: store.Project, days: Optional[int] = None) -> List[Path]:
    """What a purge of this kind deletes, existing paths only."""
    home = config.home()
    if what == "project":
        found = [project.dir / name for name in PROJECT_DATA]
    elif what == "archives":
        found = [project.history]
    elif what == "legacy":
        found = store.unclaimed_legacy(project)
    elif what == "stale":
        cutoff = time.time() - (days or 0) * 86_400
        projects = home / "projects"
        found = [
            folder for folder in (sorted(projects.iterdir()) if projects.is_dir() else [])
            if folder.is_dir() and _last_write(folder) < cutoff
        ]
        sessions = home / "sessions"
        found += [path for path in sorted(sessions.glob("*.json")) if path.stat().st_mtime < cutoff] if sessions.is_dir() else []
    else:  # everything
        found = [home / name for name in HOME_DATA]
    return [path for path in found if path.exists()]


def _parse(args: List[str]) -> Tuple[Optional[str], Optional[int], bool, List[str]]:
    """(kind, days, yes, the rest), or a kind of None for bad arguments."""
    yes = "--yes" in args
    args = [arg for arg in args if arg != "--yes"]
    kinds = [arg for arg in args if arg in ("--archives", "--legacy", "--stale", "--everything")]
    if len(kinds) > 1:
        return None, None, yes, args
    kind = kinds[0][2:] if kinds else "project"
    days = None
    if kind == "stale":
        at = args.index("--stale")
        value = args[at + 1] if at + 1 < len(args) else ""
        if not value.isdigit() or int(value) < 1:
            return None, None, yes, args
        days = int(value)
        del args[at:at + 2]
    rest = [arg for arg in args if arg not in kinds]
    if len(rest) > 1 or any(arg.startswith("-") for arg in rest):
        return None, None, yes, rest
    return kind, days, yes, rest


def main(args: List[str]) -> int:
    """List or delete. Bad arguments exit 2, unless --exit-zero is given, as /contextrail:purge does."""
    exit_zero = "--exit-zero" in args
    kind, days, yes, rest = _parse([arg for arg in args if arg != "--exit-zero"])
    if kind is None:
        print(USAGE)
        return 0 if exit_zero else 2
    root = facts.project_root(str(Path(rest[0]).expanduser() if rest else Path.cwd()))
    project = store.open_project(root)
    found = targets(kind, project, days)
    scope = {
        "project": f"this project's handoffs and session state ({root})",
        "archives": f"this project's archived handoffs ({root})",
        "legacy": "this project's folder from ContextRail 0.6 or earlier",
        "stale": f"project folders and status-line reports not written to in {days} days",
        "everything": f"all of ContextRail's data under {tilde(config.home())}, except config.json",
    }[kind]
    if not found:
        print(f"ContextRail: nothing to delete: {scope}.")
        return 0
    total = sum(_size(path) for path in found)
    if not yes:
        print(f"ContextRail would delete {scope}, {total:,} bytes:")
        for path in found:
            print(f"  {tilde(path)}")
        print("Nothing was deleted. Add --yes to delete them; it can't be undone.")
        return 0
    # A session saving a handoff meanwhile waits, and then starts from an empty folder.
    lock = store.lock_file(project.lock) if kind in ("project", "archives") else contextlib.nullcontext()
    try:
        with lock:
            for path in found:
                _delete(path)
    except OSError as error:
        print(f"ContextRail: couldn't delete everything: {error}")
        return 0 if exit_zero else 1
    print(f"ContextRail deleted {scope}: {len(found)} item(s), {total:,} bytes.")
    if kind in ("project", "everything"):
        print("The next session here starts without a handoff.")
    return 0


def retention(home: Optional[Path] = None) -> None:
    """Housekeeping at each session start: an error log nobody wrote to for ERROR_LOG_DAYS goes.
    Handoffs are kept by history_keep, session state for 14 days, status-line reports for 7."""
    path = (home or config.home()) / "error.log"
    try:
        if time.time() - os.stat(path).st_mtime > ERROR_LOG_DAYS * 86_400:
            path.unlink()
    except OSError:
        pass
