"""Find evaluation cases in Claude Code's transcripts. A case is a session A
that saved a handoff and the session B that loaded it next. Nothing here calls
a model."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from contextrail import handoff, usage
from contextrail.store import claude_slug

Entry = Dict[str, Any]

LOAD_RE = re.compile(r'<contextrail-previous-handoff saved="([^"]+)"')
# Claude Code keeps hook context over its limit in a file and shows the session a preview.
PERSISTED_RE = re.compile(r"Full output saved to: (\S+)")
# A blocked stop's reason reaches the agent as a meta user message with this prefix. Stop feedback
# (Claude Code 2.1.163 and later) is saved as a hook_additional_context attachment instead.
FEEDBACK = "Stop hook feedback:\n"
REQUEST_START = "ContextRail: this session is at "
FIX_START = "ContextRail: the handoff needs fixing"
USAGE_RE = re.compile(r"this session is at (.+?)\. The user set up ContextRail")
CAP_RE = re.compile(r"Stay under ([\d,]+) characters")
WINDOW_RE = re.compile(r"of a (\d+)([kM]) window")
REFRESH = "Context has grown a lot since the last handoff"
# A's reply comes at most this long before the save B loaded, or just after it (the save is
# stamped to the second).
REPLY_BEFORE = 600
REPLY_AFTER = 5
# A B whose transcript changed more recently than this may still be running.
QUIET_SECONDS = 30 * 60
# The handoff loads among a session's first lines, before its first reply.
START_LINES = 400
# User lines that are commands and notices, not messages.
NOT_MESSAGES = ("<command-name>", "<local-command", "<task-notification>")
# Who a message can come from: the user, or a plugin sending the user's resume prompt.
SENDERS = ("human", "plugin")
# The entrypoint of claude -p and Agent SDK sessions.
SCRIPTED = "sdk-cli"
# The share of cases, in percent, set aside for tuning; the rest are held out.
TUNE_SHARE = 30


@dataclass
class Case:
    a: str  # session that saved the handoff
    b: str  # session that loaded it
    b_path: str
    reply_uuid: str  # A's reply with the handoff it saved
    reply_at: str
    fork_uuid: str  # A's last reply before the request; the regenerated handoffs start here
    asked_by: str  # "hook", or "user" for a handoff the user asked for
    usage_line: str
    refresh: bool
    tokens: int  # context at the fork
    cwd: str
    model: str
    effort: Optional[str]
    saved_cap: Optional[int]  # the cap A's own request gave, when it shows
    saved_body: str  # what A wrote, as saved
    loaded: str  # what B loaded, whole
    messages: int  # messages in B

    @property
    def key(self) -> str:
        return f"{self.a[:8]}-{self.reply_uuid[:8]}"

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "Case":
        return cls(**data)


def parse_time(value: Any) -> Optional[dt.datetime]:
    """A transcript timestamp (UTC, ending in Z) or a saved_at with its offset."""
    if not isinstance(value, str):
        return None
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.astimezone()


def _entries(path: Path, wanted: Optional[bytes] = None) -> Iterator[Tuple[int, Entry]]:
    """(line number, entry) for each line, or only for lines holding `wanted`."""
    with open(path, "rb") as f:
        for number, raw in enumerate(f):
            if wanted is not None and wanted not in raw:
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if isinstance(entry, dict):
                yield number, entry


def read(path: Path) -> List[Entry]:
    return [entry for _, entry in _entries(path)]


def text(entry: Entry) -> str:
    """The text blocks of a transcript line's message."""
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text") or "") for block in content if isinstance(block, dict) and block.get("type") == "text"
    )


def main_line(entry: Entry, kind: str) -> bool:
    return entry.get("type") == kind and not entry.get("isSidechain")


def tool_results(entry: Entry) -> bool:
    content = (entry.get("message") or {}).get("content")
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content
    )


def hook_text(entry: Entry) -> str:
    """What a Stop hook sent the agent in this transcript line, either way it can arrive; "" for any
    other line."""
    if main_line(entry, "user") and entry.get("isMeta"):
        sent = text(entry)
        return sent[len(FEEDBACK):] if sent.startswith(FEEDBACK) else ""
    attachment = entry.get("attachment")
    if main_line(entry, "attachment") and isinstance(attachment, dict) and attachment.get("type") == "hook_additional_context" \
            and attachment.get("hookEvent") == "Stop":
        content = attachment.get("content")
        return "\n".join(part for part in content if isinstance(part, str)) if isinstance(content, list) else ""
    return ""


def is_request(entry: Entry) -> bool:
    """ContextRail's handoff request, as the Stop hook delivered it."""
    return hook_text(entry).startswith(REQUEST_START)


def is_fix(entry: Entry) -> bool:
    return hook_text(entry).startswith(FIX_START)


def is_prompt(entry: Entry) -> bool:
    """Anything sent as a turn of its own: a message, a slash command, a notification."""
    if not main_line(entry, "user") or entry.get("isMeta") or tool_results(entry):
        return False
    return bool(text(entry).strip())


def is_message(entry: Entry) -> bool:
    """A message from the user, typed or sent for them by a plugin."""
    if not is_prompt(entry):
        return False
    origin = entry.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in SENDERS:
        return False
    return not text(entry).lstrip().startswith(NOT_MESSAGES)


def has_handoff(entry: Entry) -> bool:
    """Whether a main-thread reply holds a handoff, not just the tag in passing."""
    if not main_line(entry, "assistant"):
        return False
    body = handoff.extract(text(entry))
    return body is not None and "goal" in handoff.sections(body)


def _whole(content: str) -> str:
    """Hook context that Claude Code saved to a file: the file's text, while it exists."""
    if "<persisted-output>" not in content:
        return content
    match = PERSISTED_RE.search(content)
    if match:
        try:
            return Path(match.group(1)).read_text(encoding="utf-8")
        except OSError:
            pass
    return content


def loaded(path: Path) -> Optional[Tuple[str, str]]:
    """(saved_at, text) of the handoff a session loaded at its start, or None."""
    for number, entry in _entries(path):
        if number > START_LINES or main_line(entry, "assistant"):
            break
        attachment = entry.get("attachment") if entry.get("type") == "attachment" else None
        if not isinstance(attachment, dict) or attachment.get("hookEvent") != "SessionStart":
            continue
        if attachment.get("type") != "hook_additional_context":
            continue
        content = attachment.get("content")
        parts = content if isinstance(content, list) else [content]
        whole = _whole("\n".join(part for part in parts if isinstance(part, str)))
        match = LOAD_RE.search(whole)
        if match:
            return match.group(1), whole
    return None


@dataclass(frozen=True)
class Reply:
    path: Path
    uuid: str
    at: dt.datetime


def replies(folder: Path) -> List[Reply]:
    """Every main-thread reply with a handoff in the folder's transcripts."""
    found = []
    for path in sorted(folder.glob("*.jsonl")):
        for _, entry in _entries(path, b"<contextrail-handoff>"):
            at = parse_time(entry.get("timestamp"))
            if at and entry.get("uuid") and has_handoff(entry):
                found.append(Reply(path, str(entry["uuid"]), at))
    return found


def request_index(entries: List[Entry], reply: int) -> Optional[int]:
    """Where A was asked for the handoff it saved: the nearest earlier ContextRail
    request, or the user's own message. Earlier attempts, fix rounds and anything
    typed during a fix round belong to the same request."""
    candidate = None
    for i in range(reply - 1, -1, -1):
        entry = entries[i]
        if is_request(entry):
            return i
        if is_fix(entry) or has_handoff(entry):
            candidate = None
        elif is_prompt(entry):
            candidate = i
        elif candidate is not None and main_line(entry, "assistant"):
            return candidate
    return candidate


def _last_reply(entries: List[Entry], before: int) -> Optional[int]:
    for i in range(before - 1, -1, -1):
        if main_line(entries[i], "assistant") and entries[i].get("uuid"):
            return i
    return None


def _measured(entries: List[Entry], upto: int) -> Optional[Entry]:
    """The latest reply at or before `upto` with a context size (synthetic ones have none)."""
    for i in range(upto, -1, -1):
        if usage._context_tokens(entries[i]) > 0:
            return entries[i]
    return None


def _window(texts: List[str]) -> Optional[int]:
    for words in texts:
        match = WINDOW_RE.search(words)
        if match:
            return int(match.group(1)) * (1_000_000 if match.group(2) == "M" else 1_000)
    return None


def _usage_line(entries: List[Entry], request: int, measured: Entry, loaded_text: str) -> Tuple[str, bool]:
    """The usage line and refresh flag for the request: copied from ContextRail's own
    request, or built the way it builds them when the user asked."""
    asked = hook_text(entries[request])
    if is_request(entries[request]):
        match = USAGE_RE.search(asked)
        if match:
            return match.group(1), REFRESH in asked
    tokens = usage._context_tokens(measured)
    model = _model(measured)
    earlier = [hook_text(entry) for entry in entries[:request] if is_request(entry)]
    window = _window(earlier[::-1] + [loaded_text])
    size, source = (window, "observed") if window else usage.resolve_window(model, tokens, None)
    refresh = any(has_handoff(entry) for entry in entries[:request])
    return usage.Usage(tokens=tokens, model=model, window=size, window_source=source).describe(), refresh


def _model(entry: Entry) -> str:
    requested = entry.get("requestedModel")
    return requested if isinstance(requested, str) and requested else str((entry.get("message") or {}).get("model") or "")


def _cap(entries: List[Entry], reply: int) -> Optional[int]:
    """The cap in the latest ContextRail request before the reply."""
    for i in range(reply - 1, -1, -1):
        if is_request(entries[i]):
            match = CAP_RE.search(hook_text(entries[i]))
            return int(match.group(1).replace(",", "")) if match else None
    return None


def build(a_path: Path, reply_uuid: str, b_path: Path, loaded_text: str, messages: int) -> Tuple[Optional[Case], str]:
    """The case for A's reply, or None and why not."""
    entries = read(a_path)
    reply = next((i for i, entry in enumerate(entries) if entry.get("uuid") == reply_uuid), None)
    if reply is None:
        return None, "the reply is gone from A's transcript"
    request = request_index(entries, reply)
    if request is None:
        return None, "no request found before A's reply"
    fork = _last_reply(entries, request)
    measured = _measured(entries, fork) if fork is not None else None
    if fork is None or measured is None:
        return None, "nothing to fork from before the request"
    usage_line, refresh = _usage_line(entries, request, measured, loaded_text)
    effort = measured.get("effort")
    case = Case(
        a=a_path.stem,
        b=b_path.stem,
        b_path=str(b_path),
        reply_uuid=reply_uuid,
        reply_at=str(entries[reply].get("timestamp")),
        fork_uuid=str(entries[fork]["uuid"]),
        asked_by="hook" if is_request(entries[request]) else "user",
        usage_line=usage_line,
        refresh=refresh,
        tokens=usage._context_tokens(measured),
        cwd=str(entries[fork].get("cwd") or measured.get("cwd") or ""),
        model=_model(measured),
        effort=effort if isinstance(effort, str) and effort else None,
        saved_cap=_cap(entries, reply),
        saved_body=handoff.extract(text(entries[reply])) or "",
        loaded=loaded_text,
        messages=messages,
    )
    return case, ""


def discover(project: Path, min_messages: int = 1, now: Optional[float] = None) -> Tuple[List[Case], List[str]]:
    """Cases in the transcripts of sessions started in `project`, oldest first, and a note on
    each pair left out. When two sessions loaded the same handoff, B is the one with more messages."""
    now = time.time() if now is None else now
    names = {claude_slug(project), claude_slug(project.resolve())}
    folders = [usage.claude_projects() / name for name in sorted(names)]
    found: List[Case] = []
    notes: List[str] = []
    for folder in folders:
        loads = [(path, *hit) for path in sorted(folder.glob("*.jsonl")) for hit in [loaded(path)] if hit]
        if not loads:
            continue
        candidates = replies(folder)
        chosen: Dict[Tuple[Path, str], Tuple[Path, str, int]] = {}
        for b_path, saved_at, loaded_text in loads:
            b = b_path.stem[:8]
            saved = parse_time(saved_at)
            window = [
                reply for reply in candidates
                if saved and reply.path != b_path
                and -REPLY_BEFORE <= (reply.at - saved).total_seconds() <= REPLY_AFTER
            ]
            if not window:
                notes.append(f"B {b}: no transcript holds the reply it loaded (saved {saved_at})")
                continue
            reply = max(window, key=lambda r: r.at)
            idle = now - b_path.stat().st_mtime
            if idle < QUIET_SECONDS:
                notes.append(f"B {b}: changed {idle / 60:.0f} minutes ago, may still be running")
                continue
            b_entries = read(b_path)
            if any(entry.get("entrypoint") == SCRIPTED for entry in b_entries):
                notes.append(f"B {b}: a scripted run")
                continue
            messages = sum(1 for entry in b_entries if is_message(entry))
            if messages < min_messages:
                notes.append(f"B {b}: {messages} message(s), fewer than {min_messages}")
                continue
            key = (reply.path, reply.uuid)
            if key in chosen and chosen[key][2] >= messages:
                notes.append(f"B {b}: also loaded A {reply.path.stem[:8]}'s handoff, with fewer messages")
                continue
            if key in chosen:
                notes.append(f"B {chosen[key][0].stem[:8]}: also loaded A {reply.path.stem[:8]}'s handoff, with fewer messages")
            chosen[key] = (b_path, loaded_text, messages)
        for (a_path, reply_uuid), (b_path, loaded_text, messages) in chosen.items():
            case, why = build(a_path, reply_uuid, b_path, loaded_text, messages)
            if case:
                found.append(case)
            else:
                notes.append(f"A {a_path.stem[:8]} → B {b_path.stem[:8]}: {why}")
    found.sort(key=lambda case: parse_time(case.reply_at) or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
    return found, notes


def split(key: str) -> str:
    """"tune" for the cases prompts and thresholds may be tuned on, "test" for the held-out ones
    that judge the result. Fixed by a hash of the key, so a case never changes sides."""
    return "tune" if int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) % 100 < TUNE_SHARE else "test"


def facts_part(loaded_text: str) -> str:
    """The script-recorded part of the handoff B loaded: its title, save line and facts."""
    start = loaded_text.find("# Handoff:")
    if start < 0:
        return ""
    head, mark, _ = loaded_text[start:].partition(handoff.NARRATIVE_MARK)
    return head.strip() if mark else ""
