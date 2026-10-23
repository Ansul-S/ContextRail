"""The handoff document: pull it out of the agent's reply, check it, and
render the saved file with the script-recorded facts on top."""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from .facts import MAX_LISTED, Activity, GitState, find_check

TAG = "contextrail-handoff"
BLOCK_RE = re.compile(r"<contextrail-handoff>\s*(.*?)\s*</contextrail-handoff>", re.S)
# The same with each tag on a line of its own, so a tag quoted inside the handoff doesn't end it.
LINE_BLOCK_RE = re.compile(
    r"^[ \t]*<contextrail-handoff>[ \t\r]*\n(.*?)\n[ \t]*</contextrail-handoff>[ \t\r]*$", re.S | re.M
)
HEADING_RE = re.compile(r"^##[ \t]+(.+?)[ \t#]*$", re.M)
NARRATIVE_MARK = "<!-- contextrail:narrative -->"
CARRIED_OVER = "_Carried over from the previous handoff"

REQUIRED = ["Goal", "User's rules", "Done", "In progress", "Next steps", "Resume prompt"]
# Keys are headings as _key() normalizes them, so "User's rules" is "users rules".
ALIASES = {
    "user rules": "users rules",
    "rules": "users rules",
    "constraints": "users rules",
    "facts": "known facts",
    "run": "how to run",
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
# Sections that may lose their last items when the handoff is too long to load whole, least
# needed first. Goal, User's rules, In progress, Next steps, Verify first and Resume prompt stay whole.
TRIM_ORDER = ("known facts", "dead ends", "decisions", "how to run", "done", "open questions")
SECTION_START_RE = re.compile(r"^##[ \t]", re.M)
CUT_OFF = "… The rest is cut; read the full handoff."
# Sections whose items the drift check holds against the files changed since.
CLAIM_SECTIONS = ("done", "in progress")
BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
EXCERPT_CHARS = 60
# Content checks against the recorded facts. They are kept loose on purpose:
# each one that fires costs a round trip, so only clear gaps should.
MAX_UNMENTIONED = 5
NOTHING_RE = re.compile(r"(none|nothing|n/?a)\b", re.I)
EVIDENCE_RE = re.compile(
    r"\b(tests?|tested|pass(ed|es|ing)?|ran|runs?|output|built|builds?|commit(s|ted)?|verif(ied|y)"
    r"|confirmed|checked|succeeded|ok)\b",
    re.I,
)
PASS_CLAIM_RE = re.compile(r"\b(pass(ed|es|ing)?|green|succeeded|ok)\b", re.I)
# "All tests pass", "the unit tests pass now", "tests passed".
GENERIC_PASS_RE = re.compile(r"\b(all|the)\b[^.;]*\btests?\b[^.;]*\bpass|\btests?\s+(now\s+)?pass", re.I)
TEST_RUN_RE = re.compile(r"test|spec|jest|mocha|tox|nox", re.I)
# Test files come with the code they test; "tests pass" covers them.
TEST_FILE_RE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*$|[._-](test|spec)\.[^/]+$|_test\.[^/]+$")
WAITING_RE = re.compile(r"wait for the user", re.I)
# A path, a command or name in backticks, a quoted request, an identifier, an issue number.
CONCRETE_RE = re.compile(
    r"[`\"“”]|\w/\w|\b\w+\.[A-Za-z]{1,5}\b|\b\w+_\w+\b|\w\(\)|\b[a-z]+[A-Z]\w*|\b(?=\w*\d)(?=\w*[A-Za-z])\w+\b|#\d+"
)


def extract(text: Optional[str]) -> Optional[str]:
    """The last handoff block in a message, without its tags: one whose tags stand on
    lines of their own if there is one, else any."""
    matches = LINE_BLOCK_RE.findall(text or "") or BLOCK_RE.findall(text or "")
    return matches[-1].strip() if matches else None


def _key(heading: str) -> str:
    key = " ".join(re.sub(r"[^a-z]+", " ", re.sub(r"['’]", "", heading.lower())).split())
    return ALIASES.get(key, key)


def sections(body: str) -> Dict[str, str]:
    parts = HEADING_RE.split(body)
    return {_key(parts[i]): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}


def _name_pattern(names: List[str]) -> "re.Pattern[str]":
    # A whole name only: app.py is not in myapp.py or app.pyc, but is in src/app.py.
    names = sorted(set(names), key=len, reverse=True)
    return re.compile(r"(?<![\w.-])(?:" + "|".join(map(re.escape, names)) + r")(?![\w/-]|\.\w)")


def _items(text: str) -> List[str]:
    return [BULLET_RE.sub("", line).strip() for line in text.splitlines() if line.strip()]


def _uncommitted(path: str, dirty: List[str]) -> bool:
    # git lists a new folder as one entry, "folder/".
    return path in dirty or any(entry.endswith("/") and path.startswith(entry) for entry in dirty)


def _mentioned(path: str, body: str) -> bool:
    """Whether the narrative names the file: by path, by file name, by name without
    the extension (`handoff`, `handoff.render`, CHANGELOG), or by its folder."""
    name = path.rsplit("/", 1)[-1]
    if _name_pattern([path, name]).search(body):
        return True
    stem = name.split(".")[0]
    if len(stem) > 2 and re.search(rf"(?<![\w./-]){re.escape(stem)}(?![\w/-])", body):
        return True
    folder = path.rsplit("/", 1)[0] + "/" if "/" in path else ""
    return bool(folder) and re.search(rf"(?<![\w.-]){re.escape(folder)}(?![\w-]|\.\w)", body) is not None


def _claims_pass(item: str, command: str, superseded: bool) -> bool:
    """Whether a Done item says this failed check passed: by quoting it, or, for a
    test run no later test run passed after, with a general claim that quotes no
    check of its own."""
    if f"`{command}`" in item:
        return bool(PASS_CLAIM_RE.search(item))
    if superseded or not TEST_RUN_RE.search(command):
        return False
    quotes_a_check = any(find_check(span) for span in re.findall(r"`([^`]+)`", item))
    return not quotes_a_check and bool(GENERIC_PASS_RE.search(item))


def content_problems(body: str, activity: Activity, dirty: List[str]) -> List[str]:
    """Gaps between the handoff and what ContextRail recorded in the session."""
    found = sections(body)
    issues = []
    # Committed files have their commit messages; uncommitted ones only have the handoff.
    unmentioned = [
        path for path in activity.edited
        if _uncommitted(path, dirty) and not TEST_FILE_RE.search(path) and not _mentioned(path, body)
    ]
    if unmentioned:
        listed = ", ".join(f"`{path}`" for path in unmentioned[:MAX_UNMENTIONED])
        more = f" and {len(unmentioned) - MAX_UNMENTIONED} more" if len(unmentioned) > MAX_UNMENTIONED else ""
        issues.append(f"mention or explain these uncommitted files edited in this session: {listed}{more}")
    done = [item for item in _items(found.get("done", "")) if not NOTHING_RE.match(item)]
    if done and not any(EVIDENCE_RE.search(item) for item in done):
        issues.append(
            "'## Done' gives no evidence (a test, command output or commit): "
            "add it, or move the items to '## In progress'"
        )
    steps = _items(found.get("next steps", ""))
    if steps and not WAITING_RE.search(steps[0]) and not CONCRETE_RE.search(steps[0]):
        issues.append("make step 1 of '## Next steps' concrete: name the file, function or command")
    for i, (command, result) in enumerate(activity.checks):
        if result != "failed":
            continue
        superseded = any(later == "passed" and TEST_RUN_RE.search(c) for c, later in activity.checks[i + 1:])
        if any(_claims_pass(item, command, superseded) for item in done):
            issues.append(
                f"the facts show `{command}` failed, but '## Done' says it passed: correct that, or run it again"
            )
    return issues


def problems(
    body: str, max_chars: int, activity: Optional[Activity] = None, dirty: Optional[List[str]] = None
) -> List[str]:
    """What keeps the handoff from being saved as it is. With the session's
    activity, also check its content against the recorded facts."""
    found = sections(body)
    issues = []
    missing = [name for name in REQUIRED if not found.get(_key(name))]
    if missing:
        issues.append("missing or empty: " + ", ".join(f"'## {name}'" for name in missing))
    if len(body) > max_chars:
        issues.append(
            f"too long: {len(body):,} characters, limit {max_chars:,}. "
            "Cut detail but keep paths, decisions and next steps"
        )
    if len(found.get("resume prompt", "")) > MAX_RESUME_CHARS:
        issues.append(f"'## Resume prompt' is over {MAX_RESUME_CHARS} characters; keep it to 2-3 lines")
    if activity is not None:
        issues += content_problems(body, activity, dirty or [])
    return issues


def resume_prompt(body: str) -> str:
    return sections(body).get("resume prompt", "").strip()


def mentions(narrative: str, paths: List[str]) -> List[str]:
    """Done and In progress items that name one of these files, by path or file name, as excerpts."""
    if not paths:
        return []
    pattern = _name_pattern(paths + [path.rsplit("/", 1)[-1] for path in paths])
    found = sections(narrative)
    items: List[str] = []
    for key in CLAIM_SECTIONS:
        for text in _items(found.get(key, "")):
            match = pattern.search(text)
            excerpt = _excerpt(text, match) if match else None
            if excerpt and excerpt not in items:
                items.append(excerpt)
    return items


def _excerpt(text: str, match: "re.Match[str]") -> str:
    """The item cut to EXCERPT_CHARS, keeping the file name it was flagged for."""
    if len(text) <= EXCERPT_CHARS:
        return text
    if match.end() < EXCERPT_CHARS:
        return text[: EXCERPT_CHARS - 1] + "…"
    tail = "…" if match.end() < len(text) else ""
    return "…" + text[match.end() - EXCERPT_CHARS + 2 : match.end()] + tail


def narrative_of(document: str) -> str:
    """The agent-written part of a saved handoff."""
    _, mark, rest = document.partition(NARRATIVE_MARK)
    return rest.strip() if mark else ""


def units(text: str) -> int:
    """Length the way Claude Code measures hook output: in UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def _parts(narrative: str) -> List[List[str]]:
    """The narrative as [text before the first heading], then [heading, item, item, ...]
    per section. An item is a line that isn't indented, with the lines indented under it."""
    starts = [match.start() for match in SECTION_START_RE.finditer(narrative)]
    parts = [[narrative[: starts[0] if starts else len(narrative)].strip()]]
    for begin, end in zip(starts, starts[1:] + [len(narrative)]):
        heading, _, rest = narrative[begin:end].strip().partition("\n")
        items: List[str] = []
        for line in rest.strip("\n").splitlines():
            if items and (not line.strip() or line[:1].isspace()):
                items[-1] += "\n" + line
            elif line.strip():
                items.append(line)
        parts.append([heading] + [item.rstrip() for item in items])
    return parts


def shorten(document: str, limit: int) -> str:
    """A saved handoff cut to `limit` UTF-16 code units. Items are dropped from the end of
    the TRIM_ORDER sections, least needed first, and each cut says how many items went. If
    that is not enough, the text is cut off at the end."""
    if units(document) <= limit:
        return document
    head, mark, narrative = document.partition(NARRATIVE_MARK)
    if not mark:
        head, narrative = "", document
    parts = _parts(narrative)
    keys = [""] + [_key(part[0]) for part in parts[1:]]
    dropped = [0] * len(parts)

    def render() -> str:
        blocks = [head.rstrip() + "\n\n" + mark if mark else ""]
        for part, count in zip(parts, dropped):
            lines = part + ([f"- … {count} more in the full handoff"] if count else [])
            blocks.append("\n".join(line for line in lines if line))
        return "\n\n".join(block for block in blocks if block) + "\n"

    text = render()
    for kept in (1, 0):  # each section's first item goes only if cutting the rest is not enough
        for key in TRIM_ORDER:
            for i, part in enumerate(parts):
                while keys[i] == key and len(part) > 1 + kept and units(text) > limit:
                    part.pop()
                    dropped[i] += 1
                    text = render()
    if units(text) <= limit:
        return text
    room = limit - units(CUT_OFF) - 1
    if room <= 0:
        return ""
    text = text[:room]
    while units(text) > room:  # characters outside the BMP take two units
        text = text[:-1]
    return text.rpartition("\n")[0] + "\n" + CUT_OFF


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


def fallback_body(previous_narrative: str, previous_saved_at: Optional[str], when: str = "before compaction") -> str:
    """Body for a handoff saved without the agent: before compaction, or when a session ends."""
    if previous_narrative.startswith(CARRIED_OVER):
        previous_narrative = previous_narrative.split("\n\n", 1)[-1]
    if previous_narrative:
        when = f" (saved {previous_saved_at})" if previous_saved_at else ""
        return (
            f"{CARRIED_OVER}{when}. Work done after it is only "
            "reflected in the facts above._\n\n" + previous_narrative
        )
    return (
        f"## Goal\nNot recorded: this handoff was saved automatically {when}.\n\n"
        "## Next steps\n1. Read the facts above, and the conversation summary if there is one, then confirm "
        "the goal and next step with the user.\n\n"
        "## Resume prompt\nContinue where the last session stopped. Check the handoff facts "
        "and tell me what you think the next step is before changing anything."
    )
