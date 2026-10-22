import io
import json
import os
import shlex
import subprocess
import sys
import time
from unittest import mock

from contextrail import doctor, handlers, status, statusline, store, usage
from tests.helpers import CR, ROOT, TempHome, assistant, user, write_transcript

SESSION = "ba911651-67f8-4392-8840-ee7852f17155"
INNER = "import json, sys; print(json.load(sys.stdin)['model']['display_name']); sys.exit(len(sys.argv) - 1)"


def sample(cwd, session_id=SESSION, window=1_000_000, used=5):
    """A status-line payload as Claude Code 2.1.308 sends it, trimmed."""
    return {
        "session_id": session_id,
        "transcript_path": f"/x/{session_id}.jsonl",
        "cwd": cwd,
        "model": {"id": "claude-haiku-5-5", "display_name": "Haiku 5.5"},
        "workspace": {"current_dir": cwd},
        "context_window": {
            "total_input_tokens": 50_591,
            "total_output_tokens": 4,
            "context_window_size": window,
            "current_usage": {
                "input_tokens": 2, "output_tokens": 4,
                "cache_creation_input_tokens": 24_901, "cache_read_input_tokens": 25_688,
            },
            "used_percentage": used,
            "remaining_percentage": 100 - used,
        },
        "exceeds_200k_tokens": False,
    }


class StatusLineTest(TempHome):
    def setUp(self):
        super().setUp()
        self.inner = self.tmp / "inner.py"
        self.inner.write_text(INNER)

    def status_input(self, **extra):
        return json.dumps(sample(str(self.repo), **extra)).encode()

    def run_cr(self, *args, stdin=b""):
        return subprocess.run(
            [sys.executable, str(CR), "statusline", *args], input=stdin, capture_output=True,
            env=dict(os.environ), timeout=30,
        )

    def report(self, session_id=SESSION):
        return store.read_json(self.home / "sessions" / f"{session_id}.json")

    def test_records_the_window_and_prints_its_own_line(self):
        result = self.run_cr(stdin=self.status_input())
        self.assertEqual((result.returncode, result.stderr), (0, b""))
        self.assertEqual(result.stdout.decode(), "ctx 5% · 51k/1M · handoff at 100k\n")
        report = self.report()
        self.assertEqual((report["window"], report["used_pct"]), (1_000_000, 5))
        self.assertLess(abs(time.time() - report["ts"]), 30)

    def test_passes_the_wrapped_status_line_through(self):
        line = f"{shlex.quote(sys.executable)} {shlex.quote(str(self.inner))} | tr a-z A-Z"
        result = self.run_cr("--", line, stdin=self.status_input())
        self.assertEqual((result.returncode, result.stdout), (0, b"HAIKU 5.5\n"))
        self.assertEqual(self.report()["window"], 1_000_000)
        # As separate arguments it runs without a shell; a failing command keeps its output and exit code.
        result = self.run_cr("--", sys.executable, str(self.inner), "x", "y", stdin=self.status_input())
        self.assertEqual((result.returncode, result.stdout), (2, b"Haiku 5.5\n"))

    def test_a_slow_wrapped_command_is_cut_off(self):
        with mock.patch.object(statusline, "INNER_TIMEOUT", 0.3):
            start = time.time()
            self.assertEqual(statusline.show(["--", "sleep 5"], b"{}"), 0)
        self.assertLess(time.time() - start, 3)

    def test_malformed_input_never_breaks_it(self):
        for stdin in (b"", b"not json", b"[1, 2]", b'{"context_window": "x", "session_id": 7}', b"\xff\xfe{"):
            result = self.run_cr(stdin=stdin)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""), stdin)
        self.assertEqual(self.run_cr("--", "cat", stdin=b"not json").stdout, b"not json")
        self.assertFalse((self.home / "sessions").exists())
        self.assertFalse((self.home / "error.log").exists())

    def test_unsafe_session_ids_are_not_used_as_file_names(self):
        for bad in ("../../evil", ".hidden", "a/b", ""):
            statusline.record(sample(str(self.repo), session_id=bad))
            self.assertIsNone(usage.session_report(bad))
        self.assertFalse((self.home / "sessions").exists())

    def test_writes_at_most_every_few_seconds_and_prunes_old_reports(self):
        sessions = self.home / "sessions"
        sessions.mkdir(parents=True)
        old, recent = sessions / "old.json", sessions / "recent.json"
        for path, days in ((old, 8), (recent, 1)):
            path.write_text('{"window": 200000, "ts": 1}')
            stamp = time.time() - days * 86_400
            os.utime(path, (stamp, stamp))
        statusline.record(sample(str(self.repo)))
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        first = self.report()["ts"]
        statusline.record(sample(str(self.repo), used=6))
        self.assertEqual(self.report()["ts"], first)
        statusline.record(sample(str(self.repo), window=200_000))
        self.assertEqual(self.report()["window"], 200_000)

    def test_own_line_follows_the_session_state(self):
        project = store.Project(self.repo)
        state = store.load_state(project)
        store.session(state, SESSION)["saved_tokens"] = 102_000
        store.save_state(project, state)
        self.assertEqual(statusline.line(sample(str(self.repo))), "ctx 5% · 51k/1M · handoff at 142k")
        with mock.patch.dict(os.environ, {"CONTEXTRAIL_DISABLE": "1"}):
            self.assertEqual(statusline.line(sample(str(self.repo))), "ctx 5% · 51k/1M · ContextRail off")
        fresh = sample(str(self.repo), session_id="new")
        fresh["context_window"].update(current_usage=None, used_percentage=None)
        self.assertEqual(statusline.line(fresh), "ctx 0% · 0k/1M · handoff at 100k")


class WindowSourceTest(TempHome):
    def test_hooks_use_the_reported_window(self):
        statusline.record(sample(str(self.repo), session_id=self.transcript.stem))
        self.at_tokens(102_000, model="claude-haiku-5-5")
        found = usage.last_usage(str(self.transcript))
        self.assertEqual((found.window, found.window_source), (1_000_000, "statusline"))
        out = handlers.on_stop(self.payload("Stop", last_assistant_message="ok"))
        self.assertIn("102,000 tokens of context (≈10% of a 1M window)", out["reason"])

    def test_precedence_and_freshness(self):
        statusline.record(sample(str(self.repo), session_id=self.transcript.stem, window=200_000))
        self.at_tokens(50_000, model="claude-opus-5-5[1m]")
        self.assertEqual(usage.last_usage(str(self.transcript)).window_source, "statusline")
        self.assertEqual(usage.last_usage(str(self.transcript), 500_000).window_source, "config")
        report = self.home / "sessions" / f"{self.transcript.stem}.json"
        report.write_text(json.dumps({"window": 200_000, "ts": time.time() - 25 * 3600}))
        self.assertEqual(usage.last_usage(str(self.transcript)).window_source, "model")

    def test_status_and_doctor_name_the_source(self):
        folder = self.claude / "projects" / store.slug(self.repo)
        folder.mkdir(parents=True)
        write_transcript(folder / f"{SESSION}.jsonl", [user(), assistant(50_000, model="claude-haiku-5-5")])
        statusline.record(sample(str(self.repo)))
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            status.main([str(self.repo)])
        self.assertIn("Window:    1,000,000 tokens, from the status line\n", out.getvalue())
        transcript = doctor.check_transcript(self.repo, {"window_tokens": None})
        self.assertIn("Window size from the status line", transcript.detail)
        self.assertIn("last reported a 1,000,000-token window just now", doctor.check_statusline().detail)


class PluginRootTest(TempHome):
    def test_written_by_the_session_start_hook_only(self):
        handlers.on_session_start({"cwd": str(self.repo), "source": "startup", "session_id": "doctor"})
        self.assertFalse((self.home / "plugin-root").exists())
        payload = json.dumps(self.payload("SessionStart", source="startup"))
        subprocess.run([sys.executable, str(CR), "session-start"], input=payload, text=True, env=dict(os.environ), timeout=30)
        self.assertEqual((self.home / "plugin-root").read_text().strip(), str(ROOT))


class SetupTest(TempHome):
    def setup_statusline(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = doctor.main(["--setup-statusline"])
        return code, out.getvalue()

    def write_settings(self, settings):
        self.claude.mkdir(parents=True, exist_ok=True)
        path = self.claude / "settings.json"
        path.write_text(json.dumps(settings, indent=2))
        return path

    def run_line(self, command, stdin):
        return subprocess.run(["/bin/sh", "-c", command], input=stdin, capture_output=True, env=dict(os.environ), timeout=30)

    def test_wraps_the_current_status_line_without_editing_settings(self):
        path = self.write_settings({"statusLine": {"type": "command", "command": "printf 'gsd %s' line | cat", "padding": 0}, "x": 1})
        before = path.read_bytes()
        code, out = self.setup_statusline()
        self.assertEqual(code, 0, out)
        self.assertEqual(path.read_bytes(), before)
        launcher = self.home / "bin" / "statusline"
        self.assertTrue(os.access(launcher, os.X_OK))
        entry = {"type": "command", "command": f"{launcher} -- 'printf '\"'\"'gsd %s'\"'\"' line | cat'", "padding": 0}
        self.assertIn(f'  "statusLine": {json.dumps(entry)}\n', out)
        self.assertIn("ContextRail never edits that file", out)

        stdin = json.dumps(sample(str(self.repo))).encode()
        self.assertEqual(self.run_line(entry["command"], stdin).stdout, b"gsd line")
        self.assertEqual(store.read_json(self.home / "sessions" / f"{SESSION}.json")["window"], 1_000_000)
        # The plugin gone: the launcher still runs the wrapped status line, and nothing else.
        (self.home / "plugin-root").unlink()
        self.assertEqual(self.run_line(entry["command"], stdin).stdout, b"gsd line")
        self.assertEqual(self.run_line(str(launcher), stdin).stdout, b"")

        self.write_settings({"statusLine": entry})
        self.assertIn("already runs through ContextRail", self.setup_statusline()[1])

    def test_without_a_status_line_or_with_broken_settings(self):
        code, out = self.setup_statusline()
        self.assertEqual(code, 0)
        self.assertIn(json.dumps({"type": "command", "command": str(self.home / "bin" / "statusline")}), out)
        self.assertIn(statusline.EXAMPLE, out)
        self.write_settings({}).write_text("{")
        code, out = self.setup_statusline()
        self.assertEqual(code, 1)
        self.assertIn("settings.json can't be read", out)

    def test_doctor_check(self):
        self.assertIn("not set up (optional)", doctor.check_statusline().detail)
        launcher = self.home / "bin" / "statusline"
        self.write_settings({"statusLine": {"type": "command", "command": f"{launcher} -- 'x'"}})
        self.assertEqual(doctor.check_statusline().status, "fail")
        self.setup_statusline()
        check = doctor.check_statusline()
        self.assertEqual((check.status, check.detail[:22]), ("warn", "set up in settings.jso"))
