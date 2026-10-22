"""`cr.py status [DIR]`: usage, triggers and handoff state for a project."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from . import __version__, config, facts, store
from .handlers import age_seconds, describe_age, tilde
from .usage import DEFAULT_WINDOW, last_usage


def latest_transcript(cwd: Path) -> Optional[Path]:
    """Newest Claude Code transcript for sessions started in cwd."""
    claude_home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    candidates: List[Path] = []
    for folder in {cwd, cwd.resolve()}:
        candidates += (claude_home / "projects" / store.slug(folder)).glob("*.jsonl")
    return max(candidates, key=lambda path: path.stat().st_mtime, default=None)


def main(args: List[str]) -> int:
    cwd = Path(args[0]).expanduser() if args else Path.cwd()
    root = facts.project_root(str(cwd))
    project = store.Project(root)
    cfg = config.load(project.dir)
    print(f"ContextRail {__version__}{'' if cfg['enabled'] else ' (disabled)'}")
    print(f"Project:   {root}")
    print(f"Storage:   {tilde(project.dir)}")

    transcript = latest_transcript(cwd)
    usage = last_usage(str(transcript), cfg["window_tokens"]) if transcript else None
    limits = config.thresholds(cfg, usage.window if usage else cfg["window_tokens"] or DEFAULT_WINDOW)
    if usage:
        print(f"Context:   {usage.describe()}, latest session")
    else:
        print("Context:   no session transcript found for this folder")
    print(
        f"Triggers:  handoff at {limits.soft:,} tokens, wrap-up at {limits.hard:,}, "
        f"then again every {cfg['refresh_tokens']:,}"
    )

    if not project.handoff.exists():
        print("Handoff:   none saved yet")
        return 0
    meta = store.read_json(project.meta)
    size = len(project.handoff.read_text(encoding="utf-8"))
    archived = len(list(project.history.glob("*.md"))) if project.history.exists() else 0
    age = describe_age(age_seconds(str(meta.get("saved_at") or "")))
    print(f"Handoff:   saved {age}, {size:,} characters, {archived} older in history")
    print(f"           {tilde(project.handoff)}")
    for note in facts.drift(meta, root):
        print(f"Drift:     {note}")
    return 0
