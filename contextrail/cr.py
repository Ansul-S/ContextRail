#!/usr/bin/env python3
"""ContextRail: automatic session handoff for Claude Code.

Hooks (JSON payload on stdin):  cr.py stop | post-tool | pre-compact | session-start
Status line (optional):         cr.py statusline [-- COMMAND]
Manual:                         cr.py status [DIR] | doctor [DIR] [--exit-zero] [--setup-statusline]
                                cr.py history [--restore N] [DIR]
                                cr.py lightning [LIMIT | off]
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
    """Hooks must never break a session: record the error and carry on."""
    try:
        path = config.home() / "error.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > ERROR_LOG_LIMIT:
            path.unlink()
        with open(path, "a", encoding="utf-8") as log:
            log.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(sys.argv[1:])}\n")
            log.write(traceback.format_exc() + "\n")
    except OSError:
        pass


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
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    if argv[1] in ("-V", "--version"):
        print(__version__)
        return 0
    if argv[1] in ("status", "doctor", "history", "lightning"):
        return importlib.import_module(f"contextrail.{argv[1]}").main(argv[2:])
    if argv[1] == "statusline":
        return run_statusline(argv[2:])
    if argv[1] not in handlers.HOOKS:
        print(f"cr.py: unknown command {argv[1]!r}", file=sys.stderr)
        return 2
    event, handler = handlers.HOOKS[argv[1]]
    try:
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            log_error()
            payload = None
        # Here, not in the handlers: the doctor runs those to see what a session would get.
        if handlers.unattended(payload if isinstance(payload, dict) else {}):
            return 0
        handlers.heartbeat(event)
        if event == "SessionStart":
            from contextrail import statusline
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
