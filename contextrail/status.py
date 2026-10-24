"""`cr.py status [DIR]`: usage, triggers and handoff state for a project."""

from __future__ import annotations

import time
from pathlib import Path
from typing import List

from . import __version__, config, doctor, facts, handlers, store
from .config import tilde
from .handlers import age_seconds, describe_age
from .usage import DEFAULT_WINDOW, claude_version, last_usage, start_tokens, transcripts


def main(args: List[str]) -> int:
    cwd = Path(args[0]).expanduser() if args else Path.cwd()
    root = facts.project_root(str(cwd))
    project = store.open_project(root)
    cfg = config.load(project.dir)
    print(f"ContextRail {__version__}{'' if cfg['enabled'] else ' (disabled)'}")
    print(f"Project:   {root}")
    print(f"Storage:   {tilde(project.dir)}")

    found = transcripts(cwd)
    usage = last_usage(str(found[0]), cfg["window_tokens"]) if found else None
    start = (start_tokens(str(found[0])) or 0) if usage else 0
    limits = config.thresholds(cfg, usage.window if usage else cfg["window_tokens"] or DEFAULT_WINDOW, start)
    if usage:
        print(f"Context:   {usage.describe()}, latest session")
        print(f"Start:     {start:,} tokens before any work; the token triggers count from there")
        print(f"Window:    {usage.window:,} tokens, from {doctor.WINDOW_SOURCES[usage.window_source]}")
    else:
        print("Context:   no session transcript found for this folder")
    print(
        f"Triggers:  handoff at {limits.soft:,} tokens, wrap-up at {limits.hard:,}, "
        f"then again every {cfg['refresh_tokens']:,}"
    )
    if config.splits(cfg):
        print("Sessions:  a new session is offered at the wrap-up point; handoffs before it are checkpoints")
    else:
        print("Sessions:  a new session is offered after every handoff")
    if usage:
        why = config.explain(cfg, limits, usage.window, usage.window_source, start)
        print(f"Why:       {why[0]}")
        for line in why[1:]:
            print(f"Warning:   {line}")
    if found:
        payload = {"transcript_path": str(found[0])}
        version = claude_version(str(found[0]))
        how = "as hook feedback" if handlers.feedback_supported(payload, cfg) else 'as a blocked stop, labelled "Stop hook error"'
        shown = f"Claude Code {'.'.join(map(str, version))}" if version else "Claude Code version unknown"
        print(f"Requests:  {how} ({shown}, request_style {cfg['request_style']})")
        entry = store.load_state(project)["sessions"].get(found[0].stem) or {}
        trigger = entry.get("trigger") if isinstance(entry.get("trigger"), dict) else None
        if trigger and isinstance(trigger.get("tokens"), int):
            print(f"Asked:     at {trigger['tokens']:,} tokens for a {trigger.get('kind', 'final')} handoff: {trigger.get('because', '')}")
        failure = entry.get("failure") if isinstance(entry.get("failure"), dict) else None
        if failure and isinstance(failure.get("time"), (int, float)):
            ago = describe_age(time.time() - failure["time"])
            print(f"API error: the latest turn that failed ended on {failure.get('error')} {ago}; the saved handoff was kept")
    if cfg["lightning_tokens"]:
        print(f"Lightning: on at {cfg['lightning_tokens']:,}: a new session starts after the handoff (/contextrail:lightning)")
    errors = doctor.recent_errors(doctor.DAY)
    if errors:
        print(f"Errors:    {len(errors)} in the last 24 h. Run /contextrail:doctor")

    if not project.handoff.exists():
        print("Handoff:   none saved yet")
        return 0
    document, meta, problems = store.read_handoff(project)
    if document is None:
        print(f"Handoff:   saved, but it won't load: {problems[0] if problems else 'it can not be read'}")
        return 0
    archived = len(store.archived(project))
    age = describe_age(age_seconds(str(meta.get("saved_at") or "")))
    print(f"Handoff:   saved {age}, {len(document):,} characters, {archived} older in history")
    print(f"           {tilde(project.handoff)}")
    found_drift = facts.drift(meta, root) if not problems else facts.Drift([problems[0]], [], [], False)
    for note in found_drift.notes:
        print(f"Drift:     {note}")
    return 0
