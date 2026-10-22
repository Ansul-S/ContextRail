"""`cr.py doctor [DIR]`: check that ContextRail can run, that its hooks fire,
and that the handoff reaches the next session. Exits 1 if a check fails."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from . import __version__, config, facts, handlers, statusline, store
from .config import tilde
from .usage import REPORT_FRESH, claude_projects, last_usage, transcripts

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
ISSUES_URL = "https://github.com/Ansul-S/ContextRail/issues"
SLOW_START_MS = 150
DAY = 86_400
WEEK = 7 * DAY
TRANSCRIPTS_TO_TRY = 5
STOP_GRACE = 10 * 60
ERROR_HEADER = re.compile(r"--- (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) ?(.*)")
WINDOW_SOURCES = {
    "config": "the window_tokens setting",
    "statusline": "the status line",
    "model": "the model name",
    "observed": "usage above 200k",
    "assumed": (
        "the 200k default. For the exact size, run /contextrail:doctor --setup-statusline, or set window_tokens"
    ),
}
SETUP_HINT = "run /contextrail:doctor --setup-statusline"
LABELS = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}


@dataclass(frozen=True)
class Result:
    status: str  # ok | warn | fail
    name: str
    detail: str


def hook_environment() -> Dict[str, str]:
    """The environment a hook gets from Claude Code. When the doctor itself was
    started through a pyenv shim, pyenv has put its own folders first on PATH and
    set version variables; undo that so python3 resolves as it does for a hook."""
    env = dict(os.environ)
    prefix = env.pop("_PYENV_INSTALL_PREFIX", None)  # set only inside `pyenv exec`
    if prefix is None:
        return env
    for name in ("PYENV_VERSION", "PYENV_DIR", "PYENV_HOOK_PATH"):
        env.pop(name, None)
    added = (prefix.rstrip(os.sep) + os.sep, os.path.join(env.get("PYENV_ROOT", "~"), "versions") + os.sep)
    env["PATH"] = os.pathsep.join(
        entry for entry in env.get("PATH", "").split(os.pathsep) if not (entry.rstrip(os.sep) + os.sep).startswith(added)
    )
    return env


def check_python() -> Result:
    """The python3 that the hooks run: its version and how long a hook takes to start."""
    env = hook_environment()
    python = shutil.which("python3", path=env.get("PATH"))
    if python is None:
        return Result("fail", "Python", "python3 is not on PATH, so the hooks can't start")
    shown = tilde(Path(python))

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([python, *args], capture_output=True, text=True, timeout=20, env=env)

    try:
        version = run("-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])").stdout.strip()
        timings = []
        for _ in range(2):  # the faster of two, in case the first pays for a cold disk cache
            start = time.perf_counter()
            entry = run(str(PLUGIN_ROOT / "contextrail" / "cr.py"), "--version")
            timings.append((time.perf_counter() - start) * 1000)
    except (OSError, subprocess.SubprocessError) as error:
        return Result("fail", "Python", f"{shown} could not be run: {error}")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        return Result("fail", "Python", f"{shown} could not be run")
    if tuple(int(part) for part in version.split(".")[:2]) < (3, 9):
        return Result("fail", "Python", f"{shown} is Python {version}; ContextRail needs 3.9 or later")
    if entry.returncode != 0:
        last = (entry.stderr.strip().splitlines() or ["no output"])[-1]
        return Result("fail", "Python", f"{shown} can't run ContextRail: {last}")
    fastest = min(timings)
    detail = f"{version} at {shown}, hook start-up {fastest:.0f} ms"
    if fastest > SLOW_START_MS:
        cause = (
            " It is a version-manager shim; a real python3 ahead of it on PATH would make hooks faster."
            if os.sep + "shims" + os.sep in python
            else ""
        )
        return Result("warn", "Python", f"{detail}, which every hook pays.{cause}")
    return Result("ok", "Python", detail)


def check_plugin() -> Result:
    """hooks/hooks.json parses and each hook runs a cr.py command that exists."""
    path = PLUGIN_ROOT / "hooks" / "hooks.json"
    try:
        hooks = json.loads(path.read_text(encoding="utf-8"))["hooks"]
        commands = [
            (event, hook["command"]) for event, groups in hooks.items() for group in groups for hook in group["hooks"]
        ]
    except (OSError, ValueError, KeyError, TypeError) as error:
        return Result("fail", "Plugin", f"{tilde(path)} can't be read: {error!r}")
    problems = []
    for event, command in commands:
        try:
            words = shlex.split(command.replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN_ROOT)))
        except ValueError:
            words = []
        script = next((Path(word) for word in words if word.endswith("cr.py")), None)
        if script is None or not script.is_file():
            problems.append(f"the {event} hook doesn't run an existing cr.py")
        elif handlers.HOOKS.get(words[-1], ("",))[0] != event:
            problems.append(f"the {event} hook runs an unknown command, {words[-1]!r}")
    missing = [event for event, _ in handlers.HOOKS.values() if event not in hooks]
    if missing:
        problems.append("no hook for " + ", ".join(missing))
    if problems:
        return Result("fail", "Plugin", "; ".join(problems))
    return Result("ok", "Plugin", f"{len(commands)} hooks ({', '.join(hooks)}) in {tilde(PLUGIN_ROOT)}")


def check_storage() -> Result:
    home = config.home()
    probe = home / f".doctor-{os.getpid()}.tmp"
    try:
        home.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as error:
        return Result("fail", "Storage", f"can't write to {tilde(home)}: {error.strerror or error}")
    return Result("ok", "Storage", f"{tilde(home)} is writable")


def check_settings(project: store.Project, cfg: Dict[str, Any]) -> Result:
    problems = config.problems(project.dir)
    if problems:
        return Result("fail", "Settings", "; ".join(problems) + ". These are ignored until fixed")
    sources = [
        tilde(path) for path in (config.home() / "config.json", project.dir / "config.json") if path.exists()
    ]
    sources += sorted(name for name in os.environ if name.startswith(config.ENV_PREFIX) and name != "CONTEXTRAIL_HOME")
    origin = "from " + ", ".join(sources) if sources else "built-in defaults"
    if not cfg["enabled"]:
        return Result("warn", "Settings", f"ContextRail is turned off ({origin})")
    return Result("ok", "Settings", origin)


def check_heartbeats() -> Result:
    """When each hook last ran, as recorded by handlers.heartbeat."""
    beats = store.read_json(config.home() / handlers.HEARTBEAT)
    now = time.time()
    seen = []
    never = []
    for event, _ in handlers.HOOKS.values():
        stamp = beats.get(event)
        if isinstance(stamp, (int, float)):
            seen.append(f"{event} {handlers.describe_age(now - stamp)}")
        else:
            never.append(event)
    detail = "last ran: " + ", ".join(seen + [f"{event} never" for event in never])
    started = beats.get("SessionStart")
    # Stop runs when Claude finishes a reply, so it can lag the first SessionStart.
    stop_overdue = "Stop" in never and isinstance(started, (int, float)) and now - started > STOP_GRACE
    if "SessionStart" in never or stop_overdue:
        return Result(
            "warn",
            "Hooks",
            f"{detail}. If ContextRail was just installed or updated, restart Claude Code; "
            "otherwise check in /plugin that contextrail is enabled",
        )
    return Result("ok", "Hooks", detail)


def _newest_transcripts() -> List[Path]:
    """The newest transcripts of any folder, for when this one has none yet."""
    stamped = []
    for path in claude_projects().glob("*/*.jsonl"):
        try:
            stamped.append((path.stat().st_mtime, path))
        except OSError:
            continue
    return [path for _, path in sorted(stamped, reverse=True)[:TRANSCRIPTS_TO_TRY]]


def check_transcript(cwd: Path, cfg: Dict[str, Any]) -> Result:
    """Token usage can be read from Claude Code's transcripts."""
    found = transcripts(cwd)[:TRANSCRIPTS_TO_TRY]
    where = "latest session in this folder"
    if not found:
        found = _newest_transcripts()
        where = "latest session in another folder, as this one has none yet"
    if not found:
        return Result(
            "warn", "Transcript", "no Claude Code transcripts found, so /contextrail:status can't show usage"
        )
    for path in found:
        usage = last_usage(str(path), cfg["window_tokens"])
        if usage is not None:
            return Result(
                "ok",
                "Transcript",
                f"{usage.describe()}, {where}. Window size from {WINDOW_SOURCES[usage.window_source]}",
            )
    if len(found) == 1:
        return Result("warn", "Transcript", "the only session found has no token usage yet")
    return Result(
        "fail",
        "Transcript",
        f"no token usage in the {len(found)} newest transcripts, so the handoff request can't "
        "trigger. Claude Code's transcript format may have changed",
    )


def check_statusline() -> Result:
    """The optional status-line wrapper, which tells the hooks the exact window size."""
    report = statusline.newest_report()
    if report and time.time() - report[0] < REPORT_FRESH:
        stamp, window = report
        age = handlers.describe_age(time.time() - stamp)
        return Result("ok", "Status line", f"last reported a {window:,}-token window {age}")
    entry, _ = statusline.current_status_line()
    if not statusline.uses_wrapper(entry):
        return Result("ok", "Status line", f"not set up (optional). For the exact window size, {SETUP_HINT}")
    launcher = statusline.launcher_path()
    if str(launcher) in str(entry.get("command")) and not os.access(launcher, os.X_OK):
        return Result("fail", "Status line", f"settings.json runs {tilde(launcher)}, which is missing; {SETUP_HINT}")
    return Result(
        "warn", "Status line", "set up in settings.json, but it hasn't reported a window size in 24 hours. Restart Claude Code"
    )


def check_delivery(root: Path, cfg: Dict[str, Any]) -> Result:
    """Run the SessionStart handler (it changes nothing) and check what it would load."""
    project = store.Project(root)
    if not project.handoff.exists():
        return Result("ok", "Handoff", "none saved yet for this project")
    if not cfg["enabled"]:
        return Result("warn", "Handoff", "saved, but ContextRail is turned off, so it won't load")
    sources = [source for source in cfg["inject_on"] if source in config.SESSION_SOURCES]
    if not sources:
        return Result("warn", "Handoff", "saved, but inject_on names no session start, so it never loads")
    output = handlers.on_session_start({"cwd": str(root), "source": sources[0], "session_id": "contextrail-doctor"})
    if output is None:
        seconds = handlers.age_seconds(str(store.read_json(project.meta).get("saved_at") or ""))
        if seconds is not None and seconds > cfg["max_age_days"] * DAY:
            return Result(
                "warn",
                "Handoff",
                f"saved {handlers.describe_age(seconds)}, older than max_age_days ({cfg['max_age_days']}), "
                "so it won't load",
            )
        return Result("fail", "Handoff", "saved, but the SessionStart hook returns nothing to load")
    context = (output.get("hookSpecificOutput") or {}).get("additionalContext")
    if not isinstance(context, str) or not context:
        return Result("fail", "Handoff", "the SessionStart hook returns no additionalContext, so the model won't see it")
    return Result(
        "ok", "Handoff", f"loads on {', '.join(sources)}: {len(context):,} characters (≈{len(context) // 4:,} tokens)"
    )


def read_errors() -> List[Tuple[float, str, str]]:
    """Entries of error.log as (time, command, last line of the traceback)."""
    try:
        text = (config.home() / "error.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    entries: List[List[Any]] = []
    for line in text.splitlines():
        header = ERROR_HEADER.fullmatch(line)
        if header:
            try:
                stamp = time.mktime(time.strptime(header.group(1), "%Y-%m-%d %H:%M:%S"))
            except (ValueError, OverflowError):
                continue
            entries.append([stamp, header.group(2), ""])
        elif entries and line.strip():
            entries[-1][2] = line.strip()
    return [(stamp, command, last) for stamp, command, last in entries]


def recent_errors(seconds: float) -> List[Tuple[float, str, str]]:
    now = time.time()
    return [entry for entry in read_errors() if now - entry[0] < seconds]


def check_errors() -> Result:
    week = recent_errors(WEEK)
    if not week:
        return Result("ok", "Errors", "none in the last 7 days")
    now = time.time()
    day = [entry for entry in week if now - entry[0] < DAY]
    stamp, command, last = week[-1]
    detail = (
        f"{len(week)} in the last 7 days, {len(day)} in the last 24 h. Latest, "
        f"{handlers.describe_age(now - stamp)} in `{command or '?'}`: {last or 'no details'}. "
        f"Full log: {tilde(config.home() / 'error.log')}"
    )
    return Result("fail" if day else "warn", "Errors", detail)


def _guarded(name: str, check: Callable[..., Result], *args: Any) -> Result:
    try:
        return check(*args)
    except Exception as error:  # the doctor reports problems, it never adds one
        return Result("fail", name, f"the check itself failed: {error!r}")


def run(cwd: Path) -> Tuple[Path, List[Result]]:
    """The project root for cwd, and the result of every check."""
    root = facts.project_root(str(cwd))
    project = store.Project(root)
    cfg = config.load(project.dir)
    return root, [
        _guarded("Python", check_python),
        _guarded("Plugin", check_plugin),
        _guarded("Storage", check_storage),
        _guarded("Settings", check_settings, project, cfg),
        _guarded("Hooks", check_heartbeats),
        _guarded("Transcript", check_transcript, cwd, cfg),
        _guarded("Status line", check_statusline),
        _guarded("Handoff", check_delivery, root, cfg),
        _guarded("Errors", check_errors),
    ]


def main(args: List[str]) -> int:
    """Exit 1 if a check fails, unless --exit-zero is given: a slash command's
    shell block that exits non-zero never reaches the model."""
    exit_zero = "--exit-zero" in args
    setup = "--setup-statusline" in args
    args = [arg for arg in args if arg not in ("--exit-zero", "--setup-statusline")]
    if any(arg.startswith("-") for arg in args):
        print("usage: cr.py doctor [DIR] [--exit-zero] [--setup-statusline]", file=sys.stderr)
        return 2
    if setup:
        code = statusline.setup()
        return 0 if exit_zero else code
    cwd = Path(args[0]).expanduser() if args else Path.cwd()
    root, results = run(cwd)
    print(f"ContextRail {__version__} doctor, for {root}")
    width = max(len(result.name) for result in results) + 1
    for result in results:
        print(f"  {LABELS[result.status]}  {result.name + ':':<{width}} {result.detail}")
    failed = sum(result.status == "fail" for result in results)
    warned = sum(result.status == "warn" for result in results)
    if failed:
        print(f"{failed} check(s) failed, {warned} warning(s). If you can't fix it, please report it at {ISSUES_URL}")
    elif warned:
        print(f"No failures, {warned} warning(s).")
    else:
        print("All checks passed.")
    return 1 if failed and not exit_zero else 0
