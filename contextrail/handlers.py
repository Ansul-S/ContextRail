"""Hook handlers. Each takes the hook's JSON payload and returns the JSON
object to print, or None to stay silent.

docs/lifecycle.md describes the states a session goes through and how each
handler moves it on. Every handler is safe to run twice for the same event."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import __version__, config, facts, handoff, prompts, store
from .config import tilde
from .usage import Usage, await_reply, claude_version, last_assistant_text, last_usage, session_start

Payload = Dict[str, Any]
Output = Optional[Dict[str, Any]]

WARN_EVERY_TOKENS = 30_000
# The reply that ends a turn can add this much to the context read from the transcript before it
# is written (usage.await_reply): closer than this below the next request, the Stop hook waits for it.
REPLY_MARGIN = 30_000
# Claude Code stops a SessionEnd hook after about 1.5 s (measured; the variable
# below changes it). A checkpoint that can't be written well within that is skipped.
SESSION_END_SECONDS = 1.5
SESSION_END_MARGIN = 0.6
# Stop payloads list in-flight background work; a task without a status counts too.
IN_FLIGHT = ("running", "pending")
# Task types (Claude Code's labels) that are watchers or its own chores, not work whose results belong
# in a handoff: artifact live updates and Monitor streams are "monitor". They can run for good, so
# nothing waits for them. mods/contextrail-continue skips the same types; a test keeps them in step.
PASSIVE = ("monitor", "dream", "auto-mode scan", "memory import")
MAX_TASK_CHARS = 60
HEARTBEAT = "heartbeat.json"
HEARTBEAT_EVERY = 60
# Claude Code moves hook context over 10,000 characters (UTF-16 code units, measured) to a file
# and shows the model a 2 KB preview, so a longer handoff is loaded shortened. Stay a little under.
MAX_CONTEXT = 9_800
# A reply that waits for the user's answer: its last line ends with a question mark, maybe inside
# closing markdown, quotes or brackets. mods/contextrail-continue holds its offer on the same test.
QUESTION_END_RE = re.compile(r"""\?[*_`'"’”)\]\s]*$""")
# Claude Code 2.1.163 added Stop feedback, hookSpecificOutput.additionalContext, which continues the
# turn like a block but isn't labelled "Stop hook error". Older versions only continue a blocked stop.
FEEDBACK_SINCE = (2, 1, 163)
# A second SessionStart of the same kind in the same session this soon after a load is the same event
# delivered twice (two copies of the plugin, a retried hook), and loads nothing.
RELOAD_WINDOW = 60
# An API error this soon after a request was the request's turn failing: it doesn't count as an attempt.
FAILURE_WINDOW = 15 * 60
META_FORMAT = 2


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


def text_field(payload: Payload, key: str) -> Optional[str]:
    """A payload field that should be text: the text, or None when it is missing or of another type."""
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _transcript(payload: Payload) -> Optional[str]:
    return text_field(payload, "transcript_path")


def _session_id(payload: Payload) -> str:
    return text_field(payload, "session_id") or ""


def waits_for_answer(message: Optional[str]) -> bool:
    """Whether the agent ended its turn with a question to the user."""
    lines = [line for line in (message or "").splitlines() if line.strip()]
    return bool(lines) and QUESTION_END_RE.search(lines[-1]) is not None


def unattended(payload: Payload) -> bool:
    """Whether the hooks should do nothing in this session: no request, no load, no checkpoint.
    Nobody is there to /clear, and a handoff from a scripted run would replace the person's."""
    if config.attended():
        return False  # the common case, decided without reading any settings
    return not _open(payload)[2]["unattended"]


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
    root = facts.project_root(text_field(payload, "cwd") or os.getcwd())
    project = store.open_project(root)
    return root, project, config.load(project.dir)


def _start(entry: Dict[str, Any], payload: Payload) -> int:
    """The session's context at its start, kept in its state entry once final; 0 until known."""
    if isinstance(entry["start_tokens"], int):
        return entry["start_tokens"]
    start, final = session_start(_transcript(payload))
    if final:
        entry["start_tokens"] = start
    return start or 0


def feedback_supported(payload: Payload, cfg: Dict[str, Any]) -> bool:
    """Whether a request goes to the agent as Stop feedback rather than as a blocked stop."""
    style = cfg["request_style"]
    if style != "auto":
        return style == "feedback"
    version = claude_version(_transcript(payload))
    return version is not None and version >= FEEDBACK_SINCE


def _continue(payload: Payload, cfg: Dict[str, Any], text: str, note: str) -> Dict[str, Any]:
    """Output that keeps the turn going with text for the agent and a note for the person."""
    if feedback_supported(payload, cfg):
        return {"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": text}, "systemMessage": note}
    return {"decision": "block", "reason": text, "systemMessage": note}


def _record(payload: Payload, root: Path) -> Tuple[Optional[facts.GitState], facts.Activity]:
    """The facts ContextRail records itself: git state and what the session did."""
    return facts.git_state(root), facts.session_activity(_transcript(payload), root)


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
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    """Write the handoff, with the git state and session activity recorded for it,
    and return its resume prompt."""
    git, activity = recorded
    saved_at = _now_iso()
    usage_line = usage.describe() if usage else "unknown context usage"
    document = handoff.render(root.name, saved_at, usage_line, git, activity, body, notes)
    version = claude_version(_transcript(payload))
    meta = {
        "format": META_FORMAT,
        "version": __version__,
        "claude_code": ".".join(map(str, version)) if version else None,
        "saved_at": saved_at,
        "narrative_saved_at": narrative_saved_at or saved_at,
        "session_id": _session_id(payload) or None,
        "tokens": usage.tokens if usage else None,
        "branch": git.branch if git else None,
        "head": git.head if git else None,
        "worktree_of": git.worktree_of if git and git.worktree_of else None,
        "dirty": git.dirty if git else [],
        "diff_hash": git.diff_hash if git else None,
        "file_hashes": facts.uncommitted_hashes(root) if git and git.head else None,
        # Outside git, the files the session edited are the only ones a later drift check can compare.
        "edited_hashes": facts.edited_hashes(root, activity.edited) if git is None else None,
        "edited": activity.edited,
        "checks": [run.to_json() for run in activity.checks],
        "body_sha256": store.sha256(body),
        "items": handoff.structured(body),
        **(extra or {}),
    }
    resume = handoff.resume_prompt(body)
    store.save_handoff(project, document, meta, resume, cfg["history_keep"])
    return resume


def on_stop(payload: Payload) -> Output:
    root, project, cfg = _open(payload)
    if not cfg["enabled"]:
        return None
    transcript = _transcript(payload)
    message = text_field(payload, "last_assistant_message")
    if message is None:
        message = last_assistant_text(transcript)
    body = handoff.extract(message)
    with store.locked(project):
        state = store.load_state(project)
        entry = store.session(state, _session_id(payload))
        entry["updated"] = time.time()
        if body is not None:
            output = _receive(payload, root, project, cfg, entry, body)
        else:
            output = _maybe_request(payload, cfg, entry, message)
        store.save_state(project, state)
    return output


def _already_saved(project: store.Project, payload: Payload, entry: Dict[str, Any], body: str, usage: Optional[Usage]) -> bool:
    """Whether the current handoff was saved from this very Stop event: same session, same body,
    same context size, and no request since. A repeated delivery of the event then saves nothing."""
    meta = store.read_json(project.meta)
    return (
        entry["asked_kind"] is None and meta.get("body_sha256") == store.sha256(body)
        and meta.get("session_id") == (_session_id(payload) or None)
        and meta.get("tokens") == (usage.tokens if usage else None)
    )


def _receive(
    payload: Payload, root: Path, project: store.Project, cfg: Dict[str, Any], entry: Dict[str, Any], body: str
) -> Output:
    usage = last_usage(_transcript(payload), cfg["window_tokens"])
    if _already_saved(project, payload, entry, body, usage):
        return None
    recorded = _record(payload, root)
    git, activity = recorded
    checked = activity if cfg["content_checks"] else None
    issues = handoff.problems(body, cfg["max_chars"], checked, git.dirty if git else [])
    if issues and entry["retries"] < 1:
        entry["retries"] += 1
        return _continue(payload, cfg, prompts.fix(issues), "ContextRail: the handoff is incomplete; asking for a fix.")
    notes = [f"Saved with problems: {'; '.join(issues)}."] if issues else []
    limits = config.thresholds(cfg, usage.window, _start(entry, payload)) if usage else None
    asked = entry["asked_kind"]
    kind = asked
    if kind is None:  # a handoff nobody asked for counts as final from the hard point on
        kind = "final" if limits and usage and usage.tokens >= limits.hard else "checkpoint"
    trigger = entry["trigger"] if asked and isinstance(entry["trigger"], dict) else {"rule": "written without a request"}
    resume = _save(project, root, cfg, payload, usage, recorded, body, notes, extra={"kind": kind, "trigger": trigger})
    entry.update(
        attempts=0, retries=0, saved_tokens=usage.tokens if usage else 0, warned_tokens=None, gave_up=False,
        asked_kind=None, saved_kind=kind, question_notice=False, deferred_notice=False, trigger=None,
        # The next session starts from a requested final handoff: once these tasks finish, ask for it again.
        saved_running=asked == "final" and bool(_in_flight(payload.get("background_tasks"))),
    )
    if config.splits(cfg) and kind == "checkpoint":
        later = f" at {limits.hard:,} tokens" if limits else " at the wrap-up point"
        message = (
            f"ContextRail: checkpoint handoff saved to {tilde(project.handoff)}. Keep working: the final "
            f"handoff and the offer of a new session come{later}. Type /clear to continue from this one sooner."
        )
    else:
        message = (
            f"ContextRail: handoff saved to {tilde(project.handoff)}. Type /clear to continue in a "
            "fresh session; the handoff loads automatically."
        )
    if resume and cfg["copy_resume_prompt"] and copy_to_clipboard(resume):
        message += " The resume prompt is on your clipboard."
    if issues:
        message += f" Saved with problems: {'; '.join(issues)}."
    return {"systemMessage": message}


def _maybe_request(payload: Payload, cfg: Dict[str, Any], entry: Dict[str, Any], message: str) -> Output:
    if payload.get("stop_hook_active"):
        return None  # never chain requests; the next turn will ask again
    transcript = _transcript(payload)
    usage = last_usage(transcript, cfg["window_tokens"])
    if usage is None:
        return None
    start = _start(entry, payload)
    limits = config.thresholds(cfg, usage.window, start)
    split = config.splits(cfg)
    running = _in_flight(payload.get("background_tasks"))
    saved = entry["saved_tokens"]
    due = config.next_request(cfg, limits, saved, entry["saved_kind"])
    if due - REPLY_MARGIN <= usage.tokens < due and await_reply(transcript, message):
        usage = last_usage(transcript, cfg["window_tokens"]) or usage
    # The final handoff was saved while background tasks ran; now they have finished, so the
    # next session gets their results. contextrail-continue holds its offer until then.
    finished = bool(entry["saved_running"]) and not running
    if finished:
        kind = "final"
    else:
        if usage.tokens < limits.soft:
            return None
        kind = "checkpoint" if split and usage.tokens < limits.hard else "final"
        # Past the hard point, a checkpoint saved since doesn't stand in for the final handoff.
        final_due = split and kind == "final" and entry["saved_kind"] == "checkpoint"
        if saved is not None and usage.tokens < saved + cfg["refresh_tokens"] and not final_due:
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
    why = config.explain(cfg, limits, usage.window, usage.window_source, start)
    if running and usage.tokens < limits.hard:
        # Their results would change what the handoff says, and finishing wakes the session,
        # whose next stop asks. The hard limit caps the wait.
        if entry["deferred_notice"]:
            return None
        entry["deferred_notice"] = True
        return {"systemMessage": "\n".join([_waiting(running, limits.hard), *why])}
    if waits_for_answer(message) and usage.tokens < limits.hard:
        # A request now would put a handoff over the question, and in Lightning Mode a new session;
        # the stop after the answer asks. The hard limit caps the wait.
        if entry["question_notice"]:
            return None
        entry["question_notice"] = True
        return {
            "systemMessage": (
                f"ContextRail: context is at {usage.tokens:,} tokens; the handoff request waits until "
                "you have answered the question above."
            )
        }
    entry["attempts"] += 1
    entry["retries"] = 0
    entry["asked_kind"] = kind
    entry["trigger"] = {
        "kind": kind if split else "final", "tokens": usage.tokens, "handoff_at": limits.soft, "wrap_up_at": limits.hard,
        "start": start, "window": usage.window, "window_source": usage.window_source,
        "because": "background tasks finished" if finished else why[0], "time": time.time(),
    }
    entry.update(saved_running=False, question_notice=False, deferred_notice=False)
    text = prompts.request(
        usage.describe(), cfg["max_chars"], refresh=saved is not None and not finished, running=_labels(running),
        kind=kind if split else "", finished=finished, scheduled=_scheduled(payload.get("session_crons")),
    )
    note = "\n".join([_asking(usage.tokens, kind if split else "", limits, saved, running, finished), *why])
    return _continue(payload, cfg, text, note)


def _asking(
    tokens: int, kind: str, limits: config.Thresholds, saved: Optional[int], running: List[Dict[str, Any]],
    finished: bool = False,
) -> str:
    """What the request is for, and what happens after it, for the person."""
    if finished:
        return (
            f"ContextRail: the background tasks that ran when the handoff was saved have finished; "
            f"asking for the handoff again with their results. Context is at {tokens:,} tokens."
        )
    if kind == "checkpoint":
        text = (
            f"ContextRail: context is at {tokens:,} tokens; asking for a checkpoint handoff. Work goes on after it; "
            f"the final handoff and the offer of a new session come at {limits.hard:,}."
        )
    elif kind == "final":
        text = (
            f"ContextRail: context is at {tokens:,} tokens, past the wrap-up point; asking for the final handoff. "
            "contextrail-continue, if installed, offers a new session after it"
            + (", once the background tasks finish." if running else ".")
        )
    else:
        text = f"ContextRail: context is at {tokens:,} tokens; asking for a handoff."
    if saved is not None:
        text += f" The last handoff was saved at {saved:,}."
    if running:
        text += f" Still running: {_labels(running)}; nothing is stopped."
    return text


def _in_flight(tasks: Any) -> List[Dict[str, Any]]:
    if not isinstance(tasks, list):
        return []
    return [
        task for task in tasks
        if isinstance(task, dict) and str(task.get("status") or "running").lower() in IN_FLIGHT
        and str(task.get("type") or "") not in PASSIVE
    ]


def _short(text: Any) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= MAX_TASK_CHARS else text[: MAX_TASK_CHARS - 1] + "…"


def _labels(running: List[Dict[str, Any]]) -> str:
    """The first three tasks by their descriptions, and how many more; "" for none."""
    labels = [f"`{_short(task.get('description') or task.get('type') or 'task')}`" for task in running[:3]]
    more = f" and {len(running) - 3} more" if len(running) > 3 else ""
    return f"{', '.join(labels)}{more}"


def _scheduled(crons: Any) -> str:
    """The session's scheduled tasks by their prompts and schedules, as _labels does tasks; "" for none."""
    if not isinstance(crons, list):
        return ""
    crons = [cron for cron in crons if isinstance(cron, dict)]
    labels = []
    for cron in crons[:3]:
        how = "repeating" if cron.get("recurring") is not False else "once"
        labels.append(f"`{_short(cron.get('prompt') or 'a prompt')}` ({how}, `{_short(cron.get('schedule') or '?')}`)")
    more = f" and {len(crons) - 3} more" if len(crons) > 3 else ""
    return f"{', '.join(labels)}{more}"


def _waiting(running: List[Dict[str, Any]], hard: int) -> str:
    count = f"{len(running)} background task{'' if len(running) == 1 else 's'}"
    return (
        f"ContextRail: waiting for {count} ({_labels(running)}) to finish before asking for a "
        f"handoff, at most until {hard:,} tokens. Run /contextrail:handoff to write one now."
    )


def on_stop_failure(payload: Payload) -> Output:
    """The turn ended on an API error (rate limit, overload, ...) instead of a reply. Nothing was
    saved, so the current handoff stays as it was. If the turn was answering a request, that
    request doesn't count against max_attempts. Claude Code ignores this hook's output."""
    root, project, cfg = _open(payload)
    session_id = _session_id(payload)
    if not cfg["enabled"] or not session_id:
        return None
    with store.locked(project):
        state = store.load_state(project)
        if session_id not in state["sessions"]:
            return None
        entry = store.session(state, session_id)
        now = time.time()
        trigger = entry["trigger"] if isinstance(entry["trigger"], dict) else {}
        asked_at = trigger.get("time")
        recent = isinstance(asked_at, (int, float)) and 0 <= now - asked_at < FAILURE_WINDOW
        if entry["asked_kind"] is not None and entry["attempts"] > 0 and recent:
            entry["attempts"] -= 1
            entry["gave_up"] = False
        entry["failure"] = {"error": _short(payload.get("error") or "unknown"), "time": now}
        entry["updated"] = now
        store.save_state(project, state)
    return None


def on_post_tool(payload: Payload) -> Output:
    if payload.get("agent_id"):
        return None  # a subagent's tool call; its context is not the main one
    root, project, cfg = _open(payload)
    if not cfg["enabled"]:
        return None
    usage = last_usage(_transcript(payload), cfg["window_tokens"])
    # Below the limit counted from zero, it is below it from any start; this runs on every tool call.
    if usage is None or usage.tokens < config.thresholds(cfg, usage.window).hard:
        return None
    with store.locked(project):
        state = store.load_state(project)
        entry = store.session(state, _session_id(payload))
        if usage.tokens < config.thresholds(cfg, usage.window, _start(entry, payload)).hard:
            return None
        saved, warned = entry["saved_tokens"], entry["warned_tokens"]
        final_due = config.splits(cfg) and entry["saved_kind"] == "checkpoint"
        if saved is not None and usage.tokens < saved + cfg["refresh_tokens"] and not final_due:
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
    usage = last_usage(_transcript(payload), cfg["window_tokens"])
    with store.locked(project):
        state = store.load_state(project)
        entry = store.session(state, _session_id(payload))
        saved = entry["saved_tokens"]
        fresh = saved is not None and usage is not None and usage.tokens < saved + cfg["refresh_tokens"]
        # Context starts small again after compaction, so the next request is due at the soft limit,
        # counted from the first reply after it.
        entry.update(
            attempts=0, retries=0, saved_tokens=None, warned_tokens=None, gave_up=False, start_tokens=None,
            asked_kind=None, saved_kind=None, saved_running=False, question_notice=False, trigger=None,
        )
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
    previous, previous_meta, _ = store.read_handoff(project)
    narrative_saved_at = previous_meta.get("narrative_saved_at") if previous else None
    body = handoff.fallback_body(handoff.narrative_of(previous or ""), narrative_saved_at, when)
    extra = {"kind": "facts only", "trigger": {"rule": when}}
    _save(project, root, cfg, payload, usage, recorded, body, [note], narrative_saved_at, extra=extra)


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
    usage = last_usage(_transcript(payload), cfg["window_tokens"])
    if usage is None or usage.tokens < cfg["checkpoint_tokens"]:
        return None
    with store.locked(project):
        state = store.load_state(project)
        entry = store.session(state, _session_id(payload))
        saved = entry["saved_tokens"]
        if usage.tokens < _start(entry, payload) + cfg["checkpoint_tokens"]:
            return None
        if saved is not None and usage.tokens < saved + cfg["refresh_tokens"]:
            return None
        recorded = _record(payload, root)
        if not recorded[1].edited or time.monotonic() > _session_end_deadline(start):
            return None
        note = f"Session ended (`{text_field(payload, 'reason') or 'other'}`) without a handoff."
        _checkpoint(project, root, cfg, payload, usage, recorded, note, "when the session ended")
        entry.update(saved_tokens=usage.tokens, updated=time.time())
        store.save_state(project, state)
    return None


def load_context(project: store.Project, root: Path, cfg: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    """What a session start would load: the hook output, or None and why there is nothing. Reads
    only; on_session_start and the doctor both use it."""
    document, meta, problems = store.read_handoff(project)
    if document is None:
        return None, problems[0] if problems else "no handoff is saved"
    saved_at = str(meta.get("saved_at") or "")
    seconds = age_seconds(saved_at)
    if seconds is not None and seconds > cfg["max_age_days"] * 86_400:
        return None, f"the handoff is older than max_age_days ({cfg['max_age_days']})"
    if problems:
        found = facts.Drift([f"Changes since the handoff can't be checked: {problems[0]}."], [], [], False)
    else:
        found = facts.drift(meta, root)
    narrative = handoff.narrative_of(document)
    flagged = handoff.mentions(narrative, found.changed + [old for old, _ in found.renamed])
    checks = meta.get("checks") if isinstance(meta.get("checks"), list) else []
    stale = bool(checks) and (bool(found.changed) or not found.verified)
    options = {"approvals": handoff.approvals(narrative), "stale_checks": stale, "unverified": not found.verified}
    document = handoff.neutralize(document)
    age = describe_age(seconds)
    context = prompts.loaded(saved_at, age, found.notes, flagged, document, **options)
    shortened = handoff.units(context) > MAX_CONTEXT
    if shortened:
        full = tilde(project.handoff)
        room = MAX_CONTEXT - handoff.units(prompts.loaded(saved_at, age, found.notes, flagged, "", full, **options))
        context = prompts.loaded(saved_at, age, found.notes, flagged, handoff.shorten(document, room), full, **options)
    if not found.verified:
        changes = "changes since couldn't be fully checked"
    elif found.changed:
        changes = f"{len(found.changed)} file(s) changed since"
    else:
        changes = f"{len(found.notes)} repo change(s) since" if found.notes else "no repo changes since"
    output = {
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context},
        "systemMessage": (
            f"ContextRail: loaded the handoff saved {age} ({changes})"
            + (", shortened to fit Claude Code's limit for hook context." if shortened else ".")
        ),
    }
    return output, ""


def on_session_start(payload: Payload) -> Output:
    root, project, cfg = _open(payload)
    source = text_field(payload, "source") or "startup"
    if not cfg["enabled"] or source not in cfg["inject_on"] or not project.handoff.exists():
        return None
    session_id = _session_id(payload)
    with store.locked(project):
        output, why = load_context(project, root, cfg)
        if output is None:
            if "belongs to" in why or "another project" in why:
                return {"systemMessage": f"ContextRail: didn't load the saved handoff: {why}. Run /contextrail:doctor."}
            return None
        if session_id:
            sha = store.sha256(project.handoff.read_text(encoding="utf-8"))
            state = store.load_state(project)
            entry = store.session(state, session_id)
            now = time.time()
            loaded_at = entry["loaded_at"]
            again = (
                entry["loaded_sha"] == f"{source}:{sha}" and isinstance(loaded_at, (int, float))
                and 0 <= now - loaded_at < RELOAD_WINDOW
            )
            if again:
                return None
            entry.update(loaded_sha=f"{source}:{sha}", loaded_at=now, updated=now)
            store.save_state(project, state)
    return output


# cr.py command -> (hook event, handler)
HOOKS = {
    "stop": ("Stop", on_stop),
    "stop-failure": ("StopFailure", on_stop_failure),
    "post-tool": ("PostToolUse", on_post_tool),
    "pre-compact": ("PreCompact", on_pre_compact),
    "session-start": ("SessionStart", on_session_start),
    "session-end": ("SessionEnd", on_session_end),
}
