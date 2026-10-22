import io
import json
import os
import subprocess
import sys
import time
from unittest import mock

from contextrail import doctor, handlers, store
from tests.helpers import CR, VALID_BODY, TempHome, assistant, user, write_transcript

REAL_CHECK_PYTHON = doctor.check_python


def log_entry(seconds_ago, command, error):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - seconds_ago))
    return f"--- {stamp} {command}\nTraceback (most recent call last):\n  File \"x.py\", line 1\n{error}\n\n"


class DoctorTest(TempHome):
    def setUp(self):
        super().setUp()
        # Starting python3 takes a while; the real check runs in its own tests below.
        quick = mock.patch.object(doctor, "check_python", lambda: doctor.Result("ok", "Python", "not checked"))
        quick.start()
        self.addCleanup(quick.stop)

    def results(self, cwd=None):
        _, results = doctor.run(cwd or self.repo)
        return {result.name: result for result in results}

    def session(self, name, entries):
        folder = self.claude / "projects" / store.slug(self.repo)
        folder.mkdir(parents=True, exist_ok=True)
        write_transcript(folder / name, entries)

    def test_healthy_home_exits_0(self):
        handlers.heartbeat("SessionStart")
        handlers.heartbeat("Stop")
        result = subprocess.run(
            [sys.executable, str(CR), "doctor", str(self.repo)],
            capture_output=True, text=True, env=dict(os.environ), timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"doctor, for {self.repo}", result.stdout)
        self.assertIn("  ok    Plugin:", result.stdout)
        self.assertNotIn("FAIL", result.stdout)

    def test_broken_config_fails_with_a_clear_message(self):
        self.home.mkdir(parents=True)
        (self.home / "config.json").write_text('{"soft_tokens": 90000,}')
        settings = self.results()["Settings"]
        self.assertEqual(settings.status, "fail")
        self.assertIn(f"{self.home / 'config.json'} is not valid JSON", settings.detail)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(doctor.main([str(self.repo)]), 1)
        self.assertIn("  FAIL  Settings:", out.getvalue())
        self.assertIn("1 check(s) failed", out.getvalue())
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(doctor.main([str(self.repo), "--exit-zero"]), 0)
        self.assertIn("  FAIL  Settings:", out.getvalue())
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(doctor.main(["--setup"]), 2)

    def test_settings_sources_and_off_switch(self):
        self.assertEqual(self.results()["Settings"].detail, "from CONTEXTRAIL_COPY_RESUME_PROMPT")
        with mock.patch.dict(os.environ, {"CONTEXTRAIL_DISABLE": "1"}):
            settings = self.results()["Settings"]
        self.assertEqual(settings.status, "warn")
        self.assertIn("turned off", settings.detail)

    def test_errors_are_reported(self):
        self.home.mkdir(parents=True)
        log = self.home / "error.log"
        log.write_text(log_entry(3 * 86_400, "stop", "KeyError: 'a'"))
        errors = self.results()["Errors"]
        self.assertEqual(errors.status, "warn")
        self.assertIn("1 in the last 7 days, 0 in the last 24 h", errors.detail)

        log.write_text(log_entry(30 * 86_400, "stop", "OldError") + log.read_text() + log_entry(60, "post-tool", "ValueError: bad"))
        errors = self.results()["Errors"]
        self.assertEqual(errors.status, "fail")
        self.assertIn("2 in the last 7 days, 1 in the last 24 h", errors.detail)
        self.assertIn("in `post-tool`: ValueError: bad", errors.detail)

    def test_no_errors(self):
        self.assertEqual(self.results()["Errors"], doctor.Result("ok", "Errors", "none in the last 7 days"))

    def test_missing_heartbeat_warns(self):
        hooks = self.results()["Hooks"]
        self.assertEqual(hooks.status, "warn")
        self.assertIn("SessionStart never", hooks.detail)
        self.assertIn("restart Claude Code", hooks.detail)

        handlers.heartbeat("SessionStart")
        self.assertEqual(self.results()["Hooks"].status, "ok")  # Stop only runs after the first reply

        (self.home / handlers.HEARTBEAT).write_text(json.dumps({"SessionStart": time.time() - 3_600}))
        hooks = self.results()["Hooks"]
        self.assertEqual(hooks.status, "warn")
        self.assertIn("SessionStart 1 hour ago, Stop never", hooks.detail)

    def test_handoff_delivery(self):
        self.assertEqual(self.results()["Handoff"].detail, "none saved yet for this project")
        self.at_tokens(102_000)
        block = f"<contextrail-handoff>\n{VALID_BODY}\n</contextrail-handoff>"
        handlers.on_stop(self.payload("Stop", last_assistant_message=block))
        delivery = self.results()["Handoff"]
        self.assertEqual(delivery.status, "ok")
        self.assertRegex(delivery.detail, r"^loads on startup, clear, compact: \d,\d{3} characters \(≈\d+ tokens\)$")

        with mock.patch.dict(os.environ, {"CONTEXTRAIL_MAX_AGE_DAYS": "0"}):
            delivery = self.results()["Handoff"]
        self.assertEqual(delivery.status, "warn")
        self.assertIn("older than max_age_days (0)", delivery.detail)

        with mock.patch.object(handlers, "on_session_start", lambda payload: {"systemMessage": "x"}):
            delivery = self.results()["Handoff"]
        self.assertEqual(delivery.status, "fail")
        self.assertIn("no additionalContext", delivery.detail)

    def test_transcript_usage(self):
        self.assertEqual(self.results()["Transcript"].status, "warn")  # no sessions anywhere

        self.session("old.jsonl", [user(), assistant(50_000)])
        transcript = self.results()["Transcript"]
        self.assertEqual(transcript.status, "ok")
        self.assertIn("50,000 tokens of context", transcript.detail)
        self.assertIn("latest session in this folder", transcript.detail)
        self.assertIn("window size from the 200k default", transcript.detail.lower())

        other = self.tmp / "other"
        other.mkdir()
        self.assertIn("in another folder", self.results(other)["Transcript"].detail)

    def test_transcripts_without_usage_fail(self):
        no_usage = {"type": "assistant", "message": {"role": "assistant", "content": []}}
        self.session("a.jsonl", [user(), no_usage])
        self.assertEqual(self.results()["Transcript"].status, "warn")  # one new session: not enough to tell
        self.session("b.jsonl", [user(), no_usage])
        transcript = self.results()["Transcript"]
        self.assertEqual(transcript.status, "fail")
        self.assertIn("format may have changed", transcript.detail)

    def test_plugin_hooks(self):
        plugin = self.results()["Plugin"]
        self.assertEqual(plugin.status, "ok")
        self.assertIn("4 hooks (Stop, PostToolUse, PreCompact, SessionStart)", plugin.detail)
        with mock.patch.object(handlers, "HOOKS", {**handlers.HOOKS, "notify": ("Notification", None)}):
            plugin = self.results()["Plugin"]
        self.assertEqual(plugin.status, "fail")
        self.assertIn("no hook for Notification", plugin.detail)

    def test_a_crashing_check_is_reported_not_raised(self):
        with mock.patch.object(doctor, "check_storage", side_effect=RuntimeError("boom")):
            storage = self.results()["Storage"]
        self.assertEqual(storage.status, "fail")
        self.assertIn("the check itself failed: RuntimeError('boom')", storage.detail)

    def test_unwritable_storage(self):
        self.home.write_text("a file, not a folder")
        self.assertEqual(self.results()["Storage"].status, "fail")


class PythonCheckTest(TempHome):
    def test_reports_version_and_start_up(self):
        result = REAL_CHECK_PYTHON()
        self.assertIn(result.status, ("ok", "warn"), result.detail)
        self.assertRegex(result.detail, r"^3\.\d+\.\d+ at .+, hook start-up \d+ ms")
        with mock.patch.object(doctor, "SLOW_START_MS", 0):
            slow = REAL_CHECK_PYTHON()
        self.assertEqual(slow.status, "warn")
        self.assertIn("which every hook pays", slow.detail)

    def test_no_python3_on_path(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp)}):
            result = REAL_CHECK_PYTHON()
        self.assertEqual(result, doctor.Result("fail", "Python", "python3 is not on PATH, so the hooks can't start"))

    def test_hook_environment_undoes_pyenv_exec(self):
        inside = {
            "PATH": os.pathsep.join(["/p/versions/3.11.9/bin", "/brew/pyenv/2.6/libexec", "/p/shims", "/usr/bin"]),
            "PYENV_ROOT": "/p",
            "PYENV_VERSION": "3.11.9",
            "PYENV_DIR": "/work",
            "PYENV_HOOK_PATH": "/p/pyenv.d",
            "_PYENV_INSTALL_PREFIX": "/brew/pyenv/2.6",
        }
        with mock.patch.dict(os.environ, inside, clear=True):
            env = doctor.hook_environment()
        self.assertEqual(env, {"PATH": os.pathsep.join(["/p/shims", "/usr/bin"]), "PYENV_ROOT": "/p"})

        outside = {"PATH": os.pathsep.join(["/p/shims", "/usr/bin"]), "PYENV_ROOT": "/p"}
        with mock.patch.dict(os.environ, outside, clear=True):
            self.assertEqual(doctor.hook_environment(), outside)
