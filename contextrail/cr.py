#!/usr/bin/env python3
"""ContextRail: automatic session handoff for Claude Code.

Hooks (JSON payload on stdin):  cr.py stop | post-tool | pre-compact | session-start
Manual:                         cr.py status [DIR]
"""

import json
import sys
import time
import traceback
from pathlib import Path

# Import the package from the plugin root, not modules from this folder.
sys.path[0] = str(Path(__file__).resolve().parent.parent)

from contextrail import __version__, config, handlers, status  # noqa: E402

HANDLERS = {
    "stop": handlers.on_stop,
    "post-tool": handlers.on_post_tool,
    "pre-compact": handlers.on_pre_compact,
    "session-start": handlers.on_session_start,
}
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


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    if argv[1] in ("-V", "--version"):
        print(__version__)
        return 0
    if argv[1] == "status":
        return status.main(argv[2:])
    handler = HANDLERS.get(argv[1])
    if handler is None:
        print(f"cr.py: unknown command {argv[1]!r}", file=sys.stderr)
        return 2
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        output = handler(payload) if isinstance(payload, dict) else None
    except Exception:
        log_error()
        return 0
    if output:
        sys.stdout.write(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
