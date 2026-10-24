import json
import os
import subprocess
import sys
import time
from unittest import mock

from contextrail import config, handlers, handoff, prompts, store
from tests.helpers import CR, ROOT, VALID_BODY, TempHome


class CliTest(TempHome):
    def run_cr(self, *args, stdin=""):
        return subprocess.run(
            [sys.executable, str(CR), *args], input=stdin, capture_output=True, text=True,
            env=dict(os.environ), timeout=30, encoding="utf-8",
        )

    def test_stop_hook_prints_json(self):
        self.at_tokens(102_000)
        result = self.run_cr("stop", stdin=json.dumps(self.payload("Stop", last_assistant_message="ok")))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_silent_below_the_limit(self):
        self.at_tokens(10_000)
        result = self.run_cr("stop", stdin=json.dumps(self.payload("Stop", last_assistant_message="ok")))
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_bad_input_never_fails_the_hook(self):
        for stdin in ("not json", "[1, 2]", ""):
            result = self.run_cr("session-start", stdin=stdin)
            self.assertEqual((result.returncode, result.stdout), (0, ""))

    def break_storage(self):
        """Make every project folder impossible to create, so the next hook fails inside."""
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "projects").write_text("not a folder", encoding="utf-8")

    def test_malformed_fields_are_ignored(self):
        result = self.run_cr("stop", stdin=json.dumps({"cwd": 12345, "transcript_path": ["x"], "session_id": {}}))
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertFalse((self.home / "error.log").exists())

    def test_errors_are_logged_not_raised(self):
        self.at_tokens(102_000)
        self.break_storage()
        result = self.run_cr("stop", stdin=json.dumps(self.payload("Stop", last_assistant_message="ok")))
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertTrue((self.home / "error.log").exists())
        if os.name != "nt":
            self.assertEqual((self.home / "error.log").stat().st_mode & 0o777, 0o600)

    def test_status(self):
        self.transcript = self.claude / "projects" / store.claude_slug(self.repo) / "session-1.jsonl"
        self.transcript.parent.mkdir(parents=True)
        self.at_tokens(10_000)
        self.run_cr("stop", stdin=json.dumps(self.payload(
            "Stop", last_assistant_message=f"<contextrail-handoff>\n{VALID_BODY}\n</contextrail-handoff>")))
        result = self.run_cr("status", str(self.repo))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Project:   {self.repo}", result.stdout)
        self.assertIn("Start:     2,000 tokens before any work; the token triggers count from there", result.stdout)
        self.assertIn("Triggers:  handoff at 100,000 tokens, wrap-up at 130,000", result.stdout)
        self.assertIn("Sessions:  a new session is offered at the wrap-up point; handoffs before it are checkpoints", result.stdout)
        self.assertIn("Why:       Handoff at 100,000 (50% of the window, before start 2,000 + soft_tokens 100,000 = 102,000)", result.stdout)
        self.assertIn("Warning:   The window size is assumed", result.stdout)
        self.at_tokens(60_000, model="claude-opus-5-5[1m]", start=50_522)
        result = self.run_cr("status", str(self.repo))
        self.assertIn("Triggers:  handoff at 150,522 tokens, wrap-up at 180,522", result.stdout)
        self.assertIn("Why:       Handoff at 150,522 (start 50,522 + soft_tokens 100,000)", result.stdout)
        self.assertNotIn("Warning:", result.stdout)
        self.assertIn("Handoff:   saved just now", result.stdout)

    def test_utf8_in_and_out_whatever_the_platform_encoding(self):
        os.environ["PYTHONIOENCODING"] = "cp1252"  # a Windows code page; TempHome restores the environment
        self.transcript = self.claude / "projects" / store.claude_slug(self.repo) / "session-1.jsonl"
        self.transcript.parent.mkdir(parents=True)
        self.at_tokens(10_000)
        body = VALID_BODY.replace("starve the rest.", "starve the rest (≈ 600/min → 429 ✓).")
        # Raw UTF-8, as Claude Code sends it, not \u escapes.
        payload = self.payload("Stop", last_assistant_message=f"<contextrail-handoff>\n{body}\n</contextrail-handoff>")
        self.run_cr("stop", stdin=json.dumps(payload, ensure_ascii=False))
        self.assertIn("(≈ 600/min → 429 ✓).", store.Project(self.repo).handoff.read_text(encoding="utf-8"))
        result = self.run_cr("status", str(self.repo))
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertIn("≈", result.stdout)

    def test_status_points_at_doctor_after_errors(self):
        self.assertNotIn("Errors:", self.run_cr("status", str(self.repo)).stdout)
        self.at_tokens(102_000)
        self.break_storage()
        self.run_cr("stop", stdin=json.dumps(self.payload("Stop", last_assistant_message="ok")))  # logs an error
        (self.home / "projects").unlink()
        self.assertIn("Errors:    1 in the last 24 h. Run /contextrail:doctor", self.run_cr("status", str(self.repo)).stdout)

    def test_hooks_record_a_heartbeat(self):
        self.at_tokens(10_000)
        self.run_cr("stop", stdin=json.dumps(self.payload("Stop", last_assistant_message="ok")))
        self.run_cr("session-start", stdin="not json")
        beats = json.loads((self.home / "heartbeat.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(beats), ["SessionStart", "Stop"])
        self.assertLessEqual(abs(beats["Stop"] - time.time()), 60)

    def test_hooks_do_nothing_in_unattended_sessions(self):
        self.at_tokens(102_000)
        stop = json.dumps(self.payload("Stop", last_assistant_message="ok"))
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ATTENDED": "0"}):
            for command, stdin in (("stop", stop), ("post-tool", stop), ("session-start", "not json")):
                self.assertEqual(self.run_cr(command, stdin=stdin).stdout, "")
            self.assertEqual([path.name for path in self.home.iterdir()], ["error.log"])  # the bad JSON; no heartbeat or state
            with mock.patch.dict(os.environ, {"CONTEXTRAIL_UNATTENDED": "1"}):
                self.assertEqual(json.loads(self.run_cr("stop", stdin=stop).stdout)["decision"], "block")
            self.run_cr("stop", stdin=json.dumps(self.payload(
                "Stop", last_assistant_message=f"<contextrail-handoff>\n{VALID_BODY}\n</contextrail-handoff>")))
            self.assertFalse(store.Project(self.repo).handoff.exists())
            project_config = store.Project(self.repo).dir / "config.json"
            project_config.write_text(json.dumps({"unattended": True}), encoding="utf-8")
            self.assertIn("handoff saved", self.run_cr("stop", stdin=json.dumps(self.payload(
                "Stop", last_assistant_message=f"<contextrail-handoff>\n{VALID_BODY}\n</contextrail-handoff>"))).stdout)
        project_config.unlink()
        self.assertIn("SessionStart", self.run_cr("session-start", stdin=json.dumps(
            self.payload("SessionStart", source="clear"))).stdout)

    def test_unwritable_home_never_fails_the_hook(self):
        self.home.write_text("a file, not a folder", encoding="utf-8")
        self.at_tokens(102_000)
        result = self.run_cr("stop", stdin=json.dumps(self.payload("Stop", last_assistant_message="ok")))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_unknown_command_and_help(self):
        self.assertEqual(self.run_cr("nope").returncode, 2)
        self.assertIn("ContextRail", self.run_cr("--help").stdout)


class PluginFilesTest(TempHome):
    def test_manifests_are_valid_json(self):
        for path in (".claude-plugin/plugin.json", ".claude-plugin/marketplace.json", "hooks/hooks.json",
                     "mods/contextrail-continue/.claude-plugin/plugin.json"):
            json.loads((ROOT / path).read_text(encoding="utf-8"))

    def test_versions_match(self):
        from contextrail import __version__
        plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
        mod = json.loads((ROOT / "mods/contextrail-continue/.claude-plugin/plugin.json").read_text(encoding="utf-8"))
        market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin["version"], __version__)
        self.assertEqual(mod["version"], __version__)
        self.assertEqual([entry["version"] for entry in market["plugins"]], [__version__, __version__])

    def test_marketplace_lists_the_mod(self):
        market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
        entry = market["plugins"][1]
        self.assertEqual(entry["name"], "contextrail-continue")
        self.assertTrue((ROOT / entry["source"] / ".claude-plugin/plugin.json").is_file())
        self.assertEqual(json.loads((ROOT / entry["source"] / "hooks/hooks.json").read_text(encoding="utf-8")), {"modules": ["./register.tsx"]})

    def test_mod_knows_a_checkpoint_request(self):
        register = (ROOT / "mods" / "contextrail-continue" / "hooks" / "register.tsx").read_text(encoding="utf-8")
        words = register.split("const CHECKPOINT = '")[1].split("'")[0]
        self.assertEqual(words, prompts.CHECKPOINT)
        self.assertIn(words, prompts.request("x tokens", 8_000, refresh=False, kind="checkpoint"))
        for kind in ("final", ""):
            self.assertNotIn(words, prompts.request("x tokens", 8_000, refresh=False, kind=kind))

    def test_mod_reads_the_request_prefix_contextrail_sends(self):
        register = (ROOT / "mods/contextrail-continue/hooks/register.tsx").read_text(encoding="utf-8")
        prefix = register.split("const REQUEST = '")[1].split("'")[0]
        self.assertTrue(prompts.request("x tokens", 8_000, refresh=False).startswith(prefix))
        self.assertTrue(prompts.request("x tokens", 8_000, refresh=True).startswith(prefix))
        self.assertTrue(prompts.request("x tokens", 8_000, refresh=False, kind="final", finished=True).startswith(prefix))

    def test_mod_skips_the_same_background_task_types(self):
        register = (ROOT / "mods/contextrail-continue/hooks/register.tsx").read_text(encoding="utf-8")
        listed = register.split("const PASSIVE = [")[1].split("]")[0]
        self.assertEqual(tuple(part.strip().strip("'") for part in listed.split(",")), handlers.PASSIVE)

    def test_mod_holds_its_offer_on_the_same_question_test(self):
        register = (ROOT / "mods/contextrail-continue/hooks/register.tsx").read_text(encoding="utf-8")
        pattern = register.split("const QUESTION_END = /")[1].split("/\n")[0]
        self.assertEqual(pattern, handlers.QUESTION_END_RE.pattern)

    def test_hooks_point_at_real_commands(self):
        hooks = json.loads((ROOT / "hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
        for event, groups in hooks.items():
            for group in groups:
                for hook in group["hooks"]:
                    command = hook["command"]
                    self.assertIn('"${CLAUDE_PLUGIN_ROOT}/contextrail/cr.py"', command)
                    self.assertEqual(handlers.HOOKS[command.split()[-1]][0], event)
        # SessionStart runs for every source; the inject_on setting decides what loads.
        self.assertNotIn("matcher", hooks["SessionStart"][0])

    def test_commands_run_cr_py(self):
        # A shell block that exits non-zero stops the slash command before the model sees it.
        for name, line in (("status", "status"), ("doctor", "doctor --exit-zero $ARGUMENTS"), ("history", "history --exit-zero $ARGUMENTS")):
            command = (ROOT / f"commands/{name}.md").read_text(encoding="utf-8")
            self.assertIn(f'```!\n"${{CLAUDE_PLUGIN_ROOT}}/contextrail/cr.py" {line}\n```', command)

    def test_handoff_command_matches_the_hook_prompt(self):
        command = (ROOT / "commands/handoff.md").read_text(encoding="utf-8")
        self.assertIn(prompts.TEMPLATE, command)
        self.assertIn(prompts.RULES.format(max_chars=config.DEFAULTS["max_chars"]), command)

    def test_example_and_test_body_follow_the_template(self):
        headings = handoff.HEADING_RE.findall(prompts.TEMPLATE)
        example = handoff.narrative_of((ROOT / "examples/HANDOFF.example.md").read_text(encoding="utf-8"))
        for body in (VALID_BODY, example):
            self.assertEqual(handoff.HEADING_RE.findall(body), headings)
        self.assertEqual(example, VALID_BODY)
        self.assertEqual(handoff.problems(VALID_BODY, config.DEFAULTS["max_chars"]), [])

    def test_entry_point_is_executable(self):
        self.assertTrue(os.access(CR, os.X_OK))
