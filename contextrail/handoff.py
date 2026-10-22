"""The handoff document: pull it out of the agent's reply, check it, and
render the saved file with the script-recorded facts on top."""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from .facts import MAX_LISTED, Activity, GitState

TAG = "contextrail-handoff"
BLOCK_RE = re.compile(r"<contextrail-handoff>\s*(.*?)\s*</contextrail-handoff>", re.S)
HEADING_RE = re.compile(r"^##[ \t]+(.+?)[ \t#]*$", re.M)
NARRATIVE_MARK = "<!-- contextrail:narrative -->"
CARRIED_OVER = "_Carried over from the previous handoff"

REQUIRED = ["Goal", "Done", "In progress", "Next steps", "Resume prompt"]
ALIASES = {
    "next": "next steps",
    "next step": "next steps",
    "resume": "resume prompt",
    "wip": "in progress",
    "in flight": "in progress",
    "dead end": "dead ends",
    "decision": "decisions",
    "verify": "verify first",
}
MAX_RESUME_CHARS = 800


def extract(text: Optional[str]) -> Optional[str]:
    """The last handoff block in a message, without its tags."""
    matches = BLOCK_RE.findall(text or "")
    return matches[-1].strip() if matches else None


def _key(heading: str) -> str:
    key = " ".join(re.sub(r"[^a-z]+", " ", heading.lower()).split())
    return ALIASES.get(key, key)


def sections(body: str) -> Dict[str, str]:
    parts = HEADING_RE.split(body)
    return {_key(parts[i]): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}


def problems(body: str, max_chars: int) -> List[str]:
    found = sections(body)
    issues = []
    missing = [name for name in REQUIRED if not found.get(name.lower())]
    if missing:
        issues.append("missing or empty: " + ", ".join(f"'## {name}'" for name in missing))
    if len(body) > max_chars:
        issues.append(
            f"too long: {len(body):,} characters, limit {max_chars:,}. "
            "Cut detail but keep paths, decisions and next steps"
        )
    if len(found.get("resume prompt", "")) > MAX_RESUME_CHARS:
        issues.append(f"'## Resume prompt' is over {MAX_RESUME_CHARS} characters; keep it to 2-3 lines")
    return issues


def resume_prompt(body: str) -> str:
    return sections(body).get("resume prompt", "").strip()


def narrative_of(document: str) -> str:
    """The agent-written part of a saved handoff."""
    _, mark, rest = document.partition(NARRATIVE_MARK)
    return rest.strip() if mark else ""


def _edited(activity: Activity) -> str:
    """The files edited in the project, with the rest only counted."""
    listed = ", ".join(f"`{path}`" for path in activity.edited[-MAX_LISTED:])
    counted = []
    if len(activity.edited) > MAX_LISTED:
        counted.append(f"{len(activity.edited) - MAX_LISTED} more")
    if activity.outside:
        counted.append(f"{activity.outside} file{'' if activity.outside == 1 else 's'} outside the project")
    text = ", ".join([part for part in [listed] if part] + counted[:-1])
    if counted:
        text = f"{text} and {counted[-1]}" if text else counted[-1]
    return text


def render(
    project: str,
    saved_at: str,
    usage_line: str,
    git: Optional[GitState],
    activity: Activity,
    body: str,
    notes: List[str],
) -> str:
    lines = [f"# Handoff: {project}", "", f"_Saved {saved_at}, at {usage_line}._", ""]
    lines.append("## Facts (recorded by ContextRail)")
    if git is None:
        lines.append("- Not a git repository.")
    else:
        head = f"`{git.head[:9]}` {git.subject}".strip() if git.head else "no commits yet"
        lines.append(f"- Branch `{git.branch or '?'}` at {head}")
        if git.dirty:
            shown = ", ".join(f"`{path}`" for path in git.dirty[:15])
            more = f" and {len(git.dirty) - 15} more" if len(git.dirty) > 15 else ""
            lines.append(f"- Uncommitted ({len(git.dirty)}): {shown}{more}")
        else:
            lines.append("- Working tree clean")
    if activity.edited or activity.outside:
        lines.append("- Edited this session: " + _edited(activity))
    if activity.checks:
        lines.append("- Recent checks: " + "; ".join(f"`{cmd}` {result}" for cmd, result in activity.checks))
    for note in notes:
        lines += ["", f"> {note}"]
    lines += ["", NARRATIVE_MARK, "", body.strip(), ""]
    return "\n".join(lines)


def fallback_body(previous_narrative: str, previous_saved_at: Optional[str]) -> str:
    """Body for a handoff saved without the agent (before compaction)."""
    if previous_narrative.startswith(CARRIED_OVER):
        previous_narrative = previous_narrative.split("\n\n", 1)[-1]
    if previous_narrative:
        when = f" (saved {previous_saved_at})" if previous_saved_at else ""
        return (
            f"{CARRIED_OVER}{when}. Work done after it is only "
            "reflected in the facts above._\n\n" + previous_narrative
        )
    return (
        "## Goal\nNot recorded: this handoff was saved automatically before compaction.\n\n"
        "## Next steps\n1. Read the facts above and the conversation summary, then confirm "
        "the goal and next step with the user.\n\n"
        "## Resume prompt\nContinue where the last session stopped. Check the handoff facts "
        "and tell me what you think the next step is before changing anything."
    )
