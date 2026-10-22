"""Facts recorded by script rather than written by the model: git state,
files edited in the session, recent test/build commands, and drift since
the last handoff."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
CHECK_RE = re.compile(
    r"\b(pytest|unittest|tox|nox|vitest|jest|mocha|playwright|cypress|rspec|phpunit|"
    r"ruff|mypy|pyright|eslint|tsc|"
    r"(npm|pnpm|yarn|bun)( run)? (test|build|lint|typecheck|check|e2e)|"
    r"go (test|build|vet)|cargo (test|build|check|clippy)|swift (test|build)|"
    r"dotnet (test|build)|mvn|gradle|make)\b"
)
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
    edited: List[str] = field(default_factory=list)
    checks: List[Tuple[str, Optional[bool]]] = field(default_factory=list)  # (command, passed)


def _display_path(path: str, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(root.resolve()))
    except (ValueError, OSError):
        home = str(Path.home())
        return "~" + path[len(home):] if path.startswith(home + "/") else path


def session_activity(transcript_path: Optional[str], root: Path) -> Activity:
    edited: Dict[str, None] = {}
    commands: Dict[str, str] = {}  # tool_use_id -> command
    passed: Dict[str, bool] = {}
    if not transcript_path:
        return Activity()
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
                                shown = _display_path(path, root)
                                edited.pop(shown, None)
                                edited[shown] = None
                        elif name == "Bash":
                            command = args.get("command")
                            if isinstance(command, str) and CHECK_RE.search(command):
                                commands[str(block.get("id"))] = command
                    elif block.get("type") == "tool_result":
                        tool_id = str(block.get("tool_use_id"))
                        if tool_id in commands:
                            passed[tool_id] = not block.get("is_error", False)
    except OSError:
        return Activity()
    latest: Dict[str, Optional[bool]] = {}
    for tool_id, command in commands.items():
        one_line = " ".join(command.split())
        if len(one_line) > 120:
            one_line = one_line[:117] + "..."
        latest.pop(one_line, None)
        latest[one_line] = passed.get(tool_id)
    return Activity(
        edited=list(edited)[-MAX_LISTED:],
        checks=list(latest.items())[-MAX_CHECKS:],
    )


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
