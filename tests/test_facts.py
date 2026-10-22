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
    def test_edits_and_checks(self):
        write_transcript(self.transcript, [
            user(),
            tool_use("e1", "Edit", {"file_path": str(self.repo / "app.py")}),
            tool_use("e2", "Write", {"file_path": "/elsewhere/notes.md"}),
            tool_use("e3", "Edit", {"file_path": str(self.repo / "app.py")}),
            tool_use("b1", "Bash", {"command": "pytest -q tests/"}),
            tool_result("b1", is_error=True),
            tool_use("b2", "Bash", {"command": "ls -la"}),
            tool_result("b2"),
            tool_use("b3", "Bash", {"command": "pytest -q tests/"}),
            tool_result("b3"),
            tool_use("b4", "Bash", {"command": "pnpm build"}),
        ])
        activity = facts.session_activity(str(self.transcript), self.repo)
        self.assertEqual(activity.edited, ["/elsewhere/notes.md", "app.py"])
        self.assertEqual(activity.checks, [("pytest -q tests/", True), ("pnpm build", None)])

    def test_missing_transcript(self):
        self.assertEqual(facts.session_activity(str(self.tmp / "nope.jsonl"), self.repo), facts.Activity())


class DriftTest(TempHome):
    def meta(self):
        state = facts.git_state(self.repo)
        return {"branch": state.branch, "head": state.head, "dirty": state.dirty, "diff_hash": state.diff_hash}

    def test_no_changes(self):
        self.assertEqual(facts.drift(self.meta(), self.repo), [])

    def test_new_commits_and_dirty_files(self):
        meta = self.meta()
        (self.repo / "app.py").write_text("print('v2')\n")
        git(self.repo, "commit", "-qam", "second change")
        (self.repo / "todo.txt").write_text("x\n")
        notes = facts.drift(meta, self.repo)
        self.assertEqual(len(notes), 2)
        self.assertIn("1 new commit(s) since the handoff", notes[0])
        self.assertIn("second change", notes[0])
        self.assertEqual(notes[1], "Newly uncommitted: todo.txt.")

    def test_uncommitted_edits_changed(self):
        (self.repo / "app.py").write_text("print('wip')\n")
        meta = self.meta()
        (self.repo / "app.py").write_text("print('more wip')\n")
        self.assertEqual(facts.drift(meta, self.repo), ["The uncommitted changes were edited after the handoff was saved."])

    def test_branch_switch_and_rewritten_history(self):
        meta = self.meta()
        meta["head"] = "0" * 40
        git(self.repo, "checkout", "-qb", "feature")
        notes = facts.drift(meta, self.repo)
        self.assertIn("Branch changed: main → feature.", notes)
        self.assertTrue(any("no longer in its history" in note for note in notes))
