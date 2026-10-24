import io
import json
import os
import time
from unittest import mock

from contextrail import handlers, purge, store
from tests.helpers import VALID_BODY, TempHome, make_repo


class PurgeTest(TempHome):
    def save(self, root=None, step="first"):
        body = VALID_BODY.replace("rate limiting", f"rate limiting {step}")
        handlers.on_stop(dict(self.payload("Stop", last_assistant_message=f"<contextrail-handoff>\n{body}\n</contextrail-handoff>"),
                              cwd=str(root or self.repo)))

    def project(self, root=None):
        return store.open_project(root or self.repo)

    def purge(self, *args):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = purge.main([*args, str(self.repo)])
        return code, out.getvalue()

    def test_lists_first_and_deletes_only_with_yes(self):
        self.save()
        self.save(step="second")
        (self.project().dir / "config.json").write_text(json.dumps({"soft_tokens": 50_000}), encoding="utf-8")
        code, out = self.purge()
        self.assertEqual(code, 0)
        self.assertIn("would delete this project's handoffs and session state", out)
        self.assertIn("HANDOFF.md", out)
        self.assertIn("Nothing was deleted", out)
        self.assertTrue(self.project().handoff.exists())

        code, out = self.purge("--yes")
        self.assertEqual(code, 0)
        self.assertIn("ContextRail deleted this project's handoffs", out)
        self.assertIn("The next session here starts without a handoff.", out)
        names = sorted(path.name for path in self.project().dir.iterdir())
        self.assertNotIn("HANDOFF.md", names)
        self.assertNotIn("history", names)
        self.assertNotIn("state.json", names)
        self.assertIn("config.json", names)  # settings stay
        self.assertIsNone(handlers.on_session_start(self.payload("SessionStart", source="startup")))
        self.assertIn("nothing to delete", self.purge("--yes")[1])

    def test_archives_only(self):
        self.save()
        self.save(step="second")
        self.assertEqual(len(store.archived(self.project())), 1)
        self.purge("--archives", "--yes")
        self.assertEqual(store.archived(self.project()), [])
        self.assertTrue(self.project().handoff.exists())

    def test_other_projects_are_left_alone(self):
        other = make_repo(self.tmp / "other")
        self.save()
        self.save(root=other)
        self.purge("--yes")
        self.assertFalse(self.project().handoff.exists())
        self.assertTrue(self.project(other).handoff.exists())

    def test_stale_projects_and_reports(self):
        other = make_repo(self.tmp / "other")
        self.save()
        self.save(root=other)
        sessions = self.home / "sessions"
        sessions.mkdir()
        (sessions / "old.json").write_text("{}", encoding="utf-8")
        (sessions / "new.json").write_text("{}", encoding="utf-8")
        old = time.time() - 40 * 86_400
        for path in [*self.project(other).dir.rglob("*"), sessions / "old.json"]:
            os.utime(path, (old, old))
        code, out = self.purge("--stale", "30")
        self.assertIn(self.project(other).dir.name, out)
        self.assertNotIn(self.project().dir.name, out)
        self.purge("--stale", "30", "--yes")
        self.assertFalse(self.project(other).dir.exists())
        self.assertTrue(self.project().handoff.exists())
        self.assertEqual(sorted(path.name for path in sessions.iterdir()), ["new.json"])

    def test_everything_but_the_settings(self):
        self.save()
        (self.home / "config.json").write_text(json.dumps({"lightning_tokens": 300_000}), encoding="utf-8")
        (self.home / "error.log").write_text("x", encoding="utf-8")
        self.purge("--everything", "--yes")
        self.assertEqual(sorted(path.name for path in self.home.iterdir()), ["config.json"])

    def test_legacy_folder_left_in_place(self):
        legacy = self.home / "projects" / store.claude_slug(self.repo)
        legacy.mkdir(parents=True)
        (legacy / "meta.json").write_text(json.dumps({"head": "deadbeef" * 5}), encoding="utf-8")
        self.project()  # declines it: that commit is not in this repository
        self.assertTrue(legacy.exists())
        self.assertIn(legacy.name, self.purge("--legacy")[1])
        self.purge("--legacy", "--yes")
        self.assertFalse(legacy.exists())

    def test_bad_arguments(self):
        for args in (["--stale"], ["--stale", "0"], ["--stale", "x"], ["--archives", "--everything"], ["--nope"], ["a", "b"]):
            with self.subTest(args=args), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(purge.main(args), 2)
                self.assertIn("usage:", out.getvalue())
                with mock.patch("sys.stdout", new_callable=io.StringIO):
                    self.assertEqual(purge.main([*args, "--exit-zero"]), 0)

    def test_an_old_error_log_goes_at_a_session_start(self):
        self.home.mkdir(parents=True)
        log = self.home / "error.log"
        log.write_text("old", encoding="utf-8")
        purge.retention()
        self.assertTrue(log.exists())
        old = time.time() - (purge.ERROR_LOG_DAYS + 1) * 86_400
        os.utime(log, (old, old))
        purge.retention()
        self.assertFalse(log.exists())
