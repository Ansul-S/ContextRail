#!/usr/bin/env python3
"""ContextRail: automatic session handoff for Claude Code.

Hooks (JSON payload on stdin):  cr.py stop | stop-failure | post-tool | pre-compact | session-start | session-end
Status line (optional):         cr.py statusline [-- COMMAND]
Manual:                         cr.py status [DIR] | doctor [DIR] [--exit-zero] [--setup-statusline]
                                cr.py history [--restore N] [DIR]
                                cr.py lightning [LIMIT | off]
                                cr.py purge [--archives | --legacy | --stale DAYS | --everything] [--yes] [DIR]
"""

import importlib
import json
import sys
import time
import traceback
from pathlib import Path

# Import the package from the plugin root, not modules from this folder.
sys.path[0] = str(Path(__file__).resolve().parent.parent)

# The commands' modules are imported when used: hooks run on every tool call, and only need handlers.
from contextrail import __version__, config, handlers  # noqa: E402

ERROR_LOG_LIMIT = 256 * 1024


def log_error() -> None:
    """Hooks must never break a session: record the error and carry on. The log is private and
    kept small: past ERROR_LOG_LIMIT it starts again."""
    from contextrail import store
    try:
        path = config.home() / "error.log"
        if path.exists() and path.stat().st_size > ERROR_LOG_LIMIT:
            path.unlink()
        store.append_private(
            path, f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(sys.argv[1:])}\n{traceback.format_exc()}\n"
        )
    except OSError:
        pass


def utf8_output() -> None:
    """Claude Code reads what the hooks, commands and status line print as UTF-8. Without this,
    Windows would write the console's code page, which lacks characters such as the rail's."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")
        except (AttributeError, ValueError, OSError):
            pass  # not a text stream (a test's replacement), or no stream at all


def run_statusline(args):
    """The status line must keep working: errors are logged, and the wrapped command still runs."""
    from contextrail import statusline
    try:
        raw = sys.stdin.buffer.read()
    except (OSError, ValueError):
        raw = b""
    try:
        statusline.record(statusline.parse(raw))
    except Exception:
        log_error()
    try:
        return statusline.show(args, raw)
    except Exception:
        log_error()
        return 0


def main(argv):
    utf8_output()
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    if argv[1] in ("-V", "--version"):
        print(__version__)
        return 0
    if argv[1] in ("status", "doctor", "history", "lightning", "purge"):
        return importlib.import_module(f"contextrail.{argv[1]}").main(argv[2:])
    if argv[1] == "statusline":
        return run_statusline(argv[2:])
    if argv[1] not in handlers.HOOKS:
        print(f"cr.py: unknown command {argv[1]!r}", file=sys.stderr)
        return 2
    event, handler = handlers.HOOKS[argv[1]]
    try:
        try:
            # Bytes, not the platform's text encoding: Claude Code sends UTF-8 on Windows too.
            payload = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
        except ValueError:
            log_error()
            payload = None
        # Here, not in the handlers: the doctor runs those to see what a session would get.
        if handlers.unattended(payload if isinstance(payload, dict) else {}):
            return 0
        handlers.heartbeat(event)
        if event == "SessionStart":
            from contextrail import purge, statusline, store
            store.secure_home()  # data written by ContextRail 0.6 or earlier was readable by other users
            purge.retention()
            statusline.note_plugin_root()
        output = handler(payload) if isinstance(payload, dict) else None
    except Exception:
        log_error()
        return 0
    if output:
        sys.stdout.write(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
