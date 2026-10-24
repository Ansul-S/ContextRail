"""Facts recorded by script rather than written by the model: git state,
files edited in the session, recent test/build commands, and drift since
the last handoff."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# Programs that are checks on their own.
CHECK_TOOLS = {
    "pytest", "py.test", "tox", "nox", "vitest", "jest", "mocha", "playwright", "cypress", "rspec",
    "phpunit", "ruff", "mypy", "pyright", "flake8", "pylint", "eslint", "tsc", "ctest",
    "mvn", "mvnw", "gradle", "gradlew",
}
# Modules that are checks when run as `python -m X`.
CHECK_MODULES = {"pytest", "unittest", "tox", "nox", "mypy", "ruff", "flake8", "pylint", "pyright"}
# Programs that are checks when their subcommand names one (`go test`).
VERB_TOOLS = {"go", "cargo", "swift", "dotnet", "deno"}
# Programs that are checks when run bare or with a target that names one (`make lint`).
TARGET_TOOLS = {"make", "gmake", "just"}
PACKAGE_MANAGERS = {"npm", "pnpm", "yarn", "bun"}
LAUNCHERS = {"npx", "bunx", "pnpx", "uvx"}  # the rest is a command
RUNNERS = {"uv", "poetry", "pipenv", "pdm", "hatch", "rye", "bundle"}  # `uv run X`, `bundle exec X`
CHECK_VERB = re.compile(r"(tests?|check|build|lint|type-?check|verify|vet|clippy|e2e)(?![a-z])")
# Flags of package managers that take a value: `pnpm --filter web build`.
VALUE_FLAGS = {"--filter", "-F", "-C", "--dir", "--prefix", "-w", "--workspace", "--cwd"}
# Words that can come before the program: `time pytest`, `if pytest; then`.
CONTROL_WORDS = {"!", "if", "elif", "then", "else", "do", "while", "until"}
PREFIX_WORDS = CONTROL_WORDS | {"time", "env", "nice", "nohup", "command", "exec", "builtin"}
ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
REDIRECT_RE = re.compile(r"(\d*|&)(>>?|<)")
HEREDOC_RE = re.compile(r"(?<!<)<<(?!<)-?[ \t]*(['\"]?)([A-Za-z_][\w.-]*)\1")
# Summary lines of test runners, type checkers and builds, for output whose exit
# code is hidden by a pipe or a later command.
FAIL_RE = re.compile(
    r"\bFAILED \((failures|errors)=|\b[1-9]\d* (failed|failing|errors?)\b|^FAIL\b|test result: FAILED"
    r"|\bBUILD FAIL(ED|URE)\b|\bBuild FAILED\b|error: could not compile",
    re.M,
)
PASS_RE = re.compile(
    r"^OK( \(|$)|\b\d+ (passed|passing)\b|^ok\s|test result: ok\b|All checks passed|Success: no issues found"
    r"|\b0 errors\b|\bBUILD SUCCESS(FUL)?\b|\bBuild succeeded\b|Compiled successfully",
    re.M,
)
MAX_CHECK_CHARS = 80
MAX_CHECKS = 6
MAX_LISTED = 15
# Uncommitted files hashed when a handoff is saved, so the next session can tell
# which of them changed since. Above this, the drift check falls back to git state.
MAX_HASHED = 300
MAX_HASH_BYTES = 8 * 1024 * 1024
# Characters per drift note, so the whole block stays near 1,000.
BRANCH_CHARS = 30
COMMITS_BUDGET = 180
FILES_BUDGET = 200
DIRTY_BUDGET = 120
ITEM_CHARS = 60
# Deleted and new files compared by content to find renames git doesn't pair up.
MAX_RENAME_PAIRS = 50


def run_git(args: List[str], cwd: Path, stdin: Optional[str] = None) -> Optional[str]:
    """Raw stdout of a git command, or None if git fails or is missing."""
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, encoding="utf-8", errors="replace",
            timeout=5, input=stdin,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def project_root(cwd: str) -> Path:
    top = run_git(["rev-parse", "--show-toplevel"], Path(cwd))
    return Path(top.strip()) if top and top.strip() else Path(cwd).resolve()


@dataclass
class GitState:
    branch: str
    head: str
    subject: str
    dirty: List[str]
    diff_hash: str
    # For a linked worktree (git worktree add), the repository it belongs to; "" for a main one.
    worktree_of: str = ""


def git_state(root: Path) -> Optional[GitState]:
    dirs = run_git(["rev-parse", "--absolute-git-dir", "--git-common-dir"], root)
    if dirs is None:
        return None
    worktree_of = ""
    parts = dirs.strip().splitlines()
    if len(parts) == 2:
        own, common = Path(parts[0]), Path(parts[1])
        common = common if common.is_absolute() else (root / common)
        if own.resolve() != common.resolve():
            worktree_of = str(common.resolve().parent)
    head = (run_git(["rev-parse", "HEAD"], root) or "").strip()
    branch = (run_git(["rev-parse", "--abbrev-ref", "HEAD"], root) or "").strip()
    subject = (run_git(["log", "-1", "--format=%s"], root) or "").strip() if head else ""
    status = run_git(["status", "--porcelain=v1", "--untracked-files=normal"], root) or ""
    dirty = [line[3:] for line in status.splitlines() if len(line) > 3]
    diff = run_git(["diff", "HEAD"], root) if head else ""
    diff_hash = hashlib.sha1((diff or "").encode("utf-8", "replace")).hexdigest()
    return GitState(branch=branch, head=head, subject=subject, dirty=dirty, diff_hash=diff_hash, worktree_of=worktree_of)


class CheckRun(NamedTuple):
    """A test, lint or build run, with what decides whether its result still holds."""

    command: str  # the check alone, shortened
    result: str  # one of RESULTS
    exit_code: Optional[int] = None  # of the check when known, else of the whole command
    evidence: str = ""  # "exit status", "output", or "" when nothing showed the result
    cwd: Optional[str] = None  # where it ran, relative to the project root ("." for the root)
    at: Optional[str] = None  # when it ran, as the transcript records it (ISO 8601, UTC)
    edits_after: int = 0  # files in the project edited after it ran, in the same session
    git_after: bool = False  # a commit, checkout, reset, rebase, merge, pull or stash ran after it

    @property
    def verified(self) -> bool:
        """Only an exit status of 0 that is the check's own counts as a verified pass."""
        return self.result == "passed"

    def to_json(self) -> Dict[str, Any]:
        return dict(self._asdict(), verified=self.verified)


# What a check's result can be. "passed" needs the check's own exit status of 0; "output says passed"
# is a passing summary line from a command whose exit status a pipe or a later command hid.
RESULTS = (
    "passed", "failed", "output says passed", "ran", "timed out", "interrupted", "stopped", "no result yet",
    "result unknown",
)
PASSING = ("passed", "output says passed")


@dataclass
class Activity:
    edited: List[str] = field(default_factory=list)  # inside the project, latest edit last
    outside: int = 0  # files edited outside the project
    checks: List[CheckRun] = field(default_factory=list)  # oldest first, the latest run of each command

    def __post_init__(self) -> None:
        # (command, result) pairs are taken as runs with nothing else known.
        self.checks = [check if isinstance(check, CheckRun) else CheckRun(*check) for check in self.checks]

    def results(self) -> List[Tuple[str, str]]:
        return [(check.command, check.result) for check in self.checks]


@dataclass(frozen=True)
class Check:
    """The test, lint or build command found in a Bash command."""

    command: str  # that command alone, shortened
    exit_status: bool  # whether the Bash command's exit status is the check's
    detached: bool = False  # sent to the background, so its result is not in the transcript


def _drop_heredocs(command: str) -> str:
    kept: List[str] = []
    ends: List[str] = []
    for line in command.split("\n"):
        if ends:
            if line.strip() == ends[0]:
                ends.pop(0)
            continue
        ends = [match.group(2) for match in HEREDOC_RE.finditer(line)]
        kept.append(HEREDOC_RE.sub("", line))
    return "\n".join(kept)


def split_command(command: str) -> List[Tuple[List[str], str]]:
    """Top-level parts of a shell command, each a list of pipeline stages, paired
    with the operator that follows it (&&, ||, ;, newline, & or ""). Quotes are
    respected; heredoc bodies and comments are dropped."""
    text = _drop_heredocs(command)
    segments: List[Tuple[List[str], str]] = []
    stages: List[str] = []
    buf: List[str] = []
    quote = ""

    def cut(op: str) -> None:
        stages.append("".join(buf).strip())
        buf.clear()
        if op == "|":
            return
        kept = [stage for stage in stages if stage]
        if kept:
            segments.append((kept, op))
        stages.clear()

    i = 0
    while i < len(text):
        char, pair = text[i], text[i:i + 2]
        step = 1
        if quote:
            if char == "\\" and quote == '"':
                buf.append(pair)
                step = 2
            else:
                quote = "" if char == quote else quote
                buf.append(char)
        elif pair == "\\\n":
            buf.append(" ")
            step = 2
        elif char == "\\":
            buf.append(pair)
            step = 2
        elif char in "'\"":
            quote = char
            buf.append(char)
        elif char == "#" and (i == 0 or text[i - 1] in " \t\n;&|("):
            end = text.find("\n", i)
            step = (end if end >= 0 else len(text)) - i
        elif pair in ("&&", "||"):
            cut(pair)
            step = 2
        elif char in ";\n":
            cut(char)
        elif char == "|":
            cut("|")
            step = 2 if pair == "|&" else 1
        elif char == "&" and text[i - 1:i] not in (">", "<") and text[i + 1:i + 2] != ">":
            cut("&")
        else:
            buf.append(char)
        i += step
    cut("")
    return segments


def _words(stage: str) -> List[str]:
    """The words of a pipeline stage from the program on, without redirections."""
    try:
        raw = shlex.split(stage)
    except ValueError:
        raw = stage.split()
    words: List[str] = []
    skip = False
    for word in raw:
        if skip:
            skip = False
            continue
        redirect = REDIRECT_RE.match(word)
        if redirect:
            skip = redirect.group(0) == word  # the target is the next word
            continue
        words.append(word)
    while words:
        word = words[0].lstrip("({")
        if word in ("timeout", "gtimeout"):
            words = words[2:]
        elif not word or word in PREFIX_WORDS or word.startswith("-") or ASSIGNMENT_RE.match(word):
            words = words[1:]
        else:
            words[0] = word
            break
    return words


def _plain(args: List[str]) -> List[str]:
    """Arguments that are not flags or the values of flags."""
    plain: List[str] = []
    skip = False
    for word in args:
        if skip:
            skip = False
        elif word in VALUE_FLAGS:
            skip = True
        elif not word.startswith(("-", "+")):
            plain.append(word)
    return plain


def is_check(words: List[str]) -> bool:
    """Whether a command (as words, program first) runs tests, a linter or a build."""
    if not words:
        return False
    program = os.path.basename(words[0])
    args = words[1:]
    plain = _plain(args)
    if program == "ruff" and plain[:1] == ["format"]:
        return "--check" in args or "--diff" in args  # otherwise it rewrites files
    if program in CHECK_TOOLS:
        return True
    if re.fullmatch(r"python[0-9.]*|py", program):
        if "-m" in args[:-1]:
            return args[args.index("-m") + 1].split(".")[0] in CHECK_MODULES
        # `python manage.py test`
        return len(plain) > 1 and plain[0].endswith(".py") and bool(CHECK_VERB.match(plain[1]))
    if program in VERB_TOOLS:
        return bool(plain) and bool(CHECK_VERB.match(plain[0]))
    if program in TARGET_TOOLS:
        return not plain or any(CHECK_VERB.match(word) for word in plain)
    if program in PACKAGE_MANAGERS:
        if plain[:1] == ["workspace"]:  # yarn workspace <name> <script>
            plain = plain[2:]
        if plain[:1] in (["exec"], ["dlx"], ["x"]):
            return is_check(plain[1:])
        if plain[:1] in (["run"], ["run-script"]):
            plain = plain[1:]
        return bool(plain) and (bool(CHECK_VERB.match(plain[0])) or plain[0] in CHECK_TOOLS)
    if program in RUNNERS:
        if plain[:1] not in (["run"], ["exec"]):
            return False
        if len(plain) > 1 and CHECK_VERB.match(plain[1]):  # a project script: `pdm run test`
            return True
        args = args[args.index(plain[0]) + 1:]
    elif program not in LAUNCHERS:
        return False
    # The rest is a command of its own, possibly after flags with values.
    return any(is_check(args[i:]) for i, word in enumerate(args) if not word.startswith("-"))


def _shown(stage: str) -> str:
    words = stage.replace("2>&1", " ").split()
    while words and words[0] in CONTROL_WORDS:
        words = words[1:]
    text = " ".join(words).lstrip("({ ")
    while text.endswith(")") and text.count(")") > text.count("("):
        text = text[:-1].rstrip()
    while text.endswith("}") and text.count("}") > text.count("{"):
        text = text[:-1].rstrip()
    return text if len(text) <= MAX_CHECK_CHARS else text[: MAX_CHECK_CHARS - 1] + "…"


def find_check(command: str) -> Optional[Check]:
    """The first test, lint or build command in a Bash command, if there is one."""
    segments = split_command(command)
    found = [i for i, (stages, _) in enumerate(segments) if is_check(_words(stages[0]))]
    if not found:
        return None
    first = found[0]
    stages, op = segments[first]
    shown = _shown(stages[0])
    if len(found) > 1:
        shown += f" (+{len(found) - 1} more)"
    # A pipe, or a later command not joined by &&, decides the exit status instead.
    exit_status = len(stages) == 1 and all(later == "&&" for _, later in segments[first:-1])
    return Check(command=shown, exit_status=exit_status, detached=op == "&" and first == len(segments) - 1)


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def output_verdict(text: str) -> Optional[str]:
    """passed or failed, from the summary lines that test runners, type checkers
    and builds print; None when there are none or they disagree."""
    verdicts = [
        "failed" if FAIL_RE.search(line) else "passed"
        for line in text.splitlines()
        if FAIL_RE.search(line) or PASS_RE.search(line)
    ]
    if not verdicts:
        return None
    if verdicts[-1] == "failed":
        return "failed"
    return None if "failed" in verdicts else "passed"


EXIT_RE = re.compile(r"\AExit code (\d+)")
TIMED_OUT_RE = re.compile(r"^Command timed out after ", re.M)
INTERRUPTED_RE = re.compile(r"\[Request interrupted by user")
# Claude Code moved a command that ran too long, or one a message arrived during, to the background.
MOVED_RE = re.compile(r"(?:moved to the background \(ID: |running in background with ID: )([A-Za-z0-9_-]+)")
# What Claude Code tells the session when a background command ends.
NOTICE_RE = re.compile(r"<task-notification>(.*?)</task-notification>", re.S)
NOTICE_FIELD_RE = re.compile(r"<(tool-use-id|task-id|status|summary)>(.*?)</\1>", re.S)
SUMMARY_EXIT_RE = re.compile(r"exit code (\d+)")
# Exit codes of a command stopped by a signal: 128 + SIGTERM, and Claude Code's 144 for a stopped task.
SIGNALLED = (130, 137, 143, 144)
# Git commands after which a check no longer describes the files: they change HEAD or the working tree.
GIT_CHANGE_RE = re.compile(
    r"(?:^|[;&|(\s])git(?:\s+-[Cc]\s+\S+)*\s+"
    r"(commit|checkout|switch|reset|rebase|merge|pull|stash|cherry-pick|revert|am|apply|restore|clean)\b"
)


def _verdict(check: Check, text: str, failed: bool, code: Optional[int]) -> Tuple[str, str]:
    """The result of a finished command and what showed it: (result, evidence)."""
    if code in SIGNALLED or TIMED_OUT_RE.search(text):
        return ("timed out" if TIMED_OUT_RE.search(text) else "stopped"), "exit status"
    if check.exit_status:
        return ("failed" if failed else "passed"), "exit status"
    summary = output_verdict(text)
    if summary == "failed":
        return "failed", "output"
    if summary == "passed":
        # A passing summary only stands for a command that finished well; otherwise the two disagree.
        return ("result unknown", "output") if failed else ("output says passed", "output")
    return "ran", ""


def check_result(check: Check, block: Dict[str, Any]) -> str:
    """The check's result from its tool result alone: see RESULTS."""
    return _check_outcome(check, block)[0]


def _check_outcome(check: Check, block: Dict[str, Any]) -> Tuple[str, str, Optional[int], Optional[str]]:
    """(result, evidence, exit code, background task id) of a check from its tool result."""
    text = _result_text(block.get("content"))
    moved = MOVED_RE.search(text)
    if moved:
        return "no result yet", "", None, moved.group(1)
    if check.detached:
        return "no result yet", "", None, None
    if INTERRUPTED_RE.search(text):
        return "interrupted", "", None, None
    failed = bool(block.get("is_error"))
    code_match = EXIT_RE.match(text)
    code = int(code_match.group(1)) if code_match else (None if failed else 0)
    if failed and not code_match:
        return "result unknown", "", None, None  # denied, blocked or stopped before it finished
    result, evidence = _verdict(check, text, failed, code)
    return result, evidence, code, None


def _notice_outcome(check: Check, fields: Dict[str, str]) -> Tuple[str, str, Optional[int]]:
    """The result of a background check from the notification of its end."""
    status = fields.get("status", "").strip().lower()
    summary = fields.get("summary", "")
    found = SUMMARY_EXIT_RE.search(summary)
    code = int(found.group(1)) if found else (0 if status == "completed" else None)
    if status in ("killed", "stopped") or "was stopped" in summary:
        return "stopped", "exit status", code
    if code is None:
        return "result unknown", "", None
    if code in SIGNALLED:
        return "stopped", "exit status", code
    # The notification gives the exit status of the whole command, which is the check's only when
    # nothing after it decides the status.
    if check.exit_status:
        return ("passed" if code == 0 else "failed"), "exit status", code
    return ("ran" if code == 0 else "result unknown"), "", code


def _inside(path: str, root: Path) -> Optional[str]:
    """path relative to the project root, or None if it is outside the project."""
    try:
        return str(Path(path).resolve().relative_to(root))
    except (ValueError, OSError):
        return None


def _relative_cwd(cwd: Any, root: Path) -> Optional[str]:
    if not isinstance(cwd, str) or not cwd:
        return None
    inside = _inside(cwd, root)
    return (inside or ".") if inside is not None else cwd


def _notices(content: Any) -> List[Dict[str, str]]:
    """The background-task notifications in a user message, as their fields."""
    text = content if isinstance(content, str) else _result_text(content)
    if "<task-notification>" not in text:
        return []
    return [dict(NOTICE_FIELD_RE.findall(body)) for body in NOTICE_RE.findall(text)]


def session_activity(transcript_path: Optional[str], root: Path) -> Activity:
    """Files the session edited and the checks it ran, from its transcript. Each check keeps what
    shows whether its result still holds: when and where it ran, and what was edited after it."""
    edited: Dict[str, int] = {}  # path -> sequence number of its latest edit
    outside = set()
    checks: Dict[str, Tuple[Check, int, Optional[str], Optional[str]]] = {}  # tool id -> (check, seq, cwd, at)
    outcomes: Dict[str, Tuple[str, str, Optional[int]]] = {}
    tasks: Dict[str, str] = {}  # background task id -> tool id
    pending = set()  # tool ids of checks moved to the background, waiting for their notification
    git_changes: List[int] = []  # sequence numbers of git commands that change the tree
    seq = 0
    if not transcript_path:
        return Activity()
    root = root.resolve()
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"tool_use"' not in line and '"tool_result"' not in line and "task-notification" not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("isSidechain"):
                    continue
                message = entry.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if entry.get("type") == "user":
                    for fields in _notices(content):
                        tool_id = fields.get("tool-use-id") or tasks.get(fields.get("task-id", ""), "")
                        if tool_id in checks:
                            outcomes[tool_id] = _notice_outcome(checks[tool_id][0], fields)
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        seq += 1
                        name = block.get("name") if isinstance(block.get("name"), str) else ""
                        args = block.get("input") if isinstance(block.get("input"), dict) else {}
                        if name in EDIT_TOOLS:
                            path = args.get("file_path") or args.get("notebook_path")
                            if isinstance(path, str):
                                relative = _inside(path, root)
                                if relative is None:
                                    outside.add(path)
                                else:
                                    edited.pop(relative, None)
                                    edited[relative] = seq
                        elif name == "Bash":
                            command = args.get("command")
                            if not isinstance(command, str):
                                continue
                            if GIT_CHANGE_RE.search(command):
                                git_changes.append(seq)
                            check = find_check(command)
                            if check and args.get("run_in_background"):
                                check = replace(check, detached=True)
                            if check:
                                at = entry.get("timestamp") if isinstance(entry.get("timestamp"), str) else None
                                checks[str(block.get("id"))] = (check, seq, _relative_cwd(entry.get("cwd"), root), at)
                    elif block.get("type") == "tool_result":
                        tool_id = str(block.get("tool_use_id"))
                        if tool_id in checks and tool_id not in outcomes:
                            result, evidence, code, task = _check_outcome(checks[tool_id][0], block)
                            if task:
                                tasks[task] = tool_id
                            if result == "no result yet":
                                pending.add(tool_id)
                            else:
                                outcomes[tool_id] = (result, evidence, code)
    except OSError:
        return Activity()
    latest: Dict[str, CheckRun] = {}
    for tool_id, (check, at_seq, cwd, at) in checks.items():
        waiting = check.detached or tool_id in pending
        result, evidence, code = outcomes.get(tool_id, ("no result yet" if waiting else "result unknown", "", None))
        latest.pop(check.command, None)
        latest[check.command] = CheckRun(
            command=check.command, result=result, exit_code=code, evidence=evidence, cwd=cwd, at=at,
            edits_after=sum(1 for last in edited.values() if last > at_seq),
            git_after=any(change > at_seq for change in git_changes),
        )
    return Activity(edited=list(edited), outside=len(outside), checks=list(latest.values())[-MAX_CHECKS:])


def _clip(text: str, limit: int, keep_end: bool = False) -> str:
    if len(text) <= limit:
        return text
    return "…" + text[-(limit - 1):] if keep_end else text[: limit - 1] + "…"


def _fit(items: List[str], budget: int, sep: str = ", ") -> str:
    """As many items as fit in about budget characters, then how many are left."""
    shown: List[str] = []
    used = 0
    for item in items:
        if shown and used + len(sep) + len(item) > budget:
            break
        shown.append(item)
        used += len(sep) + len(item)
    rest = len(items) - len(shown)
    return sep.join(shown) + (f" and {rest} more" if rest else "")


def _content_hash(path: Path) -> str:
    try:
        if not path.is_file():
            return "missing"
        info = path.stat()
        if info.st_size > MAX_HASH_BYTES:
            return f"size {info.st_size}, mtime {info.st_mtime_ns}"
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return "unreadable"


def _paths(output: Optional[str]) -> Optional[set]:
    return None if output is None else {path for path in output.split("\0") if path}


def uncommitted_hashes(root: Path) -> Optional[Dict[str, str]]:
    """A content hash of every uncommitted file, tracked or new, for the drift
    check; None when there are too many or git can't say."""
    tracked = _paths(run_git(["diff", "--no-renames", "--name-only", "-z", "HEAD"], root))
    untracked = _paths(run_git(["ls-files", "--others", "--exclude-standard", "-z"], root))
    if tracked is None or untracked is None or len(tracked | untracked) > MAX_HASHED:
        return None
    return {path: _content_hash(root / path) for path in sorted(tracked | untracked)}


def changed_files(meta: Dict, root: Path) -> Optional[List[str]]:
    """Files whose content changed since the handoff: committed or not, new,
    deleted or reverted. None for a handoff saved without file hashes."""
    hashes = meta.get("file_hashes")
    if not isinstance(hashes, dict) or not meta.get("head"):
        return None
    differ = _paths(run_git(["diff", "--no-renames", "--name-only", "-z", meta["head"]], root))
    untracked = _paths(run_git(["ls-files", "--others", "--exclude-standard", "-z"], root))
    if differ is None or untracked is None:
        return None
    candidates = differ | untracked
    changed = {path for path in candidates if path not in hashes or hashes[path] != _content_hash(root / path)}
    # Uncommitted at the handoff and now neither different nor new: reverted, or deleted.
    changed.update(path for path in hashes if path not in candidates)
    return sorted(changed)


def edited_hashes(root: Path, edited: List[str]) -> Dict[str, str]:
    """Content hashes of the files a session edited, for the drift check of a folder that git
    doesn't manage: there, they are the only files whose changes can be told."""
    return {path: _content_hash(root / path) for path in edited[-MAX_HASHED:]}


def _renames(meta: Dict, root: Path, changed: List[str]) -> List[Tuple[str, str]]:
    """(old, new) for files renamed since the handoff: those git pairs up, and a deleted file whose
    content now sits under a new, untracked name (a plain `mv`)."""
    found: Dict[str, str] = {}
    status = run_git(["diff", "--name-status", "-M", "-z", meta["head"]], root) or ""
    fields = status.split("\0")
    i = 0
    while i < len(fields):
        code = fields[i]
        if code.startswith(("R", "C")) and i + 2 < len(fields):
            if code.startswith("R"):
                found[fields[i + 1]] = fields[i + 2]
            i += 3
        else:
            i += 2
    gone = [path for path in changed if path not in found and not (root / path).exists()][:MAX_RENAME_PAIRS]
    new = [path for path in changed if path not in found.values() and (root / path).is_file()][:MAX_RENAME_PAIRS]
    if gone and new:
        # What each gone file held: its blob at the handoff's commit, or the hash recorded for it then.
        hashes = meta.get("file_hashes") if isinstance(meta.get("file_hashes"), dict) else {}
        before = {path: "sha1 " + str(hashes[path]) for path in gone if path in hashes}
        listing = run_git(["ls-tree", "-r", "-z", meta["head"], "--", *gone], root) or ""
        for item in listing.split("\0"):
            info, _, path = item.partition("\t")
            if path in gone and path not in before and len(info.split()) == 3:
                before[path] = "blob " + info.split()[2]
        blobs = (run_git(["hash-object", "--stdin-paths"], root, stdin="\n".join(new) + "\n") or "").split()
        for path, blob in zip(new, blobs if len(blobs) == len(new) else [""] * len(new)):
            for old, value in list(before.items()):
                if value in ("blob " + blob, "sha1 " + _content_hash(root / path)):
                    found[old] = path
                    del before[old]
                    break
    return sorted(found.items())


class Drift(NamedTuple):
    notes: List[str]
    changed: List[str]  # files changed since the handoff, when that is known
    renamed: List[Tuple[str, str]] = []  # (old, new) for files renamed since
    # False when the repository's state now could not be compared with the handoff's: the notes say why.
    verified: bool = True


def _unverified(note: str, notes: Optional[List[str]] = None) -> Drift:
    return Drift((notes or []) + [note], [], [], False)


def drift(meta: Dict, root: Path) -> Drift:
    """What changed in the repository since the handoff was saved. When that can't be worked out,
    the result says so (verified is False) instead of reporting no changes."""
    if not meta:
        return _unverified("The state recorded with the handoff is missing, so changes since it can't be checked.")
    nogit = meta.get("edited_hashes")
    if not meta.get("head"):
        now = git_state(root)
        if isinstance(nogit, dict):
            changed = sorted(path for path, digest in nogit.items() if _content_hash(root / path) != digest)
            notes = ["Not a git repository: only the files edited in the earlier session were checked."]
            if changed:
                notes.append(f"Files changed since the handoff ({len(changed)}): {_fit(changed, FILES_BUDGET)}.")
            return Drift(notes, changed, [], True)
        if now is not None and now.head:
            return _unverified("The handoff was saved before this repository had a commit, so changes since can't be checked.")
        return _unverified("The handoff recorded no git state, so changes since it can't be checked.")
    now = git_state(root)
    if now is None:
        return _unverified("The handoff was saved in a git repository, but git can't read this folder now, so changes since can't be checked.")
    notes = []
    if meta.get("branch") and now.branch != meta["branch"]:
        notes.append(f"Branch changed: {_clip(meta['branch'], BRANCH_CHARS)} → {_clip(now.branch, BRANCH_CHARS)}.")
    rewritten = False
    if now.head != meta["head"]:
        log = run_git(["log", "--format=%h %s", f"{meta['head']}..HEAD"], root)
        if log is None:
            rewritten = True
            notes.append(
                f"HEAD has moved to {now.head[:9]} and the handoff's commit {meta['head'][:9]} "
                "is no longer in its history (rebase or reset?)."
            )
        else:
            commits = [_clip(line, ITEM_CHARS) for line in log.splitlines() if line.strip()]
            if commits:
                notes.append(f"{len(commits)} new commit(s) since the handoff: {_fit(commits, COMMITS_BUDGET, '; ')}")
            else:
                notes.append(f"HEAD moved back to {now.head[:9]} (reset or checkout?).")
    if meta.get("dirty") is None:  # not recorded: a handoff restored from an old archive
        return Drift(notes + ["The handoff recorded no uncommitted files, so file changes since can't be checked."], [], [], False)
    before = set(meta["dirty"])
    after = set(now.dirty)
    if before - after:
        gone = [_clip(path, ITEM_CHARS, keep_end=True) for path in sorted(before - after)]
        notes.append(f"No longer uncommitted (committed or reverted): {_fit(gone, DIRTY_BUDGET)}.")
    changed = changed_files(meta, root)
    if changed is None:  # saved without file hashes, or the handoff's commit is gone: compare the git state only
        if after - before:
            notes.append(f"Newly uncommitted: {_fit(sorted(after - before), DIRTY_BUDGET)}.")
        if before == after and before and meta.get("diff_hash") and meta["diff_hash"] != now.diff_hash:
            notes.append("The uncommitted changes were edited after the handoff was saved.")
        if rewritten:
            notes.append("Which files changed can't be told; re-check every claim about files.")
            return Drift(notes, [], [], False)
        return Drift(notes, [])
    renamed = _renames(meta, root, changed) if changed else []
    if renamed:
        listed = _fit([f"{_clip(old, ITEM_CHARS, True)} → {_clip(new, ITEM_CHARS, True)}" for old, new in renamed], FILES_BUDGET)
        notes.append(f"Renamed since the handoff: {listed}.")
    moved = {old for old, _ in renamed} | {new for _, new in renamed}
    others = [path for path in changed if path not in moved]
    if others:
        listed = _fit([_clip(path, ITEM_CHARS, keep_end=True) for path in others], FILES_BUDGET)
        note = f"Files changed since the handoff ({len(others)}): {listed}."
        stat = run_git(["diff", "--shortstat", meta["head"]], root) if not before else None
        if stat and stat.strip():  # with nothing uncommitted at the handoff, this is all since
            note += f" Tracked files: {stat.strip()}."
        notes.append(note)
    return Drift(notes, changed, renamed)
