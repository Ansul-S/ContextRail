"""`cr.py statusline [-- COMMAND]`: an optional status-line wrapper.

Hooks are not told the size of the context window, but the status line is.
The wrapper records it for the hooks, then prints the user's own status line,
or a short one of its own. `cr.py doctor --setup-statusline` writes a launcher
that keeps working when an update moves the plugin, and prints the settings
entry to add. ContextRail never edits Claude Code's settings itself.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config, facts, store
from .config import tilde
from .usage import LARGE_WINDOW, session_report

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
WRITE_EVERY = 5
KEEP = 7 * 24 * 3600
INNER_TIMEOUT = 5
EXAMPLE = "ctx 52% · 104k/200k · handoff at 100k"
LAUNCHER = """#!/bin/sh
# ContextRail status-line launcher, written by `cr.py doctor --setup-statusline`.
# Updates move the plugin to a new folder, so look it up on every run.
root=$(cat "${CONTEXTRAIL_HOME:-$HOME/.contextrail}/plugin-root" 2>/dev/null)
if [ -n "$root" ] && [ -f "$root/contextrail/cr.py" ]; then
  exec python3 "$root/contextrail/cr.py" statusline "$@"
fi
# ContextRail is gone: run the wrapped status line on its own.
[ "$1" = "--" ] && shift
[ $# -eq 1 ] && exec /bin/sh -c "$1"
[ $# -gt 1 ] && exec "$@"
exit 0
"""


def plugin_root_file() -> Path:
    return config.home() / "plugin-root"


def launcher_path() -> Path:
    return config.home() / "bin" / "statusline"


def settings_path() -> Path:
    return config.claude_dir() / "settings.json"


def note_plugin_root() -> None:
    """Record where this copy of the plugin is, for the launcher. Runs on every SessionStart."""
    path = plugin_root_file()
    try:
        if path.read_text(encoding="utf-8").strip() == str(PLUGIN_ROOT):
            return
    except OSError:
        pass
    try:
        store.write_atomic(path, f"{PLUGIN_ROOT}\n")
    except OSError:
        pass


def parse(raw: bytes) -> Dict[str, Any]:
    try:
        data = json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _context(payload: Dict[str, Any]) -> Tuple[Optional[int], Dict[str, Any]]:
    """The window size from a status-line payload, and its context_window object."""
    info = payload.get("context_window")
    if not isinstance(info, dict):
        return None, {}
    window = info.get("context_window_size")
    return (window if isinstance(window, int) and window > 0 else None), info


def record(payload: Dict[str, Any]) -> None:
    """Note the session's window size for the hooks, at most every few seconds."""
    window, info = _context(payload)
    session_id = payload.get("session_id")
    path = session_report(session_id) if isinstance(session_id, str) else None
    if window is None or path is None:
        return
    now = time.time()
    previous = store.read_json(path)
    stamp = previous.get("ts")
    if previous.get("window") == window and isinstance(stamp, (int, float)) and 0 <= now - stamp < WRITE_EVERY:
        return
    used = info.get("used_percentage")
    report = {"window": window, "used_pct": used if isinstance(used, (int, float)) else None, "ts": now}
    try:
        store.write_atomic(path, json.dumps(report))
    except OSError:
        return
    if not previous:
        _prune(path.parent, now)


def _prune(folder: Path, now: float) -> None:
    for path in folder.glob("*.json"):
        try:
            if now - path.stat().st_mtime > KEEP:
                path.unlink()
        except OSError:
            continue


def _short(tokens: int) -> str:
    return f"{tokens / LARGE_WINDOW:g}M" if tokens >= LARGE_WINDOW else f"{round(tokens / 1000)}k"


def _due(payload: Dict[str, Any], window: int) -> str:
    """When this session's next handoff request is due, as the hooks work it out."""
    workspace = payload.get("workspace") if isinstance(payload.get("workspace"), dict) else {}
    cwd = payload.get("cwd") or workspace.get("current_dir")
    if not isinstance(cwd, str) or not cwd:
        return ""
    project = store.Project(facts.project_root(cwd))
    cfg = config.load(project.dir)
    if not cfg["enabled"]:
        return "ContextRail off"
    entry = store.load_state(project)["sessions"].get(str(payload.get("session_id") or ""))
    saved = entry.get("saved_tokens") if isinstance(entry, dict) else None
    if isinstance(saved, int):
        return f"handoff at {_short(saved + cfg['refresh_tokens'])}"
    return f"handoff at {_short(config.thresholds(cfg, cfg['window_tokens'] or window).soft)}"


def line(payload: Dict[str, Any]) -> str:
    """ContextRail's own status line, for users who have none: ctx 52% · 104k/200k · handoff at 100k."""
    window, info = _context(payload)
    if window is None:
        return ""
    current = info.get("current_usage") if isinstance(info.get("current_usage"), dict) else {}
    tokens = sum(
        int(current.get(key) or 0) for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )
    pct = info.get("used_percentage")
    if not isinstance(pct, (int, float)):
        pct = 100 * tokens / window
    due = _due(payload, window)
    return f"ctx {pct:.0f}% · {_short(tokens)}/{_short(window)}" + (f" · {due}" if due else "")


def show(args: List[str], raw: bytes) -> int:
    """Pass the wrapped status line through unchanged, or print ContextRail's own."""
    inner = args[1:] if args[:1] == ["--"] else args
    if not inner:
        text = line(parse(raw))
        if text:
            print(text)
        return 0
    # One argument is a command line as it stood in settings.json, so it runs through the shell.
    command = inner[0] if len(inner) == 1 else inner
    try:
        done = subprocess.run(command, shell=len(inner) == 1, input=raw, capture_output=True, timeout=INNER_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return 0
    sys.stdout.buffer.write(done.stdout)
    sys.stderr.buffer.write(done.stderr)
    return done.returncode if done.returncode >= 0 else 1


def current_status_line() -> Tuple[Optional[Dict[str, Any]], str]:
    """The statusLine entry of Claude Code's user settings, and a problem reading them."""
    path = settings_path()
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, ""
    except (OSError, ValueError) as error:
        return None, f"{tilde(path)} can't be read: {error}"
    entry = settings.get("statusLine") if isinstance(settings, dict) else None
    return (entry if isinstance(entry, dict) else None), ""


def uses_wrapper(entry: Optional[Dict[str, Any]]) -> bool:
    command = entry.get("command") if entry else None
    return isinstance(command, str) and "contextrail" in command and "statusline" in command


def newest_report() -> Optional[Tuple[float, int]]:
    """The time and window size of the latest report from any session."""
    reports = [store.read_json(path) for path in (config.home() / "sessions").glob("*.json")]
    stamped = [
        (report["ts"], report["window"])
        for report in reports
        if isinstance(report.get("ts"), (int, float)) and isinstance(report.get("window"), int)
    ]
    return max(stamped) if stamped else None


def setup() -> int:
    """Write the launcher and print the settings entry that turns it on."""
    if os.name == "nt":
        print("The status-line launcher is a shell script, so it doesn't work on Windows yet.")
        return 1
    launcher = launcher_path()
    try:
        store.write_atomic(launcher, LAUNCHER)
        launcher.chmod(0o755)
    except OSError as error:
        print(f"Couldn't write {tilde(launcher)}: {error.strerror or error}")
        return 1
    note_plugin_root()
    print(f"Wrote the launcher {tilde(launcher)}.")
    entry, problem = current_status_line()
    if problem:
        print(f"{problem}. Fix it, then run this again.")
        return 1
    if uses_wrapper(entry):
        print("Your status line already runs through ContextRail. Nothing else to do.")
        return 0
    inner = (entry or {}).get("command")
    inner = inner if isinstance(inner, str) and inner.strip() else None
    updated = dict(entry or {})
    updated["type"] = "command"
    updated["command"] = shlex.quote(str(launcher)) + (f" -- {shlex.quote(inner)}" if inner else "")
    print(f"To turn it on, set this in {tilde(settings_path())}, in place of any \"statusLine\" there:")
    print()
    print(f'  "statusLine": {json.dumps(updated, ensure_ascii=False)}')
    print()
    if inner:
        print("Your current status line stays as it is: the launcher runs it and passes its output through.")
    else:
        print(f"With no status line of your own, ContextRail shows one: {EXAMPLE}")
    print(
        "ContextRail never edits that file. Restart Claude Code afterwards; "
        "from then on the hooks use the exact window size."
    )
    return 0
