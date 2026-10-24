"""Settings: built-in defaults, then ~/.contextrail/config.json, then the
per-project config.json, then CONTEXTRAIL_* environment variables."""

from __future__ import annotations

import difflib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    # Fallbacks for people who set none. Ask for a handoff once the session has grown by
    # soft_tokens since its start (the context at its first reply), or context reaches
    # soft_pct of the window, whichever comes first.
    "soft_tokens": 100_000,
    "soft_pct": 50,
    # The same pair for the final handoff: the agent is told mid-turn to wrap up, and
    # contextrail-continue offers a new session after it. Must be above soft_tokens.
    "hard_tokens": 130_000,
    "hard_pct": 65,
    # Where contextrail-continue offers a new session: "hard", or "soft" to offer it after
    # every handoff. Before the hard point, a handoff is a checkpoint and work goes on.
    "new_session_at": "hard",
    # Context window in tokens. None means detect (see usage.resolve_window).
    "window_tokens": None,
    # After a handoff is saved, ask again once context has grown by this much.
    "refresh_tokens": 40_000,
    # Upper limit for the part of the handoff the agent writes.
    "max_chars": 8_000,
    # Also check the handoff against the session's facts (edited files, checks) before saving.
    "content_checks": True,
    # A session that edited files and ends without a handoff, this many tokens past its start, gets a checkpoint.
    "checkpoint_tokens": 30_000,
    # How many times one session is asked before ContextRail gives up.
    "max_attempts": 3,
    # SessionStart sources that load the handoff into the new session.
    "inject_on": ["startup", "clear", "compact"],
    # Handoffs older than this are not loaded.
    "max_age_days": 30,
    "copy_resume_prompt": True,
    "history_keep": 20,
    # Also run in sessions nobody is watching (claude -p, the Agent SDK). Off by default.
    "unattended": False,
    # Lightning Mode (/contextrail:lightning): hand off once the context reaches this many tokens
    # in all, and contextrail-continue starts a new session from the handoff after a countdown.
    # 0 is off. Global only, so ContextRail and contextrail-continue read the same value.
    "lightning_tokens": 0,
    # How a request reaches the agent: "feedback" continues the turn as hook feedback, "block" blocks
    # the stop (Claude Code labels that "Stop hook error"), "auto" uses feedback where Claude Code
    # supports it (2.1.163 and later, read from the transcript) and block elsewhere.
    "request_style": "auto",
}

# Settings that a per-project config.json can't change.
GLOBAL_ONLY = {"lightning_tokens"}
# In Lightning Mode, a session that starts near the limit still gets this much work.
LIGHTNING_MIN_WORK = 20_000
# Settings that must be above zero, and settings with a fixed set of values.
POSITIVE = {"soft_tokens", "hard_tokens", "soft_pct", "hard_pct"}
CHOICES = {"new_session_at": ("hard", "soft"), "request_style": ("auto", "feedback", "block")}
# hard_tokens when it is not above soft_tokens: unset, or set wrong.
HARD_GAP = DEFAULTS["hard_tokens"] - DEFAULTS["soft_tokens"]

ENV_PREFIX = "CONTEXTRAIL_"
# Environment variables with the prefix that are not settings.
ENV_ONLY = {"CONTEXTRAIL_HOME", "CONTEXTRAIL_DISABLE"}
SESSION_SOURCES = ("startup", "resume", "clear", "compact")


def home() -> Path:
    """Where ContextRail keeps its data (CONTEXTRAIL_HOME, default ~/.contextrail)."""
    override = os.environ.get("CONTEXTRAIL_HOME")
    return Path(override).expanduser() if override else Path.home() / ".contextrail"


def claude_dir() -> Path:
    """Claude Code's own folder, for its settings and transcripts."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def tilde(path: Path) -> str:
    """path with the home folder shown as ~."""
    home_dir = str(Path.home())
    text = str(path)
    return "~" + text[len(home_dir):] if text.startswith(home_dir + os.sep) else text


def _coerce(key: str, value: Any) -> Any:
    """Return value converted to the type of the default, or raise ValueError."""
    default = DEFAULTS[key]
    if key == "window_tokens":
        if value in (None, "", 0, "0", "auto"):
            return None
        number = int(value)
        if number <= 0:
            raise ValueError(key)
        return number
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        raise ValueError(key)
    if isinstance(default, int):
        if isinstance(value, bool):
            raise ValueError(key)
        number = int(value)
        if number < 0 or (key in POSITIVE and number == 0) or (key.endswith("_pct") and number > 100):
            raise ValueError(key)
        return number
    if key in CHOICES:
        text = str(value).strip().lower()
        if text not in CHOICES[key]:
            raise ValueError(key)
        return text
    if isinstance(default, list):
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",") if part.strip()]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(key)
        return value
    return value


def _merge(cfg: Dict[str, Any], updates: Dict[str, Any]) -> List[str]:
    """Apply the valid settings in updates, and return their keys."""
    applied = []
    for key, value in updates.items():
        if key not in DEFAULTS:
            continue
        try:
            cfg[key] = _coerce(key, value)
        except (TypeError, ValueError, OverflowError):
            continue  # a bad value keeps the previous one
        applied.append(key)
    return applied


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _merged(project_dir: Optional[Path]) -> Tuple[Dict[str, Any], List[str]]:
    """The settings from every source in order, and the keys any of them set."""
    cfg = dict(DEFAULTS)
    applied = _merge(cfg, _read_json(home() / "config.json"))
    if project_dir is not None:
        local = _read_json(project_dir / "config.json")
        applied += _merge(cfg, {key: value for key, value in local.items() if key not in GLOBAL_ONLY})
    env = {
        key: os.environ[ENV_PREFIX + key.upper()]
        for key in DEFAULTS
        if ENV_PREFIX + key.upper() in os.environ
    }
    applied += _merge(cfg, env)
    return cfg, applied


def load(project_dir: Optional[Path] = None) -> Dict[str, Any]:
    cfg, _ = _merged(project_dir)
    if cfg["hard_tokens"] <= cfg["soft_tokens"]:
        # Unset, the fallback can sit below a raised soft_tokens; set, it is reported by problems().
        cfg["hard_tokens"] = cfg["soft_tokens"] + HARD_GAP
    if os.environ.get("CONTEXTRAIL_DISABLE", "").strip().lower() in ("1", "true", "yes"):
        cfg["enabled"] = False
    return cfg


def splits(cfg: Dict[str, Any]) -> bool:
    """Whether a handoff before the hard point is a checkpoint, with no new session offered.
    Lightning Mode always offers one at its own point."""
    return cfg["new_session_at"] == "hard" and not cfg["lightning_tokens"]


def next_request(cfg: Dict[str, Any], limits: "Thresholds", saved: Optional[int], saved_kind: Optional[str]) -> int:
    """The context at which the next handoff request is made: the handoff point, or once one is
    saved, that plus refresh_tokens, and after a checkpoint no later than the wrap-up point."""
    if saved is None:
        return limits.soft
    due = saved + cfg["refresh_tokens"]
    if splits(cfg) and saved_kind == "checkpoint":
        due = min(due, max(limits.hard, saved))  # the final handoff is asked for at the wrap-up point
    return due


def attended() -> bool:
    """Whether a person is at the keyboard. Claude Code sets the variable to 1 in interactive
    sessions and to 0 in claude -p and the Agent SDK; without it, a session counts as attended."""
    return os.environ.get("CLAUDE_CODE_SESSION_ATTENDED", "").strip() != "0"


def _invalid(updates: Dict[str, Any], local: bool = False) -> List[Tuple[str, str]]:
    """(key, problem) for each setting that load() ignores or can't use; local for a per-project file."""
    found = []
    for key, value in updates.items():
        if local and key in GLOBAL_ONLY:
            found.append((key, f"works only in {tilde(home() / 'config.json')}"))
            continue
        if key not in DEFAULTS:
            close = difflib.get_close_matches(key, DEFAULTS, n=1)
            found.append((key, "is not a setting" + (f" (did you mean '{close[0]}'?)" if close else "")))
            continue
        try:
            coerced = _coerce(key, value)
        except (TypeError, ValueError, OverflowError):
            found.append((key, f"has an invalid value, {json.dumps(value)}"))
            continue
        if key == "inject_on":
            unknown = [source for source in coerced if source not in SESSION_SOURCES]
            if unknown:
                found.append((key, f"has unknown sources {unknown}; use {', '.join(SESSION_SOURCES)}"))
    return found


def problems(project_dir: Optional[Path] = None) -> List[str]:
    """What load() ignores: unreadable config files, unknown keys and invalid values."""
    found = []
    paths = [home() / "config.json"] + ([project_dir / "config.json"] if project_dir is not None else [])
    for path in paths:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError as error:
            found.append(f"{tilde(path)} can't be read ({error.strerror or error})")
            continue
        except ValueError as error:
            found.append(f"{tilde(path)} is not valid JSON ({error})")
            continue
        if not isinstance(data, dict):
            found.append(f"{tilde(path)} must hold a JSON object")
            continue
        local = path != home() / "config.json"
        found += [f"{tilde(path)}: '{key}' {problem}" for key, problem in _invalid(data, local)]
    env = {
        name[len(ENV_PREFIX):].lower(): value
        for name, value in os.environ.items()
        if name.startswith(ENV_PREFIX) and name not in ENV_ONLY
    }
    found += [f"{ENV_PREFIX}{key.upper()} {problem}" for key, problem in _invalid(env)]
    cfg, applied = _merged(project_dir)
    if "hard_tokens" in applied and cfg["hard_tokens"] <= cfg["soft_tokens"]:
        found.append(
            f"hard_tokens ({cfg['hard_tokens']:,}) must be above soft_tokens ({cfg['soft_tokens']:,}); "
            f"ContextRail uses {cfg['soft_tokens'] + HARD_GAP:,} until it is"
        )
    return found


@dataclass(frozen=True)
class Thresholds:
    soft: int
    hard: int
    # Each point before the window caps, and the caps, for explaining which one decided.
    soft_wanted: int = field(default=0, compare=False)
    hard_wanted: int = field(default=0, compare=False)
    soft_cap: int = field(default=0, compare=False)
    hard_cap: int = field(default=0, compare=False)


def thresholds(cfg: Dict[str, Any], window: int, start: int = 0) -> Thresholds:
    """The context sizes for the handoff request and the wrap-up warning. The
    token settings count from the session's start; the window shares cap them,
    so a small window still hands off before auto-compaction. In Lightning Mode the handoff
    comes at the Lightning size instead, which counts the whole context."""
    lightning = cfg["lightning_tokens"]
    soft_cap, hard_cap = window * cfg["soft_pct"] // 100, window * cfg["hard_pct"] // 100
    if lightning:
        soft_wanted = max(lightning, start + LIGHTNING_MIN_WORK)
        soft = min(soft_wanted, soft_cap)
        hard_wanted = soft + cfg["hard_tokens"] - cfg["soft_tokens"]
    else:
        soft_wanted = start + cfg["soft_tokens"]
        soft = min(soft_wanted, soft_cap)
        hard_wanted = start + cfg["hard_tokens"]
    hard = max(min(hard_wanted, hard_cap), soft)
    return Thresholds(soft, hard, soft_wanted, hard_wanted, soft_cap, hard_cap)


# Where the window size came from (usage.resolve_window), as the diagnostic line says it.
WINDOW_FROM = {
    "config": "the window_tokens setting",
    "statusline": "the status line",
    "model": "the model name",
    "observed": "usage above 200k",
    "assumed": "an assumption",
}


def _point(name: str, value: int, wanted: int, cap: int, pct: int, how: str) -> str:
    if value > min(wanted, cap):
        return f"{name} {value:,} (no earlier than the handoff)"
    if value < wanted:
        return f"{name} {value:,} ({pct}% of the window, before {how} = {wanted:,})"
    return f"{name} {value:,} ({how})"


def explain(cfg: Dict[str, Any], limits: Thresholds, window: int, source: str, start: int) -> List[str]:
    """Why the handoff and wrap-up points are where they are, and a warning when the
    window size is assumed. Numbers and setting names only."""
    if cfg["lightning_tokens"]:
        soft_how = f"lightning_tokens {cfg['lightning_tokens']:,}"
        if limits.soft_wanted > cfg["lightning_tokens"]:
            soft_how = f"start {start:,} + {LIGHTNING_MIN_WORK:,}"
        hard_how = f"handoff + {cfg['hard_tokens'] - cfg['soft_tokens']:,}"
    else:
        soft_how = f"start {start:,} + soft_tokens {cfg['soft_tokens']:,}"
        hard_how = f"start + hard_tokens {cfg['hard_tokens']:,}"
    lines = [
        _point("Handoff at", limits.soft, limits.soft_wanted, limits.soft_cap, cfg["soft_pct"], soft_how)
        + "; "
        + _point("wrap-up at", limits.hard, limits.hard_wanted, limits.hard_cap, cfg["hard_pct"], hard_how)
        + f". Window {window:,} tokens, from {WINDOW_FROM.get(source, source)}."
    ]
    if source == "assumed":
        moved = (
            f" Its {cfg['soft_pct']}% cap puts the handoff at {limits.soft:,} instead of {limits.soft_wanted:,}."
            if limits.soft < limits.soft_wanted
            else f" The {cfg['soft_pct']}% and {cfg['hard_pct']}% caps are shares of this assumed size."
        )
        lines.append(
            f"The window size is assumed: no status-line report and no window_tokens setting.{moved} "
            "Run /contextrail:doctor --setup-statusline, or set window_tokens in ~/.contextrail/config.json."
        )
    return lines
