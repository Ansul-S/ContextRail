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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config, facts, store
from .config import tilde
from .usage import LARGE_WINDOW, session_report

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
WRITE_EVERY = 5
KEEP = 7 * 24 * 3600
INNER_TIMEOUT = 5
EXAMPLE = "━━━━━━━━━●┄┄┄┄┄┄┄┄◆┄┄┄┄┄  52k · handoff at 100k · 48k to go · 5% of 1M window"
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


def _ago(seconds: float) -> str:
    if seconds < 60:
        return "just now"
    for size, unit in ((86_400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"


@dataclass
class View:
    """What the hooks will do next in this session, as the status line shows it."""

    enabled: bool
    soft: int
    hard: int
    due: int  # the next handoff request
    saved: Optional[int]  # tokens at this session's last saved handoff
    saved_ago: Optional[float]
    asked: bool
    gave_up: bool
    waiting: bool
    project: str

    @property
    def end(self) -> int:
        """Where the rail ends: the wrap-up point, or past the next request after a save."""
        return max(self.hard, self.due + max(self.hard - self.soft, self.due // 8))


def _view(payload: Dict[str, Any], window: int) -> View:
    workspace = payload.get("workspace") if isinstance(payload.get("workspace"), dict) else {}
    cwd = payload.get("cwd") or workspace.get("current_dir")
    project = store.Project(facts.project_root(cwd)) if isinstance(cwd, str) and cwd else None
    cfg = config.load(project.dir if project else None)
    limits = config.thresholds(cfg, cfg["window_tokens"] or window)
    entry = store.load_state(project)["sessions"].get(str(payload.get("session_id") or "")) if project else None
    entry = entry if isinstance(entry, dict) else {}
    saved = entry.get("saved_tokens") if isinstance(entry.get("saved_tokens"), int) else None
    ago = None
    if saved is not None and project:
        try:
            ago = max(0.0, time.time() - project.handoff.stat().st_mtime)
        except OSError:
            pass
    attempts = entry.get("attempts") if isinstance(entry.get("attempts"), int) else 0
    return View(
        enabled=bool(cfg["enabled"]), soft=limits.soft, hard=limits.hard,
        due=saved + cfg["refresh_tokens"] if saved is not None else limits.soft,
        saved=saved, saved_ago=ago, asked=attempts > 0 and not entry.get("gave_up"),
        gave_up=bool(entry.get("gave_up")), waiting=bool(entry.get("deferred_notice")) and attempts == 0,
        project=project.root.name if project else "",
    )


Colour = Tuple[int, int, int]
GREEN: Colour = (63, 185, 128)
AMBER: Colour = (230, 167, 46)
RED: Colour = (229, 83, 75)
TRACK: Colour = (88, 94, 104)
DIM: Colour = (138, 145, 155)
TRACK_CELLS, NARROW_TRACK_CELLS = 24, 16


def _colour_mode() -> str:
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return ""
    return "24" if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit") else "256"


def _code(colour: Colour, mode: str, bold: bool = False) -> str:
    """The escape sequence that starts text in this colour; 256-colour terminals get the nearest one."""
    if not mode:
        return ""
    r, g, b = colour
    code = f"38;2;{r};{g};{b}" if mode == "24" else f"38;5;{16 + 36 * round(r / 51) + 6 * round(g / 51) + round(b / 51)}"
    return f"\x1b[{'1;' if bold else ''}{code}m"


def _paint(text: str, colour: Colour, mode: str, bold: bool = False) -> str:
    start = _code(colour, mode, bold)
    return f"{start}{text}\x1b[0m" if start else text


def _zone(tokens: float, view: View) -> Colour:
    """Green while there is room, amber towards the handoff, red past the wrap-up point."""
    soft, hard = max(view.soft, 1), max(view.hard, 1)
    calm = 0.6 * soft
    if tokens <= calm:
        return GREEN
    if tokens <= soft:
        return _mix(GREEN, AMBER, (tokens - calm) / (soft - calm))
    if tokens <= hard and hard > soft:
        return _mix(AMBER, RED, (tokens - soft) / (hard - soft))
    return RED


def _mix(a: Colour, b: Colour, share: float) -> Colour:
    return tuple(round(x + (y - x) * share) for x, y in zip(a, b))  # type: ignore[return-value]


def _rail(tokens: int, view: View, cells: int, mode: str) -> str:
    """━━━━━━━━●┄┄┄┄◆┄┄┄: filled to where the session is, ◆ at the next request, ◇ at the last save."""
    per = view.end / cells

    def at(value: int) -> int:
        return min(cells - 1, int(value / per))

    head = at(tokens)
    stations = {at(view.due): ("◆", AMBER)}
    if view.saved is not None:
        stations.setdefault(at(view.saved), ("◇", GREEN))
    runs: List[Tuple[str, str]] = []  # (text, escape sequence)
    for cell in range(cells):
        if cell == head:
            char, colour, bold = "●", _zone(tokens, view), True
        elif cell in stations:
            (char, colour), bold = stations[cell], False
        elif cell < head:
            char, colour, bold = "━", _zone((cell + 0.5) * per, view), False
        else:
            char, colour, bold = "┄", TRACK, False
        start = _code(colour if view.enabled or char == "┄" else DIM, mode, bold)
        if runs and runs[-1][1] == start:
            runs[-1] = (runs[-1][0] + char, start)
        else:
            runs.append((char, start))
    return "".join(f"{start}{text}\x1b[0m" if start else text for text, start in runs)


def _state(tokens: int, view: View, mode: str) -> List[Tuple[int, str, str]]:
    """(priority, plain text, painted text) for what happens next; lower priority is dropped first."""
    def part(priority: int, text: str, colour: Optional[Colour] = None, bold: bool = False) -> Tuple[int, str, str]:
        return priority, text, _paint(text, colour, mode, bold) if colour else text

    if not view.enabled:
        return [part(9, "ContextRail off", DIM)]
    if view.gave_up:
        return [part(9, "✗ no handoff written", RED, True), part(5, "/contextrail:handoff", DIM)]
    if view.asked:
        return [part(9, "◆ handoff requested", AMBER, True)]
    if tokens >= view.due:
        if view.waiting:
            return [part(9, "◷ waiting for background tasks", AMBER, True)]
        return [part(9, "◆ handoff due", AMBER, True)]
    if view.saved is not None:
        saved = "✓ saved" + (f" {_ago(view.saved_ago)}" if view.saved_ago is not None else "")
        return [part(9, saved, GREEN, True), part(6, f"next at {_short(view.due)}")]
    return [part(9, f"handoff at {_short(view.due)}"), part(3, f"{_short(view.due - tokens)} to go", DIM)]


def line(payload: Dict[str, Any]) -> str:
    """ContextRail's own status line, as in EXAMPLE: the rail, then what the hooks do next."""
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
    view = _view(payload, window)
    mode = _colour_mode()
    model = payload.get("model") if isinstance(payload.get("model"), dict) else {}
    name = model.get("display_name") if isinstance(model.get("display_name"), str) else ""
    parts = [(10, _short(tokens), _paint(_short(tokens), _zone(tokens, view) if view.enabled else DIM, mode, True))]
    parts += _state(tokens, view, mode)
    window_text = f"{pct:.0f}% of {_short(window)} window"
    parts.append((4, window_text, _paint(window_text, DIM, mode)))
    parts += [(p, text, _paint(text, DIM, mode)) for p, text in ((2, name), (1, view.project)) if text]
    try:
        room = int(os.environ.get("COLUMNS") or 0) - 4  # Claude Code indents the status line
    except ValueError:
        room = 0
    room = room if room > 0 else 116
    cells = TRACK_CELLS
    while True:
        plain = cells + 2 + sum(len(text) for _, text, _ in parts) + 3 * (len(parts) - 1)
        if plain <= room or (cells == NARROW_TRACK_CELLS and len(parts) <= 2):
            break
        lowest = min(range(len(parts)), key=lambda i: parts[i][0])
        if parts[lowest][0] < 5 or cells == NARROW_TRACK_CELLS:
            del parts[lowest]
        else:
            cells = NARROW_TRACK_CELLS
    sep = _paint(" · ", DIM, mode)
    return _rail(tokens, view, cells, mode) + "  " + sep.join(painted for _, _, painted in parts)


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
