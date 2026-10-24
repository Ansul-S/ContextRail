import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from contextrail import store
from tests.helpers import ROOT, TempHome, git, make_repo, write_transcript

POSIX = os.name != "nt"


def save(project, document, saved_at="2026-01-01T10:00:00+00:00", keep=10):
    with store.locked(project):
        store.save_handoff(project, document, {"saved_at": saved_at}, "resume", keep)


def mode(path):
    return path.stat().st_mode & 0o777


class ProjectIdTest(unittest.TestCase):
    def test_paths_that_share_a_legacy_slug_get_their_own_folders(self):
        nested, flat = Path("/work/app/api"), Path("/work-app-api")
        self.assertEqual(store.claude_slug(nested), store.claude_slug(flat))
        self.assertNotEqual(store.project_id(nested), store.project_id(flat))
        self.assertRegex(store.project_id(nested), r"^api-[0-9a-f]{20}$")
        self.assertRegex(store.project_id(flat), r"^work-app-api-[0-9a-f]{20}$")

    def test_equivalent_paths_share_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real"
            (real / "sub").mkdir(parents=True)
            link = Path(tmp) / "link"
            link.symlink_to(real, target_is_directory=True)
            same = {store.project_id(path) for path in (real, link, real / "sub" / "..", Path(str(real) + os.sep))}
            self.assertEqual(len(same), 1)
            self.assertNotEqual(store.project_id(real / "sub"), store.project_id(real))

    def test_the_readable_part_is_safe_as_a_folder_name(self):
        self.assertRegex(store.project_id(Path("/x/we ird:na*me")), r"^we-ird-na-me-[0-9a-f]{20}$")
        self.assertRegex(store.project_id(Path("/")), r"^root-[0-9a-f]{20}$")
        self.assertRegex(store.project_id(Path("/x/..hidden")), r"^hidden-[0-9a-f]{20}$")


class IdentityTest(TempHome):
    def test_projects_that_shared_a_folder_before_keep_their_own_data(self):
        nested = make_repo(self.tmp / "work" / "app" / "api")
        flat = make_repo(self.tmp / "work-app-api")
        first, second = store.open_project(nested), store.open_project(flat)
        save(first, "nested handoff")
        save(second, "flat handoff")
        self.assertNotEqual(first.dir, second.dir)
        self.assertEqual(store.read_handoff(first)[0], "nested handoff")
        self.assertEqual(store.read_handoff(second)[0], "flat handoff")
        self.assertEqual(store.read_json(first.identity)["canonical"], first.key)
        self.assertEqual(store.read_json(first.meta)["project"], first.key)

    def test_a_folder_claimed_by_another_project_is_not_read(self):
        project = store.open_project(self.repo)
        save(project, "handoff")
        identity = store.read_json(project.identity)
        identity["canonical"] = "/somewhere/else"
        project.identity.write_text(json.dumps(identity), encoding="utf-8")
        document, meta, problems = store.read_handoff(project)
        self.assertIsNone(document)
        self.assertIn("belongs to /somewhere/else", problems[0])
        # Taking the lock never overwrites another project's claim.
        with store.locked(project):
            pass
        self.assertEqual(store.read_json(project.identity)["canonical"], "/somewhere/else")

    def test_meta_naming_another_project_is_not_used(self):
        project = store.open_project(self.repo)
        save(project, "handoff")
        meta = store.read_json(project.meta)
        project.meta.write_text(json.dumps(dict(meta, project="/somewhere/else")), encoding="utf-8")
        self.assertEqual(store.read_handoff(project), (None, {}, ["its meta.json names another project, /somewhere/else"]))

    def test_meta_of_another_document_is_dropped_but_the_document_kept(self):
        project = store.open_project(self.repo)
        save(project, "_Saved 2026-01-01T10:00:00+00:00, at 5 tokens._\nfirst")
        project.handoff.write_text("_Saved 2026-01-02T10:00:00+00:00, at 5 tokens._\nedited", encoding="utf-8")
        document, meta, problems = store.read_handoff(project)
        self.assertIn("edited", document)
        self.assertEqual(meta, {"saved_at": "2026-01-02T10:00:00+00:00"})
        self.assertEqual(problems, ["meta.json describes a different handoff"])


class LegacyTest(TempHome):
    """Folders from ContextRail 0.6 and earlier, named after Claude Code's slug of the path."""

    def legacy(self, root, meta):
        folder = self.home / "projects" / store.claude_slug(root)
        (folder / "history").mkdir(parents=True)
        (folder / "HANDOFF.md").write_text("old handoff", encoding="utf-8")
        (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        (folder / "state.json").write_text(json.dumps({"sessions": {"s": {"attempts": 1}}}), encoding="utf-8")
        (folder / "history" / "2026-01-01T10-00-00.md").write_text("older handoff", encoding="utf-8")
        return folder

    def session_ran_in(self, cwd, session_id="legacy-session"):
        folder = self.claude / "projects" / store.claude_slug(cwd)
        folder.mkdir(parents=True, exist_ok=True)
        write_transcript(folder / f"{session_id}.jsonl", [{"type": "user", "cwd": str(cwd), "message": {"content": "hi"}}])
        return session_id

    def test_moved_once_when_its_commit_is_in_this_repository(self):
        head = git(self.repo, "rev-parse", "HEAD").strip()
        folder = self.legacy(self.repo, {"saved_at": "2026-01-01T10:00:00+00:00", "head": head[:9]})
        project = store.open_project(self.repo)
        self.assertFalse(folder.exists())
        self.assertEqual(project.handoff.read_text(encoding="utf-8"), "old handoff")
        self.assertEqual([p.name for p in store.archived(project)], ["2026-01-01T10-00-00.md"])
        self.assertEqual(store.load_state(project)["sessions"]["s"]["attempts"], 1)
        identity = store.read_json(project.identity)
        self.assertEqual(identity["migrated_from"], folder.name)
        self.assertEqual(identity["canonical"], project.key)
        self.assertIn("is in this repository", identity["migrated_because"])
        self.assertEqual(store.read_handoff(project)[0], "old handoff")
        self.assertEqual(store.unclaimed_legacy(project), [])
        # Opening it again moves nothing and changes nothing.
        before = project.identity.read_text(encoding="utf-8")
        self.assertIsNone(store.adopt_legacy(store.open_project(self.repo)))
        self.assertEqual(project.identity.read_text(encoding="utf-8"), before)

    def test_moved_when_the_session_that_saved_it_ran_here(self):
        sid = self.session_ran_in(self.repo)
        folder = self.legacy(self.repo, {"session_id": sid})
        project = store.open_project(self.repo)
        self.assertFalse(folder.exists())
        self.assertEqual(store.read_json(project.identity)["migrated_because"], "the session that saved it ran here")

    def test_one_folder_when_the_names_differ_only_in_case(self):
        root = self.tmp / "MyApp"
        root.mkdir()
        with mock.patch("os.path.normcase", str.lower):  # Windows folds the case of the canonical path
            project = store.Project(root)
            self.assertEqual(project.legacy_dirs(), [self.home / "projects" / store.claude_slug(root)])

    def test_left_for_the_project_whose_session_saved_it(self):
        nested = make_repo(self.tmp / "work" / "app" / "api")
        flat = make_repo(self.tmp / "work-app-api")
        folder = self.legacy(nested, {"session_id": self.session_ran_in(nested)})
        self.assertEqual(folder.name, store.claude_slug(flat))

        other = store.open_project(flat)
        self.assertTrue(folder.exists())
        self.assertEqual(store.read_handoff(other)[0], None)
        self.assertEqual(store.unclaimed_legacy(other), [folder])
        identity = store.read_json(other.identity)
        self.assertEqual(identity["legacy_declined"], folder.name)
        self.assertIn(f"ran in {store.canonical(nested)}", identity["legacy_reason"])

        owner = store.open_project(nested)
        self.assertFalse(folder.exists())
        self.assertEqual(store.read_handoff(owner)[0], "old handoff")

    def test_left_in_place_when_its_commit_is_not_in_this_repository(self):
        folder = self.legacy(self.repo, {"head": "deadbeef" * 5})
        project = store.open_project(self.repo)
        self.assertTrue(folder.exists())
        self.assertIsNone(store.read_handoff(project)[0])
        self.assertIn("its commit deadbeefd is not in this repository", store.read_json(project.identity)["legacy_reason"])

    def test_nothing_to_tell_moves_it(self):
        folder = self.legacy(self.repo, {})
        project = store.open_project(self.repo)
        self.assertFalse(folder.exists())
        self.assertEqual(store.read_json(project.identity)["migrated_because"], "nothing in it names another project")

    def test_a_failed_move_never_fails_opening(self):
        self.legacy(self.repo, {})
        with mock.patch.object(store.os, "replace", side_effect=PermissionError(13, "denied")):
            project = store.open_project(self.repo)
        self.assertEqual(store.read_handoff(project)[0], None)


@unittest.skipUnless(POSIX, "file modes are POSIX")
class PrivacyTest(TempHome):
    def setUp(self):
        super().setUp()
        self._umask = os.umask(0o022)

    def tearDown(self):
        os.umask(self._umask)
        super().tearDown()

    def test_new_folders_and_files_are_private(self):
        project = store.open_project(self.repo)
        save(project, "first")
        save(project, "second")
        state = store.load_state(project)
        store.session(state, "s")
        store.save_state(project, state)
        store.append_private(self.home / "error.log", "x\n")
        for folder in (self.home, self.home / "projects", project.dir, project.history):
            self.assertEqual(mode(folder), store.PRIVATE_DIR, folder)
        for path in [project.handoff, project.meta, project.resume, project.state, project.lock, project.identity,
                     self.home / "error.log", *project.history.iterdir()]:
            self.assertEqual(mode(path), store.PRIVATE_FILE, path)
        self.assertEqual(store.exposed(), [])

    def test_an_older_installation_is_made_private_once(self):
        projects = self.home / "projects" / "old"
        projects.mkdir(parents=True)
        for folder in (self.home, self.home / "projects", projects):
            folder.chmod(0o755)
        (projects / "HANDOFF.md").write_text("handoff", encoding="utf-8")
        (projects / "HANDOFF.md").chmod(0o644)
        (self.home / "bin").mkdir()
        launcher = self.home / "bin" / "statusline"
        launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        launcher.chmod(0o755)
        outside = self.tmp / "not-ours.txt"
        outside.write_text("x", encoding="utf-8")
        outside.chmod(0o644)
        (self.home / "link").symlink_to(outside)

        self.assertGreater(len(store.exposed()), 0)
        self.assertGreater(store.secure_home(), 0)
        self.assertEqual(store.exposed(), [])
        self.assertEqual(mode(projects), store.PRIVATE_DIR)
        self.assertEqual(mode(projects / "HANDOFF.md"), store.PRIVATE_FILE)
        self.assertEqual(mode(launcher), store.PRIVATE_PROGRAM)
        self.assertTrue(os.access(launcher, os.X_OK))
        self.assertEqual(mode(outside), 0o644)  # a symlink's target is left alone
        self.assertEqual(store.secure_home(), 0)

    def test_a_failed_write_leaves_the_old_file_and_no_temporary_file(self):
        path = self.home / "data.json"
        store.write_atomic(path, "old")
        with mock.patch.object(store.os, "replace", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                store.write_atomic(path, "new")
        self.assertEqual(path.read_text(encoding="utf-8"), "old")
        self.assertEqual([p.name for p in self.home.iterdir()], ["data.json"])


# Counts to ROUNDS in a JSON file under the lock, slowly enough that unlocked writers would lose counts.
COUNTER = """
import json, sys, time
from pathlib import Path
from contextrail import store
if sys.argv[3] == "exclusive":
    store._lock_kind = lambda: "exclusive"
path = Path(sys.argv[1])
for _ in range(int(sys.argv[2])):
    with store.lock_file(path.with_name("counter.lock")):
        count = json.loads(path.read_text(encoding="utf-8")) if path.exists() else 0
        time.sleep(0.002)
        store.write_atomic(path, json.dumps(count + 1))
"""

# Saves ROUNDS handoffs for one project, each named after this process and round.
SAVER = """
import sys
from pathlib import Path
from contextrail import store
project = store.open_project(Path(sys.argv[1]))
for n in range(int(sys.argv[2])):
    with store.locked(project):
        store.save_handoff(project, f"handoff {sys.argv[3]}-{n}", {"saved_at": "2026-01-01T10:00:00+00:00"}, "", 1000)
"""


class LockTest(TempHome):
    def processes(self, script, *args, count=4):
        """Run script in count processes at once; each gets args and then its own number."""
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        running = [
            subprocess.Popen([sys.executable, "-c", script, *args, str(n)], env=env, stderr=subprocess.PIPE)
            for n in range(count)
        ]
        for process in running:
            _, error = process.communicate(timeout=120)
            self.assertEqual(process.returncode, 0, error.decode())

    def test_writers_in_separate_processes_lose_nothing(self):
        kinds = ["exclusive"] + ([store._lock_kind()] if store._lock_kind() != "exclusive" else [])
        for kind in kinds:
            with self.subTest(kind=kind):
                path = self.home / f"counter-{kind}.json"
                self.processes(COUNTER, str(path), "25", kind)
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")), 100)

    def test_concurrent_saves_keep_every_handoff(self):
        self.processes(SAVER, str(self.repo), "5")
        project = store.open_project(self.repo)
        document, meta, problems = store.read_handoff(project)
        self.assertEqual(problems, [])
        kept = [document] + [path.read_text(encoding="utf-8") for path in store.archived(project)]
        self.assertEqual(sorted(kept), sorted(f"handoff {p}-{n}" for p in range(4) for n in range(5)))
        self.assertEqual(meta["document_sha256"], store.sha256(document))
        self.assertEqual(list(project.dir.glob(".*.tmp")) + list(project.history.glob(".*.tmp")), [])

    def test_a_lock_held_too_long_times_out(self):
        for kind in ("exclusive", store._lock_kind()):
            with self.subTest(kind=kind), mock.patch.object(store, "_lock_kind", return_value=kind):
                path = self.home / f"{kind}.lock"
                with store.lock_file(path):
                    started = time.monotonic()
                    with self.assertRaises(store.LockTimeout):
                        with store.lock_file(path, timeout=0.1):
                            pass
                    self.assertLess(time.monotonic() - started, 2)
                with store.lock_file(path, timeout=0.1):
                    pass  # free again

    def test_a_fallback_lock_left_by_a_dead_process_is_taken_over(self):
        path = self.home / "x.lock"
        held = path.with_name("x.lock.held")
        self.home.mkdir(parents=True)
        held.write_text("12345", encoding="utf-8")
        old = time.time() - store.STALE_LOCK - 5
        os.utime(held, (old, old))
        with mock.patch.object(store, "_lock_kind", return_value="exclusive"):
            with store.lock_file(path, timeout=0.5):
                self.assertEqual(held.read_text(encoding="utf-8"), str(os.getpid()))
        self.assertFalse(held.exists())


class CrashSafeSaveTest(TempHome):
    """A process stopped part way through a save leaves a usable handoff and keeps the previous one."""

    def failing_on(self, name):
        real = store.write_atomic

        def write(path, text, mode=store.PRIVATE_FILE):
            if Path(path).name == name:
                raise KeyboardInterrupt  # what a hook stopped by its timeout sees
            real(path, text, mode)
        return mock.patch.object(store, "write_atomic", side_effect=write)

    def test_stopped_before_the_new_handoff_is_written(self):
        project = store.open_project(self.repo)
        save(project, "first")
        with self.failing_on("HANDOFF.md"), self.assertRaises(KeyboardInterrupt):
            save(project, "second", saved_at="2026-01-02T10:00:00+00:00")
        self.assertEqual(store.read_handoff(project)[0], "first")
        self.assertEqual(store.read_handoff(project)[2], [])
        self.assertEqual([p.read_text(encoding="utf-8") for p in store.archived(project)], ["first"])

    def test_stopped_between_the_handoff_and_its_meta(self):
        project = store.open_project(self.repo)
        save(project, "_Saved 2026-01-01T10:00:00+00:00, at 5 tokens._\nfirst")
        with self.failing_on("meta.json"), self.assertRaises(KeyboardInterrupt):
            save(project, "_Saved 2026-01-02T10:00:00+00:00, at 5 tokens._\nsecond")
        document, meta, problems = store.read_handoff(project)
        self.assertIn("second", document)
        self.assertEqual(meta, {"saved_at": "2026-01-02T10:00:00+00:00"})
        self.assertEqual(problems, ["meta.json describes a different handoff"])
        self.assertIn("first", store.archived(project)[0].read_text(encoding="utf-8"))

    def test_the_lock_is_released_when_a_save_fails(self):
        project = store.open_project(self.repo)
        with self.failing_on("HANDOFF.md"), self.assertRaises(KeyboardInterrupt):
            save(project, "first")
        with store.lock_file(project.lock, timeout=0.1):
            pass


if __name__ == "__main__":
    unittest.main()
