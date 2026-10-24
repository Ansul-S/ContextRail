"""Per-project files under ~/.contextrail/projects/<name>-<hash>/, outside every
repository.

Everything ContextRail keeps is private to the user on POSIX systems: folders
0700, files 0600. Writes go to a temporary file that replaces the target, and
state changes hold a lock shared by every session open in the project."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import config

SESSION_TTL = 14 * 24 * 3600
MAX_SESSIONS = 50
# A project folder is <readable name>-<SHA-256 of the project's canonical path, cut to 20 hex digits>.
ID_HEX = 20
NAME_CHARS = 40
IDENTITY = "project.json"
FORMAT = 2
PRIVATE_DIR = 0o700
PRIVATE_FILE = 0o600
# The status-line launcher has to stay executable.
PRIVATE_PROGRAM = 0o700
LOCK_TIMEOUT = 10.0
# A lock file of the fallback kind left by a process that died.
STALE_LOCK = 60.0
# The time a saved handoff gives for itself (handoff.render).
SAVED_RE = re.compile(r"^_Saved (\S+), at ", re.M)


def claude_slug(path: Path) -> str:
    """The folder name Claude Code gives a working directory under ~/.claude/projects, which
    ContextRail 0.6 and earlier also used for their own project folders. Distinct paths can
    share it: /work/app/api and /work-app-api both give -work-app-api."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def canonical(root: Path) -> str:
    """The path a project is known by: absolute, with symlinks resolved, and with case folded
    where the platform's paths ignore it (Windows)."""
    return os.path.normcase(os.path.realpath(str(root)))


def project_id(root: Path) -> str:
    """The name of a project's folder: readable, and different for every canonical path."""
    key = canonical(root)
    digest = hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()[:ID_HEX]
    name = re.sub(r"[^A-Za-z0-9._-]", "-", os.path.basename(key.rstrip("\\/")))[:NAME_CHARS].strip(".-")
    return f"{name or 'root'}-{digest}"


class Project:
    def __init__(self, root: Path, base: Optional[Path] = None):
        self.root = root
        self.key = canonical(root)
        self.home = base or config.home()
        self.dir = self.home / "projects" / project_id(root)
        self.handoff = self.dir / "HANDOFF.md"
        self.meta = self.dir / "meta.json"
        self.resume = self.dir / "RESUME_PROMPT.txt"
        self.history = self.dir / "history"
        self.state = self.dir / "state.json"
        self.lock = self.dir / ".lock"
        self.identity = self.dir / IDENTITY

    def legacy_dirs(self) -> List[Path]:
        """Where ContextRail 0.6 and earlier kept this project's files."""
        names: Dict[str, str] = {}
        for name in (claude_slug(self.root), claude_slug(Path(self.key))):
            # On Windows the key is case-folded, so the two names can differ only in case: one folder.
            names.setdefault(os.path.normcase(name), name)
        return [self.home / "projects" / name for name in names.values()]


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _chmod(path: Path, mode: int) -> None:
    if os.name == "nt":
        return  # Windows keeps a profile's files private through its ACLs; chmod only sets read-only
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def make_dir(path: Path) -> None:
    """Create a folder and its missing parents, each private to the user."""
    missing = []
    current = path
    while not current.is_dir():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for folder in reversed(missing):
        try:
            folder.mkdir(mode=PRIVATE_DIR)
        except FileExistsError:
            continue
        _chmod(folder, PRIVATE_DIR)  # mkdir's mode is masked by the umask


def write_atomic(path: Path, text: str, mode: int = PRIVATE_FILE) -> None:
    """Replace path with text in one step: readers see the old file or the new one, never part
    of either. The temporary file is created private (mkstemp), with a name nobody else holds."""
    make_dir(path.parent)
    handle, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if mode != PRIVATE_FILE:
            _chmod(Path(tmp), mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def append_private(path: Path, text: str) -> None:
    """Append to a log file that is created private."""
    make_dir(path.parent)
    handle = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, PRIVATE_FILE)
    with os.fdopen(handle, "a", encoding="utf-8") as f:
        f.write(text)


def read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def secure_home(home: Optional[Path] = None) -> int:
    """Make the data of an existing installation private: folders 0700, files 0600, the launcher
    0700. Symlinks are left alone. Returns how many modes it changed. Cheap when the home folder
    is private already, since then nobody else can reach anything inside it."""
    if os.name == "nt":
        return 0
    home = home or config.home()
    try:
        if home.is_symlink() or home.stat().st_mode & 0o077 == 0:
            return 0
    except OSError:
        return 0
    changed = 0

    def fix(path: str, mode: int) -> None:
        nonlocal changed
        try:
            info = os.lstat(path)
            if (info.st_mode & 0o777) != mode and not os.path.islink(path):
                os.chmod(path, mode)
                changed += 1
        except OSError:
            pass

    launcher = os.path.join(str(home), "bin", "statusline")
    for folder, _folders, files in os.walk(str(home)):
        fix(folder, PRIVATE_DIR)
        for name in files:
            path = os.path.join(folder, name)
            fix(path, PRIVATE_PROGRAM if path == launcher else PRIVATE_FILE)
    return changed


def exposed(home: Optional[Path] = None) -> List[str]:
    """ContextRail's folders and files that other users can read, for the doctor."""
    if os.name == "nt":
        return []
    home = home or config.home()
    found = []
    for folder, _folders, files in os.walk(str(home)):
        for path in [folder] + [os.path.join(folder, name) for name in files]:
            try:
                if not os.path.islink(path) and os.lstat(path).st_mode & 0o077:
                    found.append(path)
            except OSError:
                continue
    return found


class LockTimeout(OSError):
    """Another process held the lock for longer than LOCK_TIMEOUT."""


def _lock_kind() -> str:
    """fcntl on POSIX, msvcrt on Windows, else a file created exclusively."""
    try:
        import fcntl  # noqa: F401
        return "fcntl"
    except ImportError:
        pass
    try:
        import msvcrt  # noqa: F401
        return "msvcrt"
    except ImportError:
        return "exclusive"


def _wait(deadline: float, delay: float, path: Path) -> float:
    if time.monotonic() >= deadline:
        raise LockTimeout(errno.EAGAIN, f"timed out waiting for the lock {path}")
    time.sleep(delay)
    return min(delay * 2, 0.1)


@contextlib.contextmanager
def lock_file(path: Path, timeout: float = LOCK_TIMEOUT) -> Iterator[None]:
    """Hold an exclusive lock on path, shared by every process on the machine that uses it."""
    make_dir(path.parent)
    kind = _lock_kind()
    deadline = time.monotonic() + timeout
    delay = 0.005
    if kind == "exclusive":
        held = path.with_name(path.name + ".held")
        while True:
            try:
                handle = os.open(str(held), os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE)
                break
            except FileExistsError:
                try:
                    if time.time() - held.stat().st_mtime > STALE_LOCK:
                        held.unlink()  # its process died holding it
                        continue
                except OSError:
                    continue
                delay = _wait(deadline, delay, path)
        try:
            os.write(handle, str(os.getpid()).encode())
            os.close(handle)
            yield
        finally:
            with contextlib.suppress(OSError):
                held.unlink()
        return
    handle = os.open(str(path), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), PRIVATE_FILE)
    try:
        if kind == "fcntl":
            import fcntl
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if error.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                        raise
                    delay = _wait(deadline, delay, path)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
        else:
            import msvcrt
            while True:
                try:
                    os.lseek(handle, 0, os.SEEK_SET)
                    msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                    break
                except OSError:
                    delay = _wait(deadline, delay, path)
            try:
                yield
            finally:
                os.lseek(handle, 0, os.SEEK_SET)
                msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
    finally:
        os.close(handle)


def _claim(project: Project, folder: Optional[Path] = None, **extra: Any) -> None:
    """Write the project's identity file, in its folder or one that becomes it: which project
    the folder belongs to."""
    identity = {
        "format": FORMAT,
        "root": str(project.root),
        "canonical": project.key,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        **extra,
    }
    write_atomic((folder or project.dir) / IDENTITY, json.dumps(identity, indent=2) + "\n")


def belongs(project: Project) -> Tuple[bool, str]:
    """Whether the folder holds this project's files, as its identity file says. A folder
    without one (being created, or from before identity files) counts as this project's."""
    if not project.identity.exists():
        return True, ""
    identity = read_json(project.identity)
    owner = identity.get("canonical")
    if not isinstance(owner, str):
        return False, f"{config.tilde(project.identity)} can't be read"
    if owner != project.key:
        return False, f"{config.tilde(project.dir)} belongs to {owner}, not {project.key}"
    return True, ""


@contextlib.contextmanager
def locked(project: Project) -> Iterator[None]:
    """Serialize state changes between sessions open in the same project."""
    with lock_file(project.lock):
        if not project.identity.exists():
            _claim(project)
        yield


def _transcript_root(session_id: Any, legacy: Path) -> Optional[str]:
    """The project of the session that saved a legacy handoff, from the cwd its Claude Code
    transcript records; None when the transcript is gone or says nothing."""
    from . import facts  # facts imports nothing from here; a top-level import would be circular via usage

    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
        return None
    # Usually in the folder of the same name; a session started in a subfolder has its own.
    folders = config.claude_dir() / "projects"
    found = [folders / legacy.name / f"{session_id}.jsonl"] + sorted(folders.glob(f"*/{session_id}.jsonl"))
    path = next((candidate for candidate in found if candidate.is_file()), None)
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for number, line in enumerate(f):
                if number > 200:
                    break
                if '"cwd"' not in line:
                    continue
                try:
                    cwd = json.loads(line).get("cwd")
                except (ValueError, AttributeError):
                    continue
                if isinstance(cwd, str) and cwd:
                    return canonical(facts.project_root(cwd))
    except OSError:
        return None
    return None


def _legacy_owner(project: Project, legacy: Path) -> Tuple[Optional[bool], str]:
    """Whether a folder from ContextRail 0.6 or earlier holds this project's files: True, False,
    or None when nothing says either way. Two kinds of evidence: the cwd of the session that
    saved its handoff, and whether the handoff's commit is in this repository."""
    from . import facts

    meta = read_json(legacy / "meta.json")
    owner = _transcript_root(meta.get("session_id"), legacy)
    if owner is not None:
        if owner == project.key:
            return True, "the session that saved it ran here"
        return False, f"the session that saved it ran in {owner}"
    head = meta.get("head")
    if isinstance(head, str) and re.fullmatch(r"[0-9a-f]{7,64}", head) and facts.run_git(["rev-parse", "--git-dir"], project.root) is not None:
        if facts.run_git(["cat-file", "-e", f"{head}^{{commit}}"], project.root) is None:
            return False, f"its commit {head[:9]} is not in this repository"
        return True, f"its commit {head[:9]} is in this repository"
    return None, "nothing in it names another project"


def adopt_legacy(project: Project) -> Optional[str]:
    """Move this project's folder from ContextRail 0.6 or earlier (named claude_slug(root)) to its
    own folder, once. A legacy folder that evidence ties to another project is left where it is,
    and the decision is recorded so it isn't made again. Returns what happened, or None."""
    if project.dir.exists():
        return None
    candidates = [path for path in project.legacy_dirs() if path.is_dir()]
    if not candidates:
        return None
    for legacy in candidates:
        with lock_file(legacy / ".lock"):
            if project.dir.exists():
                return None  # another session moved it meanwhile
            if read_json(legacy / "MIGRATED.json"):
                continue  # moved for some project already
            owned, why = _legacy_owner(project, legacy)
            if owned is False:
                with lock_file(project.lock):
                    _claim(project, legacy_declined=legacy.name, legacy_reason=why)
                return f"left {config.tilde(legacy)} in place: {why}"
            staging = Path(tempfile.mkdtemp(dir=str(project.home / "projects"), prefix=".adopt-"))
            _chmod(staging, PRIVATE_DIR)
            for entry in legacy.iterdir():
                if entry.name not in (".lock", ".lock.held", IDENTITY):
                    os.replace(entry, staging / entry.name)
            # In place before the folder appears, so a session that opens it next finds it claimed.
            _claim(project, staging, migrated_from=legacy.name, migrated_because=why)
            os.replace(staging, project.dir)
            write_atomic(legacy / "MIGRATED.json", json.dumps({"to": project.dir.name, "root": project.key}) + "\n")
        with contextlib.suppress(OSError):
            (legacy / ".lock").unlink()
            (legacy / "MIGRATED.json").unlink()
            legacy.rmdir()
        return f"moved {config.tilde(legacy)} to {config.tilde(project.dir)} ({why})"
    return None


def open_project(root: Path) -> Project:
    """The project for a root, with a folder from ContextRail 0.6 or earlier moved into place first.
    Problems moving it are left for the doctor to report: opening never fails."""
    project = Project(root)
    if not project.dir.exists():
        try:
            adopt_legacy(project)
        except OSError:
            pass
    return project


def unclaimed_legacy(project: Project) -> List[Path]:
    """Legacy folders this project could have come from that are still in place, for the doctor."""
    identity = read_json(project.identity)
    return [
        path for path in project.legacy_dirs()
        if path.is_dir() and not read_json(path / "MIGRATED.json") and identity.get("migrated_from") != path.name
    ]


def load_state(project: Project) -> Dict[str, Any]:
    state = read_json(project.state)
    if not isinstance(state.get("sessions"), dict):
        state["sessions"] = {}
    # A session entry that isn't an object (a hand edit, a bad write by an old version) is dropped.
    state["sessions"] = {sid: entry for sid, entry in state["sessions"].items() if isinstance(entry, dict)}
    return state


DEFAULT_ENTRY: Dict[str, Any] = {
    "attempts": 0,
    "retries": 0,
    "saved_tokens": None,
    "warned_tokens": None,
    "gave_up": False,
    "deferred_notice": False,
    "start_tokens": None,
    # "checkpoint" or "final": the kind of the latest request, and of the handoff saved for it.
    "asked_kind": None,
    "saved_kind": None,
    # The final handoff was saved while background tasks ran: ask for it again once they finish.
    "saved_running": False,
    # The request waits for the user's answer to the agent's question, and said so once.
    "question_notice": False,
    # The handoff loaded into this session: its sha256 and when, so a repeated SessionStart loads it once.
    "loaded_sha": None,
    "loaded_at": None,
    # Why the latest request was made: the rule and the numbers, for /contextrail:status.
    "trigger": None,
    # The latest turn that ended on an API error (StopFailure), and what it was.
    "failure": None,
}


TOKEN_KEYS = ("saved_tokens", "warned_tokens", "start_tokens")
KINDS = ("checkpoint", "final")


def session(state: Dict[str, Any], session_id: str) -> Dict[str, Any]:
    """The state entry of a session, with every field present and of its type: a value of the
    wrong type (a hand edit, a write by another version) is replaced by its default."""
    entry = state["sessions"].setdefault(session_id, {})
    for key, default in DEFAULT_ENTRY.items():
        value = entry.get(key, default)
        if isinstance(default, bool):
            value = value if isinstance(value, bool) else default
        elif isinstance(default, int):
            value = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default
        elif key in TOKEN_KEYS:
            value = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        elif key in ("asked_kind", "saved_kind"):
            value = value if value in KINDS else None
        entry[key] = value
    return entry


def save_state(project: Project, state: Dict[str, Any]) -> None:
    now = time.time()
    sessions = state["sessions"]
    for entry in sessions.values():
        if not isinstance(entry.get("updated"), (int, float)):
            entry["updated"] = now
    fresh = {sid: e for sid, e in sessions.items() if now - e["updated"] < SESSION_TTL}
    newest = sorted(fresh.items(), key=lambda item: item[1]["updated"], reverse=True)[:MAX_SESSIONS]
    state["sessions"] = dict(newest)
    write_atomic(project.state, json.dumps(state, indent=2))


def archive_order(path: Path) -> Tuple[str, int]:
    """Oldest first: archives are named <stamp>.md, or <stamp>-<n>.md for later
    ones saved in the same second (the stamp is 19 characters)."""
    stem = path.stem
    counter = stem[20:] if stem[19:20] == "-" else ""
    return stem[:19], int(counter) if counter.isdigit() else 0


def archived(project: Project) -> List[Path]:
    """The archived handoffs, oldest first."""
    return sorted(project.history.glob("*.md"), key=archive_order) if project.history.exists() else []


def _archive_current(project: Project, keep: int) -> None:
    """Copy the current handoff and its meta into history/, then prune the oldest. A copy, not a
    move, so the current handoff stays in place until the new one replaces it."""
    try:
        document = project.handoff.read_text(encoding="utf-8")
    except OSError:
        return
    stamp = re.sub(r"[^0-9T]", "-", str(read_json(project.meta).get("saved_at", "")))[:19]
    if not stamp:
        stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime(project.handoff.stat().st_mtime))
    make_dir(project.history)
    # Past every archive of the same second, even one pruned below it, so names keep their order.
    same = [archive_order(path)[1] for path in project.history.glob(f"{stamp}*.md") if path.stem[:19] == stamp]
    target = project.history / (f"{stamp}-{max(same) + 1}.md" if same else f"{stamp}.md")
    if project.meta.exists():  # kept beside it, so a restored handoff gets its drift check
        shutil.copyfile(project.meta, target.with_suffix(".json"))
        _chmod(target.with_suffix(".json"), PRIVATE_FILE)
    write_atomic(target, document)
    older = archived(project)
    for old in older[: max(0, len(older) - keep)]:
        old.unlink()
        old.with_suffix(".json").unlink(missing_ok=True)


def save_handoff(project: Project, document: str, meta: Dict[str, Any], resume: str, keep: int) -> None:
    """Make document the current handoff; call it holding locked(project). The previous one is
    archived first and replaced only at the end, so a process killed part way leaves a usable
    handoff: the old one, or the new one. meta records which document it describes."""
    meta = dict(meta, project=project.key, document_sha256=sha256(document))
    if project.handoff.exists():
        _archive_current(project, keep)
    write_atomic(project.handoff, document)
    write_atomic(project.meta, json.dumps(meta, indent=2))
    if resume:
        write_atomic(project.resume, resume + "\n")


def read_handoff(project: Project) -> Tuple[Optional[str], Dict[str, Any], List[str]]:
    """The current handoff, its meta, and problems that make the meta unusable. The document is
    None when there is none, or when the folder or the meta belongs to another project."""
    owned, problem = belongs(project)
    if not owned:
        return None, {}, [problem]
    try:
        document = project.handoff.read_text(encoding="utf-8")
    except OSError:
        return None, {}, []
    meta = read_json(project.meta)
    owner = meta.get("project")
    if isinstance(owner, str) and owner != project.key:
        return None, {}, [f"its meta.json names another project, {owner}"]
    recorded = meta.get("document_sha256")
    if isinstance(recorded, str) and recorded != sha256(document):
        # Saved without its meta (a process stopped between the two writes), or edited by hand.
        saved = SAVED_RE.search(document)
        return document, {"saved_at": saved.group(1) if saved else None}, ["meta.json describes a different handoff"]
    return document, meta, []
