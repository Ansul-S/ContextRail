"""`cr.py lightning [LIMIT | off]`: Lightning Mode, a new session at a context size you set.

It writes lightning_tokens to ~/.contextrail/config.json, keeping the other
settings there. ContextRail asks for the handoff at that size, counting the whole
context, and contextrail-continue reads the same setting to count down to a new
session once the handoff is saved.
"""

from __future__ import annotations

import json
import os
import re
from typing import List, Optional

from . import config, store
from .config import tilde
from .statusline import _short

USAGE = "Usage: /contextrail:lightning [LIMIT | off], with LIMIT like 200k, 0.2M or 200000"
SMALLEST = 10_000
LIMIT = re.compile(r"(\d+(?:\.\d+)?)([km]?)")
COUNTDOWN_SECONDS = 10


def parse(text: str) -> Optional[int]:
    """Tokens for 200k, 0.2M, 200000 or 200,000; 0 for off; None for anything else."""
    text = text.strip().lower().replace(",", "").replace("_", "")
    if text in ("off", "0"):
        return 0
    match = LIMIT.fullmatch(text)
    if not match:
        return None
    number, unit = match.groups()
    return round(float(number) * {"": 1, "k": 1_000, "m": 1_000_000}[unit])


def continue_enabled() -> bool:
    """Whether Claude Code's settings enable contextrail-continue, which starts the new session."""
    try:
        settings = json.loads((config.claude_dir() / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    plugins = settings.get("enabledPlugins") if isinstance(settings, dict) else None
    return isinstance(plugins, dict) and any(
        name.split("@")[0] == "contextrail-continue" and on is True for name, on in plugins.items()
    )


def describe(tokens: int) -> str:
    if not tokens:
        return "Lightning Mode is off. Turn it on with a limit, for example /contextrail:lightning 200k."
    lines = [
        f"Lightning Mode is on at {_short(tokens)}. Once the context reaches {tokens:,} tokens, ContextRail "
        f"asks for a handoff, and a new session starts from it after a {COUNTDOWN_SECONDS} s countdown "
        "(press n or Esc, or start typing, to stay).",
        "A session that starts near the limit still gets 20k tokens of work first. "
        "/contextrail:status shows the exact trigger points.",
    ]
    if not continue_enabled():
        lines.append(
            "The countdown needs the contextrail-continue plugin, which isn't enabled. Without it, the "
            "handoff is still saved; type /clear to continue from it. To add it: "
            "claude plugin install contextrail-continue@contextrail"
        )
    return "\n".join(lines)


def main(args: List[str]) -> int:
    """Show the setting, or set it. Bad input exits 2, unless --exit-zero is given, as /contextrail:lightning does."""
    exit_zero = "--exit-zero" in args
    args = [arg for arg in args if arg != "--exit-zero"]
    if not args:
        print(describe(config.load()["lightning_tokens"]))
        return 0
    tokens = parse(" ".join(args)) if len(args) == 1 else None
    if tokens is None or 0 < tokens < SMALLEST:
        print(USAGE if tokens is None else f"ContextRail: {tokens:,} tokens is too small for Lightning; use at least {_short(SMALLEST)}.")
        return 0 if exit_zero else 2
    path = config.home() / "config.json"
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError) as error:
        print(f"ContextRail: {tilde(path)} can't be read ({error}), so it was left as it is. Fix it, then run this again.")
        return 0 if exit_zero else 1
    if not isinstance(current, dict):
        print(f"ContextRail: {tilde(path)} must hold a JSON object, so it was left as it is.")
        return 0 if exit_zero else 1
    current["lightning_tokens"] = tokens
    try:
        store.write_atomic(path, json.dumps(current, indent=2) + "\n")
    except OSError as error:
        print(f"ContextRail: couldn't write {tilde(path)}: {error.strerror or error}")
        return 0 if exit_zero else 1
    print(describe(tokens))
    override = os.environ.get(config.ENV_PREFIX + "LIGHTNING_TOKENS")
    if override is not None:
        print(f"Note: {config.ENV_PREFIX}LIGHTNING_TOKENS={override} is set, and it overrides {tilde(path)}.")
    return 0
