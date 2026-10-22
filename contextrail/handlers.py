"""Hook handlers. Each takes the hook's JSON payload and returns the JSON
object to print, or None to stay silent."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import __version__, config, facts, handoff, prompts, store
from .config import tilde
from .usage import Usage, last_assistant_text, last_usage

Payload = Dict[str, Any]
Output = Optional[Dict[str, Any]]

WARN_EVERY_TOKENS = 30_000
# Claude Code stops a SessionEnd hook after about 1.5 s (measured; the variable
# below changes it). A checkpoint that can't be written well within that is skipped.
SESSION_END_SECONDS = 1.5
SESSION_END_MARGIN = 0.6
# Stop payloads list in-flight background work; a task without a status counts too.
IN_FLIGHT = ("running", "pending")
MAX_TASK_CHARS = 60
HEARTBEAT = "heartbeat.json"
HEARTBEAT_EVERY = 60


def heartbeat(event: str) -> None:
    """Note when each hook last ran, at most once a minute, for /contextrail:doctor."""
    path = config.home() / HEARTBEAT
    beats = store.read_json(path)
    now = time.time()
    last = beats.get(event)
    if isinstance(last, (int, float)) and 0 <= now - last < HEARTBEAT_EVERY:
        return
    beats[event] = round(now)
    try:
        store.write_atomic(path, json.dumps(beats, indent=2))
    except OSError:
        pass


def _now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def age_seconds(saved_at: str) -> Optional[float]:
    try:
        then = dt.datetime.fromisoformat(saved_at)
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        then = then.astimezone()
    return max(0.0, (dt.datetime.now().astimezone() - then).total_seconds())


def describe_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "at an unknown time"
    if seconds < 90:
        return "just now"
    for size, unit in ((86_400, "day"), (3_600, "hour"), (60, "minute")):
        if seconds >= size:
            count = int(seconds // size)
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    return "just now"


def copy_to_clipboard(text: str) -> bool:
    if sys.platform == "darwin":
        candidates = [["pbcopy"]]
    elif sys.platform.startswith("win"):
        candidates = [["clip"]]
    else:
        candidates = [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
    for command in candidates:
        if shutil.which(command[0]) is None:
            continue
        try:
            # No pipes on stdout/stderr: xclip keeps them open after it forks.
            subprocess.run(
                command, input=text, text=True, timeout=3, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


def _open(payload: Payload) -> Tuple[Path, store.Project, Dict[str, Any]]:
    root = facts.project_root(str(payload.get("cwd") or os.getcwd()))
    project = store.Project(root)
    return root, project, config.load(project.dir)


def _record(payload: Payload, root: Path) -> Tuple[Optional[facts.GitState], facts.Activity]:
    """The facts ContextRail records itself: git state and what the session did."""
    return facts.git_state(root), facts.session_activity(payload.get("transcript_path"), root)


def _save(
    project: store.Project,
    root: Path,
    cfg: Dict[str, Any],
    payload: Payload,
    usage: Optional[Usage],
    recorded: Tuple[Optional[facts.GitState], facts.Activity],
    body: str,
    notes: List[str],
    narrative_saved_at: Optional[str] = None,
) -> str:
    """Write the handoff, with the git state and session activity recorded for it,
    and return its resume prompt."""
    git, activity = recorded
    saved_at = _now_iso()
    usage_line = usage.describe() if usage else "unknown context usage"
    document = handoff.render(root.name, saved_at, usage_line, git, activity, body, notes)
    meta = {
        "version": __version__,
        "saved_at": saved_at,
        "narrative_saved_at": narrative_saved_at or saved_at,
        "session_id": payload.get("session_id"),
        "tokens": usage.tokens if usage else None,
        "branch": git.branch if git else None,
        "head": git.head if git else None,
        "dirty": git.dirty if git else [],
        "diff_hash": git.diff_hash if git else None,
        "file_hashes": facts.uncommitted_hashes(root) if git and git.head else None,
    }
    resume = handoff.resume_prompt(body)
    store.save_handoff(project, document, meta, resume, cfg["history_keep"])
    return resume


def on_stop(payload: Payload) -> Output:
    root, project, cfg = _open(payload)
    if not cfg["enabled"]:
        return None
    transcript = payload.get("transcript_path")
    message = payload.get("last_assistant_message")
    if not isinstance(message, str):
        message = last_assistant_text(transcript)
    body = handoff.extract(message)
    with store.locked(project):
        state = store.load_state(project)
        entry = store.session(state, str(payload.get("session_id") or ""))
        entry["updated"] = time.time()
        if body is not None:
            output = _receive(payload, root, project, cfg, entry, body)
        else:
            output = _maybe_request(payload, cfg, entry)
        store.save_state(project, state)
    return output


def _receive(
    payload: Payload, root: Path, project: store.Project, cfg: Dict[str, Any], entry: Dict[str, Any], body: str
) -> Output:
    recorded = _record(payload, root)
    git, activity = recorded
    checked = activity if cfg["content_checks"] else None
    issues = handoff.problems(body, cfg["max_chars"], checked, git.dirty if git else [])
    if issues and entry["retries"] < 1:
        entry["retries"] += 1
        return {
            "decision": "block",
            "reason": prompts.fix(issues),
            "systemMessage": "ContextRail: the handoff is incomplete; asking for a fix.",
        }
    usage = last_usage(payload.get("transcript_path"), cfg["window_tokens"])
    notes = [f"Saved with problems: {'; '.join(issues)}."] if issues else []
    resume = _save(project, root, cfg, payload, usage, recorded, body, notes)
    entry.update(
        attempts=0, retries=0, saved_tokens=usage.tokens if usage else 0, warned_tokens=None, gave_up=False
    )
    message = (
        f"ContextRail: handoff saved to {tilde(project.handoff)}. Type /clear to continue in a "
        "fresh session; the handoff loads automatically."
    )
    if resume and cfg["copy_resume_prompt"] and copy_to_clipboard(resume):
        message += " The resume prompt is on your clipboard."
    if issues:
        message += f" Saved with problems: {'; '.join(issues)}."
    return {"systemMessage": message}


def _maybe_request(payload: Payload, cfg: Dict[str, Any], entry: Dict[str, Any]) -> Output:
    if payload.get("stop_hook_active"):
        return None  # never chain requests; the next turn will ask again
    usage = last_usage(payload.get("transcript_path"), cfg["window_tokens"])
    if usage is None:
        return None
    limits = config.thresholds(cfg, usage.window)
    saved = entry["saved_tokens"]
    if usage.tokens < limits.soft or (saved is not None and usage.tokens < saved + cfg["refresh_tokens"]):
        return None
    if entry["attempts"] >= cfg["max_attempts"]:
        if entry["gave_up"]:
            return None
        entry["gave_up"] = True
        return {
            "systemMessage": (
                f"ContextRail: asked {entry['attempts']} times for a handoff and none was written. "
                "Run /contextrail:handoff when you are ready."
            )
        }
    running = _in_flight(payload.get("background_tasks"))
    if running and usage.tokens < limits.hard:
        # Their results would change what the handoff says, and finishing wakes the session,
        # whose next stop asks. The hard limit caps the wait.
        if entry["deferred_notice"]:
            return None
        entry["deferred_notice"] = True
        return {"systemMessage": _waiting(running, limits.hard)}
    entry["attempts"] += 1
    entry["retries"] = 0
    return {
        "decision": "block",
        "reason": prompts.request(usage.describe(), cfg["max_chars"], refresh=saved is not None),
        "systemMessage": f"ContextRail: context is at {usage.tokens:,} tokens; asking for a handoff.",
    }


def _in_flight(tasks: Any) -> List[Dict[str, Any]]:
    if not isinstance(tasks, list):
        return []
    return [
        task for task in tasks
        if isinstance(task, dict) and str(task.get("status") or "running").lower() in IN_FLIGHT
    ]


def _waiting(running: List[Dict[str, Any]], hard: int) -> str:
    labels = []
    for task in running[:3]:
        text = " ".join(str(task.get("description") or task.get("type") or "task").split())
        labels.append(f"`{text if len(text) <= MAX_TASK_CHARS else text[: MAX_TASK_CHARS - 1] + '…'}`")
    more = f" and {len(running) - 3} more" if len(running) > 3 else ""
    count = f"{len(running)} background task{'' if len(running) == 1 else 's'}"
    return (
        f"ContextRail: waiting for {count} ({', '.join(labels)}{more}) to finish before asking for a "
        f"handoff, at most until {hard:,} tokens. Run /contextrail:handoff to write one now."
    )


def on_post_tool(payload: Payload) -> Output:
    if payload.get("agent_id"):
        return None  # a subagent's tool call; its context is not the main one
    root, project, cfg = _open(payload)
    if not cfg["enabled"]:
        return None
    usage = last_usage(payload.get("transcript_path"), cfg["window_tokens"])
    if usage is None or usage.tokens < config.thresholds(cfg, usage.window).hard:
        return None
    with store.locked(project):
        state = store.load_state(project)
        entry = store.session(state, str(payload.get("session_id") or ""))
        saved, warned = entry["saved_tokens"], entry["warned_tokens"]
        if saved is not None and usage.tokens < saved + cfg["refresh_tokens"]:
            return None
        if warned is not None and usage.tokens < warned + WARN_EVERY_TOKENS:
            return None
        entry["warned_tokens"] = usage.tokens
        entry["updated"] = time.time()
        store.save_state(project, state)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": prompts.wrap_up(usage.describe()),
        }
    }


def on_pre_compact(payload: Payload) -> Output:
    root, project, cfg = _open(payload)
    if not cfg["enabled"]:
        return None
    usage = last_usage(payload.get("transcript_path"), cfg["window_tokens"])
    with store.locked(project):
        state = store.load_state(project)
        entry = store.session(state, str(payload.get("session_id") or ""))
        saved = entry["saved_tokens"]
        fresh = saved is not None and usage is not None and usage.tokens < saved + cfg["refresh_tokens"]
        # Context starts small again after compaction, so the next request is due at the soft limit.
        entry.update(attempts=0, retries=0, saved_tokens=None, warned_tokens=None, gave_up=False)
        entry["updated"] = time.time()
        store.save_state(project, state)
        if fresh:
            return None
        kind = "manual" if payload.get("trigger") == "manual" else "automatic"
        note = f"Saved automatically before {kind} compaction, without a summary from the agent."
        _checkpoint(project, root, cfg, payload, usage, _record(payload, root), note, "before compaction")
    return {"systemMessage": "ContextRail: saved the session facts before compaction; they load again afterwards."}


def _checkpoint(
    project: store.Project,
    root: Path,
    cfg: Dict[str, Any],
    payload: Payload,
    usage: Optional[Usage],
    recorded: Tuple[Optional[facts.GitState], facts.Activity],
    note: str,
    when: str,
) -> None:
    """Save the recorded facts without the agent, carrying the last narrative over."""
    previous = project.handoff.read_text(encoding="utf-8") if project.handoff.exists() else ""
    previous_meta = store.read_json(project.meta)
    narrative_saved_at = previous_meta.get("narrative_saved_at") if previous else None
    body = handoff.fallback_body(handoff.narrative_of(previous), narrative_saved_at, when)
    _save(project, root, cfg, payload, usage, recorded, body, [note], narrative_saved_at)


def _session_end_deadline(start: float) -> float:
    try:
        seconds = int(os.environ["CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS"]) / 1000
    except (KeyError, ValueError):
        seconds = SESSION_END_SECONDS
    return start + seconds - SESSION_END_MARGIN


def on_session_end(payload: Payload) -> Output:
    """A session that changed files ended without a recent handoff: save a
    checkpoint of its facts. Crashes and killed terminals don't run this."""
    start = time.monotonic()
    root, project, cfg = _open(payload)
    if not cfg["enabled"]:
        return None
    usage = last_usage(payload.get("transcript_path"), cfg["window_tokens"])
    if usage is None or usage.tokens < cfg["checkpoint_tokens"]:
        return None
    with store.locked(project):
        state = store.load_state(project)
        entry = store.session(state, str(payload.get("session_id") or ""))
        saved = entry["saved_tokens"]
        if saved is not None and usage.tokens < saved + cfg["refresh_tokens"]:
            return None
        recorded = _record(payload, root)
        if not recorded[1].edited or time.monotonic() > _session_end_deadline(start):
            return None
        note = f"Session ended (`{payload.get('reason') or 'other'}`) without a handoff."
        _checkpoint(project, root, cfg, payload, usage, recorded, note, "when the session ended")
        entry.update(saved_tokens=usage.tokens, updated=time.time())
        store.save_state(project, state)
    return None


def on_session_start(payload: Payload) -> Output:
    root, project, cfg = _open(payload)
    source = str(payload.get("source") or "startup")
    if not cfg["enabled"] or source not in cfg["inject_on"] or not project.handoff.exists():
        return None
    meta = store.read_json(project.meta)
    saved_at = str(meta.get("saved_at") or "")
    seconds = age_seconds(saved_at)
    if seconds is not None and seconds > cfg["max_age_days"] * 86_400:
        return None
    try:
        document = project.handoff.read_text(encoding="utf-8")
    except OSError:
        return None
    found = facts.drift(meta, root)
    flagged = handoff.mentions(handoff.narrative_of(document), found.changed)
    age = describe_age(seconds)
    if found.changed:
        changes = f"{len(found.changed)} file(s) changed since"
    else:
        changes = f"{len(found.notes)} repo change(s) since" if found.notes else "no repo changes since"
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": prompts.loaded(saved_at, age, found.notes, flagged, document),
        },
        "systemMessage": f"ContextRail: loaded the handoff saved {age} ({changes}).",
    }


# cr.py command -> (hook event, handler)
HOOKS = {
    "stop": ("Stop", on_stop),
    "post-tool": ("PostToolUse", on_post_tool),
    "pre-compact": ("PreCompact", on_pre_compact),
    "session-start": ("SessionStart", on_session_start),
    "session-end": ("SessionEnd", on_session_end),
}
