"""`cr.py status [DIR]`: usage, triggers and handoff state for a project."""

from __future__ import annotations

from pathlib import Path
from typing import List

from . import __version__, config, doctor, facts, store
from .config import tilde
from .handlers import age_seconds, describe_age
from .usage import DEFAULT_WINDOW, last_usage, transcripts


def main(args: List[str]) -> int:
    cwd = Path(args[0]).expanduser() if args else Path.cwd()
    root = facts.project_root(str(cwd))
    project = store.Project(root)
    cfg = config.load(project.dir)
    print(f"ContextRail {__version__}{'' if cfg['enabled'] else ' (disabled)'}")
    print(f"Project:   {root}")
    print(f"Storage:   {tilde(project.dir)}")

    found = transcripts(cwd)
    usage = last_usage(str(found[0]), cfg["window_tokens"]) if found else None
    limits = config.thresholds(cfg, usage.window if usage else cfg["window_tokens"] or DEFAULT_WINDOW)
    if usage:
        print(f"Context:   {usage.describe()}, latest session")
    else:
        print("Context:   no session transcript found for this folder")
    print(
        f"Triggers:  handoff at {limits.soft:,} tokens, wrap-up at {limits.hard:,}, "
        f"then again every {cfg['refresh_tokens']:,}"
    )
    errors = doctor.recent_errors(doctor.DAY)
    if errors:
        print(f"Errors:    {len(errors)} in the last 24 h. Run /contextrail:doctor")

    if not project.handoff.exists():
        print("Handoff:   none saved yet")
        return 0
    meta = store.read_json(project.meta)
    size = len(project.handoff.read_text(encoding="utf-8"))
    archived = len(store.archived(project))
    age = describe_age(age_seconds(str(meta.get("saved_at") or "")))
    print(f"Handoff:   saved {age}, {size:,} characters, {archived} older in history")
    print(f"           {tilde(project.handoff)}")
    for note in facts.drift(meta, root):
        print(f"Drift:     {note}")
    return 0
