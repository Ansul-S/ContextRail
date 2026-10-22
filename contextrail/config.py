"""Settings: built-in defaults, then ~/.contextrail/config.json, then the
per-project config.json, then CONTEXTRAIL_* environment variables."""

from __future__ import annotations

import difflib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    # Ask for a handoff once context reaches the smaller of these two.
    "soft_tokens": 100_000,
    "soft_pct": 50,
    # Tell the agent to wrap up mid-turn once context reaches the smaller of these.
    "hard_tokens": 130_000,
    "hard_pct": 65,
    # Context window in tokens. None means detect (see usage.resolve_window).
    "window_tokens": None,
    # After a handoff is saved, ask again once context has grown by this much.
    "refresh_tokens": 40_000,
    # Upper limit for the part of the handoff the agent writes.
    "max_chars": 12_000,
    # How many times one session is asked before ContextRail gives up.
    "max_attempts": 3,
    # SessionStart sources that load the handoff into the new session.
    "inject_on": ["startup", "clear", "compact"],
    # Handoffs older than this are not loaded.
    "max_age_days": 30,
    "copy_resume_prompt": True,
    "history_keep": 20,
}

ENV_PREFIX = "CONTEXTRAIL_"
# Environment variables with the prefix that are not settings.
ENV_ONLY = {"CONTEXTRAIL_HOME", "CONTEXTRAIL_DISABLE"}
SESSION_SOURCES = ("startup", "resume", "clear", "compact")


def home() -> Path:
    """Where ContextRail keeps its data (CONTEXTRAIL_HOME, default ~/.contextrail)."""
    override = os.environ.get("CONTEXTRAIL_HOME")
    return Path(override).expanduser() if override else Path.home() / ".contextrail"


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
        if number < 0 or (key.endswith("_pct") and number > 100):
            raise ValueError(key)
        return number
    if isinstance(default, list):
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",") if part.strip()]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(key)
        return value
    return value


def _merge(cfg: Dict[str, Any], updates: Dict[str, Any]) -> None:
    for key, value in updates.items():
        if key not in DEFAULTS:
            continue
        try:
            cfg[key] = _coerce(key, value)
        except (TypeError, ValueError):
            continue  # a bad value keeps the previous one


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load(project_dir: Optional[Path] = None) -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    _merge(cfg, _read_json(home() / "config.json"))
    if project_dir is not None:
        _merge(cfg, _read_json(project_dir / "config.json"))
    env = {
        key: os.environ[ENV_PREFIX + key.upper()]
        for key in DEFAULTS
        if ENV_PREFIX + key.upper() in os.environ
    }
    _merge(cfg, env)
    if os.environ.get("CONTEXTRAIL_DISABLE", "").strip().lower() in ("1", "true", "yes"):
        cfg["enabled"] = False
    return cfg


def _invalid(updates: Dict[str, Any]) -> List[Tuple[str, str]]:
    """(key, problem) for each setting that load() ignores or can't use."""
    found = []
    for key, value in updates.items():
        if key not in DEFAULTS:
            close = difflib.get_close_matches(key, DEFAULTS, n=1)
            found.append((key, "is not a setting" + (f" (did you mean '{close[0]}'?)" if close else "")))
            continue
        try:
            coerced = _coerce(key, value)
        except (TypeError, ValueError):
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
        found += [f"{tilde(path)}: '{key}' {problem}" for key, problem in _invalid(data)]
    env = {
        name[len(ENV_PREFIX):].lower(): value
        for name, value in os.environ.items()
        if name.startswith(ENV_PREFIX) and name not in ENV_ONLY
    }
    found += [f"{ENV_PREFIX}{key.upper()} {problem}" for key, problem in _invalid(env)]
    return found


@dataclass(frozen=True)
class Thresholds:
    soft: int
    hard: int


def thresholds(cfg: Dict[str, Any], window: int) -> Thresholds:
    soft = min(cfg["soft_tokens"], window * cfg["soft_pct"] // 100)
    hard = min(cfg["hard_tokens"], window * cfg["hard_pct"] // 100)
    return Thresholds(soft=soft, hard=max(hard, soft))
