"""`cr.py history [--restore N] [DIR]`: a project's saved handoffs, newest
first, and making an archived one the current handoff again."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config, facts, handoff, store
from .config import tilde
from .handlers import age_seconds, describe_age

SAVED_RE = re.compile(r"^_Saved (\S+), at ", re.M)
BRANCH_RE = re.compile(r"^- Branch `([^`]*)` at `([0-9a-f]+)`", re.M)
STEP_MARK = re.compile(r"^\s*(\d+[.)]|[-*])\s*")
MAX_SUMMARY = 100
USAGE = "usage: cr.py history [--restore N] [DIR]"


@dataclass(frozen=True)
class Entry:
    path: Path
    saved_at: str
    summary: str  # the first next step, which tells handoffs apart better than the goal
    size: int
    where: str = ""  # the branch and commit it was saved at, as `main` at abc1234

    def point(self) -> str:
        return f", on {self.where}" if self.where else ""

    def when(self) -> str:
        if not self.saved_at:
            return "at an unknown time"
        return f"{self.saved_at[:16].replace('T', ' ')} ({describe_age(age_seconds(self.saved_at))})"


def _entry(path: Path) -> Optional[Entry]:
    try:
        document = path.read_text(encoding="utf-8")
    except OSError:
        return None
    saved = SAVED_RE.search(document)
    found = handoff.sections(handoff.narrative_of(document))
    lines = [line for line in found.get("next steps", "").splitlines() if line.strip()]
    summary = "Next: " + STEP_MARK.sub("", lines[0]) if lines else "Goal: " + found.get("goal", "?")
    meta = store.read_json(path.with_suffix(".json") if path.suffix == ".md" and path.name != "HANDOFF.md" else path.parent / "meta.json")
    branch = BRANCH_RE.search(document)
    head = meta.get("head") or (branch.group(2) if branch else "")
    name = meta.get("branch") or (branch.group(1) if branch else "")
    where = f"`{name}` at {str(head)[:9]}" if head else ""
    return Entry(
        path=path, saved_at=saved.group(1) if saved else "", summary=" ".join(summary.split()), size=len(document),
        where=where,
    )


def entries(project: store.Project) -> Tuple[Optional[Entry], List[Entry]]:
    """The current handoff, and the archived ones, newest first."""
    current = _entry(project.handoff) if project.handoff.exists() else None
    return current, [entry for entry in map(_entry, reversed(store.archived(project))) if entry is not None]


def _archived_meta(entry: Entry, document: str) -> Dict[str, Any]:
    """meta.json as archived beside the handoff, or, for one archived by
    ContextRail 0.1, what its facts section says."""
    meta = store.read_json(entry.path.with_suffix(".json"))
    if meta:
        return meta
    branch = BRANCH_RE.search(document)
    return {
        "saved_at": entry.saved_at,
        "narrative_saved_at": entry.saved_at,
        "branch": branch.group(1) if branch else None,
        "head": branch.group(2) if branch else None,
        "dirty": None,  # listed only in part, so the drift check leaves it out
        "diff_hash": None,
    }


def restore(project: store.Project, number: int, keep: int) -> Entry:
    """Make archived handoff `number` (1 is the newest) current; the current
    one is archived in its place."""
    _, archived = entries(project)
    if not 1 <= number <= len(archived):
        raise ValueError(f"there is no archived handoff {number}; the archive holds {len(archived)}")
    entry = archived[number - 1]
    document = entry.path.read_text(encoding="utf-8")
    meta = _archived_meta(entry, document)
    owner = meta.get("project")
    if isinstance(owner, str) and owner != project.key:
        raise ValueError(f"archived handoff {number} was saved for another project, {owner}")
    meta["restored_from"] = entry.path.name
    with store.locked(project):
        # keep + 1: the restored file is still in the archive while the current one joins it.
        store.save_handoff(project, document, meta, handoff.resume_prompt(handoff.narrative_of(document)), keep + 1)
        entry.path.unlink(missing_ok=True)
        entry.path.with_suffix(".json").unlink(missing_ok=True)
    return entry


def _summary(entry: Entry) -> str:
    text = entry.summary
    return text if len(text) <= MAX_SUMMARY else text[: MAX_SUMMARY - 1] + "…"


def main(args: List[str]) -> int:
    """List the handoffs, or restore one. A bad number exits 1, unless
    --exit-zero is given, as /contextrail:history does."""
    exit_zero = "--exit-zero" in args
    args = [arg for arg in args if arg != "--exit-zero"]
    number = None
    if "--restore" in args:
        at = args.index("--restore")
        value = args[at + 1] if at + 1 < len(args) else ""
        if not value.isdigit():
            print(USAGE)
            return 0 if exit_zero else 2
        number = int(value)
        del args[at:at + 2]
    if len(args) > 1 or any(arg.startswith("-") for arg in args):
        print(USAGE)
        return 0 if exit_zero else 2
    root = facts.project_root(str(Path(args[0]).expanduser() if args else Path.cwd()))
    project = store.open_project(root)
    cfg = config.load(project.dir)

    if number is not None:
        try:
            entry = restore(project, number, cfg["history_keep"])
        except (ValueError, OSError) as error:
            print(f"ContextRail: {error}.")
            return 0 if exit_zero else 1
        print(f"Restored the handoff saved {entry.when()}{entry.point()}, for {root}. {_summary(entry)}")
        print("It loads into the next session (after /clear, or when a session starts here).")
        seconds = age_seconds(entry.saved_at)
        if seconds is not None and seconds > cfg["max_age_days"] * 86_400:
            print(f"It is older than max_age_days ({cfg['max_age_days']}), so it won't load until that is raised.")
        return 0

    current, archived = entries(project)
    if current is None and not archived:
        print(f"No handoffs saved yet for {root}.")
        return 0
    print(f"Handoffs for {root}, newest first ({tilde(project.dir)}):")
    rows = ([("current", current)] if current else []) + [(str(i), entry) for i, entry in enumerate(archived, 1)]
    for label, entry in rows:
        print(f"  {label:>7}  {entry.when()}{entry.point()}, {entry.size:,} characters")
        print(f"           {_summary(entry)}")
    if archived:
        print("To make an archived one current: /contextrail:history --restore N")
    return 0
