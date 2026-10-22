import unittest
from unittest import mock

from contextrail import facts
from tests.helpers import TempHome, git, tool_result, tool_use, user, write_transcript


class GitStateTest(TempHome):
    def test_clean_then_dirty(self):
        state = facts.git_state(self.repo)
        self.assertEqual(state.branch, "main")
        self.assertEqual(state.subject, "initial commit")
        self.assertEqual(state.dirty, [])
        (self.repo / "app.py").write_text("print('changed')\n")
        (self.repo / "new.txt").write_text("n\n")
        self.assertEqual(sorted(facts.git_state(self.repo).dirty), ["app.py", "new.txt"])

    def test_not_a_repo(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertIsNone(facts.git_state(plain))
        self.assertEqual(facts.project_root(str(plain)), plain)

    def test_project_root_from_subdirectory(self):
        sub = self.repo / "pkg" / "deep"
        sub.mkdir(parents=True)
        self.assertEqual(facts.project_root(str(sub)), self.repo)


class ActivityTest(TempHome):
    def activity(self, *entries):
        write_transcript(self.transcript, [user(), *entries])
        return facts.session_activity(str(self.transcript), self.repo)

    def bash(self, tool_id, command, text=None, is_error=False, **args):
        return [tool_use(tool_id, "Bash", {"command": command, **args}), tool_result(tool_id, is_error, text)]

    def test_edits_and_checks(self):
        activity = self.activity(
            tool_use("e1", "Edit", {"file_path": str(self.repo / "app.py")}),
            tool_use("e2", "Write", {"file_path": str(self.repo / "lib.py")}),
            tool_use("e3", "Edit", {"file_path": str(self.repo / "app.py")}),
            *self.bash("b1", "pytest -q tests/", is_error=True),
            *self.bash("b2", "ls -la"),
            *self.bash("b3", "pytest -q tests/"),
            tool_use("b4", "Bash", {"command": "pnpm build"}),
        )
        self.assertEqual(activity.edited, ["lib.py", "app.py"])
        self.assertEqual(activity.checks, [("pytest -q tests/", "passed"), ("pnpm build", "result unknown")])

    def test_heredoc_body_is_not_a_check(self):
        command = "cat > scripts/ci.sh <<'EOF'\nset -e\npython -m unittest discover\nEOF\nchmod +x scripts/ci.sh"
        self.assertEqual(self.activity(*self.bash("b1", command)).checks, [])

    def test_failed_summary_behind_a_pipe(self):
        command = f"cd {self.repo} && python3 -m unittest discover -s tests -t . 2>&1 | tail -3"
        output = "Ran 12 tests in 0.4s\n\nFAILED (failures=1)"
        checks = self.activity(*self.bash("b1", command, output)).checks
        self.assertEqual(checks, [("python3 -m unittest discover -s tests -t .", "failed")])

    def test_pytest_summary(self):
        for output, expected in (("=== 2 failed, 40 passed in 1.21s ===", "failed"), ("42 passed, 1 skipped in 0.9s", "passed")):
            with self.subTest(output=output):
                checks = self.activity(*self.bash("b1", "pytest -q | tail -1", output)).checks
                self.assertEqual(checks, [("pytest -q", expected)])

    def test_pipe_without_a_summary_only_ran(self):
        self.assertEqual(self.activity(*self.bash("b1", "pnpm build | tail -5", "done")).checks, [("pnpm build", "ran")])

    def test_output_decides_when_the_exit_status_is_masked(self):
        checks = self.activity(*self.bash("b1", "pytest -q || true", "1 failed, 2 passed in 0.1s")).checks
        self.assertEqual(checks, [("pytest -q", "failed")])

    def test_exit_status_decides_without_a_pipe(self):
        checks = self.activity(
            *self.bash("b1", "cd sub && npm test", is_error=True),
            *self.bash("b2", "make lint && echo done", "no summary here"),
        ).checks
        self.assertEqual(checks, [("npm test", "failed"), ("make lint", "passed")])

    def test_commands_that_did_not_run_or_finish(self):
        checks = self.activity(
            *self.bash("b1", "pytest -q", "Permission to use Bash with command pytest -q has been denied.", is_error=True),
            *self.bash("b2", "npm run e2e", "Command running in background with ID: b2.", run_in_background=True),
            tool_use("b3", "Bash", {"command": "cargo test"}),
        ).checks
        self.assertEqual(
            checks, [("pytest -q", "result unknown"), ("npm run e2e", "result unknown"), ("cargo test", "result unknown")]
        )

    def test_content_blocks_and_long_commands(self):
        output = [{"type": "text", "text": "Ran 3 tests in 0.1s\n\nOK"}]
        command = "python3 -m unittest tests/" + "x" * 100 + " 2>&1 | tail -1; python3 -m unittest"
        [(shown, result)] = self.activity(*self.bash("b1", command, output)).checks
        self.assertEqual(result, "passed")
        self.assertEqual(shown, "python3 -m unittest tests/" + "x" * 53 + "… (+1 more)")

    def test_files_outside_the_project_are_counted(self):
        activity = self.activity(
            tool_use("e1", "Edit", {"file_path": str(self.repo / "app.py")}),
            tool_use("e2", "Write", {"file_path": str(self.tmp / "notes.md")}),
            tool_use("e3", "Edit", {"file_path": str(self.tmp / "notes.md")}),
            tool_use("e4", "Edit", {"file_path": "/elsewhere/x.py"}),
        )
        self.assertEqual((activity.edited, activity.outside), (["app.py"], 2))

    def test_missing_transcript(self):
        self.assertEqual(facts.session_activity(str(self.tmp / "nope.jsonl"), self.repo), facts.Activity())


class CommandTest(unittest.TestCase):
    def test_finds_the_check_in_a_command(self):
        cases = {
            "cd /repo && CI=1 pnpm --filter web build": "CI=1 pnpm --filter web build",
            "(cd backend && uv run --with pytest-cov pytest -q)": "uv run --with pytest-cov pytest -q",
            "if pytest -q; then echo ok; fi": "pytest -q",
            "# don't skip the tests\npytest -q": "pytest -q",
            "/usr/bin/python3 -m unittest discover": "/usr/bin/python3 -m unittest discover",
            "npx -p typescript tsc --noEmit": "npx -p typescript tsc --noEmit",
            "yarn workspace web test": "yarn workspace web test",
            "go test ./... | tee log": "go test ./...",
            "make -C sub test 2>&1": "make -C sub test",
            "ruff format --check .": "ruff format --check .",
            "mvn -q clean install": "mvn -q clean install",
            "git commit -m 'run pytest; then make'": None,
            "python3 -c 'import unittest'": None,
            "test -d build || mkdir build": None,
            "npm ci && npm run dev": None,
            "ruff format app": None,
            "make web": None,
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                check = facts.find_check(command)
                self.assertEqual(check.command if check else None, expected)

    def test_when_the_exit_status_is_the_checks(self):
        cases = {
            "pytest": True,
            "cd x && pytest && git status": True,
            "pytest | tail -3": False,
            "pytest || true": False,
            "pytest; echo done": False,
            "pytest\nls": False,
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(facts.find_check(command).exit_status, expected)
        self.assertTrue(facts.find_check("pytest &").detached)

    def test_output_verdict(self):
        cases = {
            "Ran 3 tests in 0.1s\n\nOK": "passed",
            "Ran 3 tests in 0.1s\n\nFAILED (errors=2)": "failed",
            "ok  \tpkg/a\t0.1s\nFAIL\tpkg/b\t0.2s\nFAIL": "failed",
            "Tests:       1 failed, 5 passed, 6 total": "failed",
            "test result: ok. 8 passed; 0 failed; 0 ignored": "passed",
            "Found 3 errors in 2 files (checked 9 source files)": "failed",
            "All checks passed!": "passed",
            "FAILED (failures=1)\nRan 3 tests in 0.1s\n\nOK": None,  # two runs that disagree
            "built in 2.1s": None,
        }
        for output, expected in cases.items():
            with self.subTest(output=output):
                self.assertEqual(facts.output_verdict(output), expected)


class DriftTest(TempHome):
    def meta(self):
        state = facts.git_state(self.repo)
        return {"branch": state.branch, "head": state.head, "dirty": state.dirty, "diff_hash": state.diff_hash}

    def test_no_changes(self):
        self.assertEqual(facts.drift(self.meta(), self.repo).notes, [])

    def test_new_commits_and_dirty_files(self):
        meta = self.meta()
        (self.repo / "app.py").write_text("print('v2')\n")
        git(self.repo, "commit", "-qam", "second change")
        (self.repo / "todo.txt").write_text("x\n")
        notes = facts.drift(meta, self.repo).notes
        self.assertEqual(len(notes), 2)
        self.assertIn("1 new commit(s) since the handoff", notes[0])
        self.assertIn("second change", notes[0])
        self.assertEqual(notes[1], "Newly uncommitted: todo.txt.")

    def test_uncommitted_edits_changed(self):
        (self.repo / "app.py").write_text("print('wip')\n")
        meta = self.meta()
        (self.repo / "app.py").write_text("print('more wip')\n")
        self.assertEqual(facts.drift(meta, self.repo).notes, ["The uncommitted changes were edited after the handoff was saved."])

    def hashed_meta(self):
        meta = self.meta()
        meta["file_hashes"] = facts.uncommitted_hashes(self.repo)
        return meta

    def test_reload_right_after_a_handoff_with_uncommitted_work_is_quiet(self):
        (self.repo / "app.py").write_text("print('wip')\n")
        (self.repo / "notes.txt").write_text("todo\n")
        found = facts.drift(self.hashed_meta(), self.repo)
        self.assertEqual(found, facts.Drift([], []))

    def test_changed_files_by_content(self):
        for name in ("edited.py", "reverted.py", "committed.py", "clean.py"):
            (self.repo / name).write_text(f"# {name}\n")
        (self.repo / ".gitignore").write_text("*.log\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "files")
        for name in ("edited.py", "reverted.py", "committed.py"):
            (self.repo / name).write_text(f"# {name}, uncommitted at the handoff\n")
        for name in ("deleted.txt", "kept.txt"):
            (self.repo / name).write_text("untracked\n")
        meta = self.hashed_meta()
        (self.repo / "edited.py").write_text("# edited again\n")
        git(self.repo, "checkout", "--", "reverted.py")
        git(self.repo, "commit", "-qm", "commit as it was", "committed.py")
        (self.repo / "deleted.txt").unlink()
        (self.repo / "clean.py").write_text("# changed after the handoff\n")
        (self.repo / "new.txt").write_text("new\n")
        (self.repo / "build.log").write_text("ignored\n")
        found = facts.drift(meta, self.repo)
        self.assertEqual(found.changed, ["clean.py", "deleted.txt", "edited.py", "new.txt", "reverted.py"])
        self.assertIn("No longer uncommitted (committed or reverted): committed.py, deleted.txt, reverted.py.", found.notes)
        self.assertIn(
            "Files changed since the handoff (5): clean.py, deleted.txt, edited.py, new.txt, reverted.py.", found.notes
        )
        self.assertFalse(any(note.startswith("Newly uncommitted") for note in found.notes))

    def test_falls_back_to_git_state_without_file_hashes(self):
        (self.repo / "a.txt").write_text("a\n")
        (self.repo / "b.txt").write_text("b\n")
        with mock.patch.object(facts, "MAX_HASHED", 1):
            meta = self.hashed_meta()
        self.assertIsNone(meta["file_hashes"])
        (self.repo / "c.txt").write_text("c\n")
        self.assertEqual(facts.drift(meta, self.repo), facts.Drift(["Newly uncommitted: c.txt."], []))

    def test_branch_switch_and_rewritten_history(self):
        meta = self.meta()
        meta["head"] = "0" * 40
        git(self.repo, "checkout", "-qb", "feature")
        notes = facts.drift(meta, self.repo).notes
        self.assertIn("Branch changed: main → feature.", notes)
        self.assertTrue(any("no longer in its history" in note for note in notes))
