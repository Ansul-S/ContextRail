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
        self.assertEqual(activity.results(), [("pytest -q tests/", "passed"), ("pnpm build", "result unknown")])

    def test_heredoc_body_is_not_a_check(self):
        command = "cat > scripts/ci.sh <<'EOF'\nset -e\npython -m unittest discover\nEOF\nchmod +x scripts/ci.sh"
        self.assertEqual(self.activity(*self.bash("b1", command)).checks, [])

    def test_failed_summary_behind_a_pipe(self):
        command = f"cd {self.repo} && python3 -m unittest discover -s tests -t . 2>&1 | tail -3"
        output = "Ran 12 tests in 0.4s\n\nFAILED (failures=1)"
        checks = self.activity(*self.bash("b1", command, output)).results()
        self.assertEqual(checks, [("python3 -m unittest discover -s tests -t .", "failed")])

    def test_pytest_summary(self):
        # A pipe hides pytest's exit status: a passing summary is what it says, not a verified pass.
        cases = (("=== 2 failed, 40 passed in 1.21s ===", "failed"), ("42 passed, 1 skipped in 0.9s", "output says passed"))
        for output, expected in cases:
            with self.subTest(output=output):
                checks = self.activity(*self.bash("b1", "pytest -q | tail -1", output)).results()
                self.assertEqual(checks, [("pytest -q", expected)])

    def test_pipe_without_a_summary_only_ran(self):
        self.assertEqual(self.activity(*self.bash("b1", "pnpm build | tail -5", "done")).results(), [("pnpm build", "ran")])

    def test_output_decides_when_the_exit_status_is_masked(self):
        checks = self.activity(*self.bash("b1", "pytest -q || true", "1 failed, 2 passed in 0.1s")).results()
        self.assertEqual(checks, [("pytest -q", "failed")])

    def test_exit_status_decides_without_a_pipe(self):
        checks = self.activity(
            *self.bash("b1", "cd sub && npm test", is_error=True),
            *self.bash("b2", "make lint && echo done", "no summary here"),
        ).results()
        self.assertEqual(checks, [("npm test", "failed"), ("make lint", "passed")])

    def test_commands_that_did_not_run_or_finish(self):
        checks = self.activity(
            *self.bash("b1", "pytest -q", "Permission to use Bash with command pytest -q has been denied.", is_error=True),
            *self.bash("b2", "npm run e2e", "Command running in background with ID: b2.", run_in_background=True),
            tool_use("b3", "Bash", {"command": "cargo test"}),
        ).results()
        self.assertEqual(
            checks, [("pytest -q", "result unknown"), ("npm run e2e", "no result yet"), ("cargo test", "result unknown")]
        )

    def test_content_blocks_and_long_commands(self):
        output = [{"type": "text", "text": "Ran 3 tests in 0.1s\n\nOK"}]
        command = "python3 -m unittest tests/" + "x" * 100 + " 2>&1 | tail -1; python3 -m unittest"
        [(shown, result)] = self.activity(*self.bash("b1", command, output)).results()
        self.assertEqual(result, "output says passed")
        self.assertEqual(shown, "python3 -m unittest tests/" + "x" * 53 + "… (+1 more)")

    def notice(self, tool_id, status, summary, task_id="t1"):
        text = (
            f"<task-notification>\n<task-id>{task_id}</task-id>\n<tool-use-id>{tool_id}</tool-use-id>\n"
            f"<output-file>/tmp/x.output</output-file>\n<status>{status}</status>\n<summary>{summary}</summary>\n"
            "</task-notification>"
        )
        return {"type": "user", "isSidechain": False, "message": {"role": "user", "content": text}}

    def test_a_timed_out_or_interrupted_check_is_not_a_failure(self):
        checks = self.activity(
            *self.bash("b1", "pytest -q", "Exit code 143\nCommand timed out after 2m 0s", is_error=True),
            *self.bash("b2", "npm test", "[Request interrupted by user for tool use]", is_error=True),
            *self.bash("b3", "cargo test", "Exit code 137\nKilled", is_error=True),
        ).checks
        self.assertEqual([(c.command, c.result, c.exit_code) for c in checks], [
            ("pytest -q", "timed out", 143), ("npm test", "interrupted", None), ("cargo test", "stopped", 137),
        ])
        self.assertFalse(any(check.verified for check in checks))

    def test_a_check_moved_to_the_background_takes_the_result_its_notification_gives(self):
        moved = "Command was moved to the background (ID: bx1) so that a message that arrived while it was running can reach you; it was not interrupted."
        cases = (
            ("completed", 'Background command "pytest" completed (exit code 0)', "passed", 0),
            ("failed", 'Background command "pytest" failed with exit code 1', "failed", 1),
            ("killed", 'Background command "pytest" was stopped', "stopped", None),
            ("failed", 'Background command "pytest" failed with exit code 143', "stopped", 143),
        )
        for status, summary, result, code in cases:
            with self.subTest(summary=summary):
                [check] = self.activity(*self.bash("b1", "pytest -q", moved), self.notice("b1", status, summary)).checks
                self.assertEqual((check.result, check.exit_code), (result, code))
        # Without the notification it is still running: never "passed" on the strength of is_error being false.
        [check] = self.activity(*self.bash("b1", "pytest -q", moved)).checks
        self.assertEqual(check.result, "no result yet")

    def test_a_background_check_is_resolved_by_its_task_id_too(self):
        started = "Command running in background with ID: bg7. Output is being written to: /tmp/bg7.output"
        entries = [*self.bash("b1", "npm test", started, run_in_background=True)]
        notice = self.notice("", "completed", 'Background command "npm test" completed (exit code 0)', task_id="bg7")
        [check] = self.activity(*entries, notice).checks
        self.assertEqual(check.result, "passed")
        # A pipe hides the check's own status, so the command's exit 0 only says it ran.
        entries = [*self.bash("b1", "npm test | tail -3", started, run_in_background=True)]
        [check] = self.activity(*entries, notice).checks
        self.assertEqual(check.result, "ran")

    def test_a_passing_summary_from_a_command_that_failed_is_unknown(self):
        [check] = self.activity(*self.bash("b1", "pytest -q | tee log.txt", "Exit code 1\n42 passed in 0.5s", is_error=True)).checks
        self.assertEqual((check.result, check.evidence), ("result unknown", "output"))
        self.assertFalse(check.verified)

    def test_chained_commands_keep_the_checks_own_status(self):
        checks = self.activity(
            *self.bash("b1", "cd api && pytest -q && echo ok", "3 passed"),
            *self.bash("b2", "ruff check .; echo done", "Exit code 0"),
        ).checks
        self.assertEqual([(c.command, c.result) for c in checks], [("pytest -q", "passed"), ("ruff check .", "ran")])
        self.assertTrue(checks[0].verified)

    def test_records_where_and_when_a_check_ran_and_what_changed_after_it(self):
        def at(entry, cwd, stamp):
            return dict(entry, cwd=cwd, timestamp=stamp)
        run = self.bash("b1", "pytest -q", "3 passed")
        activity = self.activity(
            at(run[0], str(self.repo / "api"), "2026-10-24T08:00:00.000Z"), run[1],
            tool_use("e1", "Edit", {"file_path": str(self.repo / "app.py")}),
            tool_use("e2", "Edit", {"file_path": str(self.repo / "lib.py")}),
            *self.bash("g1", "git stash", ""),
        )
        [check] = activity.checks
        self.assertEqual((check.cwd, check.at, check.edits_after, check.git_after), ("api", "2026-10-24T08:00:00.000Z", 2, True))
        self.assertEqual(check.to_json()["verified"], True)
        later = self.activity(tool_use("e1", "Edit", {"file_path": str(self.repo / "app.py")}), *run)
        self.assertEqual((later.checks[0].edits_after, later.checks[0].git_after), (0, False))

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

    def test_entries_of_the_wrong_shape_are_skipped(self):
        path = write_transcript(self.transcript, [
            {"type": "assistant", "message": "tool_use"},
            {"type": "user", "message": ["tool_result"]},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "x", "name": ["Edit"], "input": {}}]}},
            tool_use("t1", "Edit", {"file_path": str(self.repo / "app.py")}),
        ])
        self.assertEqual(facts.session_activity(path, self.repo).edited, ["app.py"])


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
    def meta(self, hashes=False):
        state = facts.git_state(self.repo)
        meta = {"branch": state.branch, "head": state.head, "dirty": state.dirty, "diff_hash": state.diff_hash}
        if hashes:
            meta["file_hashes"] = facts.uncommitted_hashes(self.repo)
        return meta

    def test_a_committed_rename_is_named_and_the_old_name_counts_as_changed(self):
        meta = self.meta(hashes=True)
        git(self.repo, "mv", "app.py", "main.py")
        git(self.repo, "commit", "-qm", "rename")
        found = facts.drift(meta, self.repo)
        self.assertEqual(found.renamed, [("app.py", "main.py")])
        self.assertTrue(any("Renamed since the handoff: app.py → main.py." in note for note in found.notes))
        self.assertIn("app.py", found.changed)

    def test_a_plain_move_is_found_by_content(self):
        (self.repo / "notes.txt").write_text("draft\n")
        meta = self.meta(hashes=True)
        (self.repo / "app.py").rename(self.repo / "server.py")
        (self.repo / "notes.txt").rename(self.repo / "NOTES.md")
        found = facts.drift(meta, self.repo)
        self.assertEqual(found.renamed, [("app.py", "server.py"), ("notes.txt", "NOTES.md")])
        self.assertTrue(found.verified)

    def test_a_change_by_someone_else_between_sessions_is_found(self):
        meta = self.meta(hashes=True)
        (self.repo / "app.py").write_text("print('edited in another editor')\n")
        found = facts.drift(meta, self.repo)
        self.assertEqual(found.changed, ["app.py"])

    def test_a_worktree_is_its_own_project(self):
        tree = self.tmp / "tree"
        git(self.repo, "worktree", "add", "-q", "-b", "feature", str(tree))
        state = facts.git_state(tree)
        self.assertEqual((state.branch, state.worktree_of), ("feature", str(self.repo)))
        self.assertEqual(facts.git_state(self.repo).worktree_of, "")
        meta = {"branch": state.branch, "head": state.head, "dirty": [], "diff_hash": state.diff_hash, "file_hashes": {}}
        (tree / "app.py").write_text("print('in the worktree')\n")
        self.assertEqual(facts.drift(meta, tree).changed, ["app.py"])
        self.assertEqual(facts.drift(self.meta(hashes=True), self.repo).changed, [])

    def test_rewritten_history_is_unverified(self):
        (self.repo / "app.py").write_text("print('v2')\n")
        git(self.repo, "commit", "-qam", "second")
        meta = self.meta(hashes=True)
        git(self.repo, "reset", "-q", "--hard", "HEAD~1")
        git(self.repo, "reflog", "expire", "--expire=now", "--all")
        git(self.repo, "gc", "-q", "--prune=now")
        found = facts.drift(meta, self.repo)
        self.assertFalse(found.verified)
        self.assertTrue(any("no longer in its history" in note for note in found.notes))

    def test_missing_git_or_missing_state_is_said_not_hidden(self):
        meta = self.meta(hashes=True)
        self.assertFalse(facts.drift({}, self.repo).verified)
        no_git = self.tmp / "copy"
        no_git.mkdir()
        found = facts.drift(meta, no_git)
        self.assertFalse(found.verified)
        self.assertIn("git can't read this folder now", found.notes[0])
        self.assertFalse(facts.drift({"saved_at": "x", "head": None}, self.repo).verified)

    def test_outside_git_the_files_the_session_edited_are_checked(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        (plain / "a.py").write_text("a\n")
        (plain / "b.py").write_text("b\n")
        meta = {"saved_at": "x", "head": None, "edited_hashes": facts.edited_hashes(plain, ["a.py", "b.py"])}
        self.assertEqual(facts.drift(meta, plain).changed, [])
        (plain / "a.py").write_text("changed\n")
        (plain / "b.py").unlink()
        found = facts.drift(meta, plain)
        self.assertEqual((found.changed, found.verified), (["a.py", "b.py"], True))
        self.assertIn("only the files edited in the earlier session were checked", found.notes[0])

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
