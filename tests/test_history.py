import io
import json
import os
from pathlib import Path
from unittest import mock

from contextrail import facts, handlers, history, store
from tests.helpers import VALID_BODY, TempHome, git

FIRST_STEP = VALID_BODY.split("## Next steps\n")[1].splitlines()[0]


class HistoryTest(TempHome):
    def save(self, step):
        body = VALID_BODY.replace(FIRST_STEP, f"1. Run `{step}`")
        handlers.on_stop(self.payload("Stop", last_assistant_message=f"<contextrail-handoff>\n{body}\n</contextrail-handoff>"))

    def project(self):
        return store.Project(self.repo)

    def run_history(self, *args):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = history.main([*args, str(self.repo)])
        return code, out.getvalue()

    def test_nothing_saved(self):
        self.assertEqual(self.run_history(), (0, f"No handoffs saved yet for {self.repo}.\n"))

    def test_lists_current_then_archived_newest_first(self):
        for step in ("first step", "second step", "third step"):  # all within one second
            self.save(step)
        code, out = self.run_history()
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertTrue(lines[1].startswith("  current  20"))
        self.assertEqual(lines[2].strip(), "Next: Run `third step`")
        self.assertTrue(lines[3].startswith("        1  "))
        self.assertEqual(lines[4].strip(), "Next: Run `second step`")
        self.assertEqual(lines[6].strip(), "Next: Run `first step`")
        self.assertIn("--restore N", lines[-1])
        archive = self.project().history
        self.assertEqual(len(list(archive.glob("*.md"))), 2)
        self.assertEqual(len(list(archive.glob("*.json"))), 2)

    def test_restore_makes_an_archived_handoff_current(self):
        for step in ("first step", "second step", "third step"):
            self.save(step)
        code, out = self.run_history("--restore", "2")
        self.assertEqual(code, 0)
        self.assertIn("Restored the handoff saved", out)
        self.assertIn("Next: Run `first step`", out)

        project = self.project()
        self.assertIn("1. Run `first step`", project.handoff.read_text())
        meta = json.loads(project.meta.read_text())
        self.assertEqual(meta["head"], facts.git_state(self.repo).head)
        self.assertTrue(meta["restored_from"].endswith(".md"))
        self.assertEqual(project.resume.read_text().strip(), VALID_BODY.split("## Resume prompt\n")[1])
        _, out = self.run_history()
        self.assertEqual([line.strip() for line in out.splitlines()[2::2]][:3],
                         ["Next: Run `first step`", "Next: Run `third step`", "Next: Run `second step`"])
        loaded = handlers.on_session_start(self.payload("SessionStart", source="clear"))
        self.assertIn("1. Run `first step`", loaded["hookSpecificOutput"]["additionalContext"])

    def test_says_where_each_handoff_was_saved_and_for_which_project(self):
        self.save("first step")
        self.save("second step")
        head = facts.git_state(self.repo).head[:9]
        _, out = self.run_history()
        self.assertIn(f", on `main` at {head}, ", out.splitlines()[1])
        _, out = self.run_history("--restore", "1")
        self.assertIn(f"(just now), on `main` at {head}, for {self.repo}.", out)

    def test_an_archive_of_another_project_is_not_restored(self):
        self.save("first step")
        self.save("second step")
        archive = store.archived(self.project())[0].with_suffix(".json")
        archive.write_text(json.dumps(dict(json.loads(archive.read_text()), project="/somewhere/else")))
        current = self.project().handoff.read_text()
        code, out = self.run_history("--restore", "1")
        self.assertEqual(code, 1)
        self.assertIn("was saved for another project, /somewhere/else", out)
        self.assertEqual(self.project().handoff.read_text(), current)

    def test_restore_an_archive_from_before_meta_was_kept(self):
        self.save("old step")
        self.save("new step")
        archived = store.archived(self.project())
        archived[0].with_suffix(".json").unlink()
        (self.repo / "app.py").write_text("print('v2')\n")
        git(self.repo, "commit", "-qam", "later work")
        (self.repo / "notes.txt").write_text("x\n")

        self.assertEqual(self.run_history("--restore", "1")[0], 0)
        meta = json.loads(self.project().meta.read_text())
        self.assertIsNone(meta["dirty"])
        self.assertEqual(len(meta["head"]), 9)
        found = facts.drift(meta, self.repo)
        self.assertEqual(len(found.notes), 2)
        self.assertIn("1 new commit(s) since the handoff: ", found.notes[0])
        # Without the uncommitted files of the time, file changes can't be told, and it says so.
        self.assertIn("can't be checked", found.notes[1])
        self.assertFalse(found.verified)

    def test_bad_number_and_usage(self):
        self.save("only step")
        code, out = self.run_history("--restore", "3")
        self.assertEqual((code, out), (1, "ContextRail: there is no archived handoff 3; the archive holds 0.\n"))
        self.assertEqual(self.run_history("--restore", "3", "--exit-zero")[0], 0)
        self.assertEqual(self.run_history("--restore", "x"), (2, history.USAGE + "\n"))
        self.assertEqual(self.run_history("--all")[0], 2)

    def test_pruning_keeps_the_newest_and_their_meta(self):
        with mock.patch.dict(os.environ, {"CONTEXTRAIL_HISTORY_KEEP": "2"}):
            for step in range(5):
                self.save(f"step {step}")
        archive = self.project().history
        self.assertEqual(len(list(archive.glob("*.json"))), 2)
        _, out = self.run_history()
        self.assertEqual([line.strip() for line in out.splitlines()[2::2]][:3],
                         ["Next: Run `step 4`", "Next: Run `step 3`", "Next: Run `step 2`"])

    def test_archive_order(self):
        names = ["2026-01-02T10-00-00-2.md", "2026-01-02T10-00-00.md", "2026-01-01T09-00-00.md",
                 "2026-01-02T10-00-00-10.md", "2026-01-02T10-00-00-1.md"]
        ordered = sorted((Path(name) for name in names), key=store.archive_order)
        self.assertEqual([path.name for path in ordered], [
            "2026-01-01T09-00-00.md", "2026-01-02T10-00-00.md", "2026-01-02T10-00-00-1.md",
            "2026-01-02T10-00-00-2.md", "2026-01-02T10-00-00-10.md",
        ])
