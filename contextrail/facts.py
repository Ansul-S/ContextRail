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
from typing import Any, Dict, List, Optional, Tuple

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


def run_git(args: List[str], cwd: Path) -> Optional[str]:
    """Raw stdout of a git command, or None if git fails or is missing."""
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=5
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


def git_state(root: Path) -> Optional[GitState]:
    if run_git(["rev-parse", "--git-dir"], root) is None:
        return None
    head = (run_git(["rev-parse", "HEAD"], root) or "").strip()
    branch = (run_git(["rev-parse", "--abbrev-ref", "HEAD"], root) or "").strip()
    subject = (run_git(["log", "-1", "--format=%s"], root) or "").strip() if head else ""
    status = run_git(["status", "--porcelain=v1", "--untracked-files=normal"], root) or ""
    dirty = [line[3:] for line in status.splitlines() if len(line) > 3]
    diff = run_git(["diff", "HEAD"], root) if head else ""
    diff_hash = hashlib.sha1((diff or "").encode("utf-8", "replace")).hexdigest()
    return GitState(branch=branch, head=head, subject=subject, dirty=dirty, diff_hash=diff_hash)


@dataclass
class Activity:
    edited: List[str] = field(default_factory=list)  # inside the project, latest edit last
    outside: int = 0  # files edited outside the project
    checks: List[Tuple[str, str]] = field(default_factory=list)  # (command, result word)


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


def check_result(check: Check, block: Dict[str, Any]) -> str:
    """passed, failed, ran (finished, result not visible) or result unknown."""
    if check.detached:
        return "result unknown"
    text = _result_text(block.get("content"))
    failed = bool(block.get("is_error"))
    if failed and not text.startswith("Exit code"):
        return "result unknown"  # denied, blocked or stopped before it finished
    if check.exit_status:
        return "failed" if failed else "passed"
    return output_verdict(text) or "ran"


def _inside(path: str, root: Path) -> Optional[str]:
    """path relative to the project root, or None if it is outside the project."""
    try:
        return str(Path(path).resolve().relative_to(root))
    except (ValueError, OSError):
        return None


def session_activity(transcript_path: Optional[str], root: Path) -> Activity:
    edited: Dict[str, None] = {}
    outside = set()
    checks: Dict[str, Check] = {}  # tool_use_id -> check
    results: Dict[str, str] = {}
    if not transcript_path:
        return Activity()
    root = root.resolve()
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"tool_use"' not in line and '"tool_result"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("isSidechain"):
                    continue
                content = (entry.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        name = block.get("name")
                        args = block.get("input") or {}
                        if name in EDIT_TOOLS:
                            path = args.get("file_path") or args.get("notebook_path")
                            if isinstance(path, str):
                                relative = _inside(path, root)
                                if relative is None:
                                    outside.add(path)
                                else:
                                    edited.pop(relative, None)
                                    edited[relative] = None
                        elif name == "Bash":
                            command = args.get("command")
                            check = find_check(command) if isinstance(command, str) else None
                            if check and args.get("run_in_background"):
                                check = replace(check, detached=True)
                            if check:
                                checks[str(block.get("id"))] = check
                    elif block.get("type") == "tool_result":
                        tool_id = str(block.get("tool_use_id"))
                        if tool_id in checks:
                            results[tool_id] = check_result(checks[tool_id], block)
    except OSError:
        return Activity()
    latest: Dict[str, str] = {}
    for tool_id, check in checks.items():
        latest.pop(check.command, None)
        latest[check.command] = results.get(tool_id, "result unknown")
    return Activity(edited=list(edited), outside=len(outside), checks=list(latest.items())[-MAX_CHECKS:])


def _short_list(items: List[str]) -> str:
    shown = ", ".join(items[:8])
    return shown + (f" and {len(items) - 8} more" if len(items) > 8 else "")


def drift(meta: Dict, root: Path) -> List[str]:
    """What changed in the repository since the handoff was saved."""
    if not meta.get("head"):
        return []
    now = git_state(root)
    if now is None:
        return []
    notes = []
    if meta.get("branch") and now.branch != meta["branch"]:
        notes.append(f"Branch changed: {meta['branch']} → {now.branch}.")
    if now.head != meta["head"]:
        log = run_git(["log", "--format=%h %s", f"{meta['head']}..HEAD"], root)
        if log is None:
            notes.append(
                f"HEAD has moved to {now.head[:9]} and the handoff's commit {meta['head'][:9]} "
                "is no longer in its history (rebase or reset?)."
            )
        else:
            commits = [line for line in log.splitlines() if line.strip()]
            if commits:
                listed = "; ".join(commits[:5]) + ("; ..." if len(commits) > 5 else "")
                notes.append(f"{len(commits)} new commit(s) since the handoff: {listed}")
            else:
                notes.append(f"HEAD moved back to {now.head[:9]} (reset or checkout?).")
    before = set(meta.get("dirty") or [])
    after = set(now.dirty)
    if before - after:
        notes.append(f"No longer uncommitted (committed or reverted): {_short_list(sorted(before - after))}.")
    if after - before:
        notes.append(f"Newly uncommitted: {_short_list(sorted(after - before))}.")
    if before == after and before and meta.get("diff_hash") and meta["diff_hash"] != now.diff_hash:
        notes.append("The uncommitted changes were edited after the handoff was saved.")
    return notes
