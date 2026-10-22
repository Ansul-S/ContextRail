import json
import os
import subprocess
import sys
import time

from contextrail import config, handlers, handoff, prompts
from tests.helpers import CR, ROOT, VALID_BODY, TempHome


class CliTest(TempHome):
    def run_cr(self, *args, stdin=""):
        return subprocess.run(
            [sys.executable, str(CR), *args], input=stdin, capture_output=True, text=True,
            env=dict(os.environ), timeout=30,
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

    def test_errors_are_logged_not_raised(self):
        result = self.run_cr("stop", stdin=json.dumps({"cwd": 12345, "transcript_path": ["x"]}))
        self.assertEqual(result.returncode, 0)
        self.assertTrue((self.home / "error.log").exists())

    def test_status(self):
        self.at_tokens(10_000)
        self.run_cr("stop", stdin=json.dumps(self.payload(
            "Stop", last_assistant_message=f"<contextrail-handoff>\n{VALID_BODY}\n</contextrail-handoff>")))
        result = self.run_cr("status", str(self.repo))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Project:   {self.repo}", result.stdout)
        self.assertIn("Triggers:  handoff at 100,000 tokens, wrap-up at 130,000", result.stdout)
        self.assertIn("Handoff:   saved just now", result.stdout)

    def test_status_points_at_doctor_after_errors(self):
        self.assertNotIn("Errors:", self.run_cr("status", str(self.repo)).stdout)
        self.run_cr("stop", stdin=json.dumps({"cwd": 12345, "transcript_path": ["x"]}))  # logs an error
        self.assertIn("Errors:    1 in the last 24 h. Run /contextrail:doctor", self.run_cr("status", str(self.repo)).stdout)

    def test_hooks_record_a_heartbeat(self):
        self.at_tokens(10_000)
        self.run_cr("stop", stdin=json.dumps(self.payload("Stop", last_assistant_message="ok")))
        self.run_cr("session-start", stdin="not json")
        beats = json.loads((self.home / "heartbeat.json").read_text())
        self.assertEqual(sorted(beats), ["SessionStart", "Stop"])
        self.assertLessEqual(abs(beats["Stop"] - time.time()), 60)

    def test_unwritable_home_never_fails_the_hook(self):
        self.home.write_text("a file, not a folder")
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
            json.loads((ROOT / path).read_text())

    def test_versions_match(self):
        from contextrail import __version__
        plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
        mod = json.loads((ROOT / "mods/contextrail-continue/.claude-plugin/plugin.json").read_text())
        market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
        self.assertEqual(plugin["version"], __version__)
        self.assertEqual(mod["version"], __version__)
        self.assertEqual([entry["version"] for entry in market["plugins"]], [__version__, __version__])

    def test_marketplace_lists_the_mod(self):
        market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
        entry = market["plugins"][1]
        self.assertEqual(entry["name"], "contextrail-continue")
        self.assertTrue((ROOT / entry["source"] / ".claude-plugin/plugin.json").is_file())
        self.assertEqual(json.loads((ROOT / entry["source"] / "hooks/hooks.json").read_text()), {"modules": ["./register.ts"]})

    def test_mod_reads_the_request_prefix_contextrail_sends(self):
        register = (ROOT / "mods/contextrail-continue/hooks/register.ts").read_text()
        prefix = register.split("const REQUEST = '")[1].split("'")[0]
        self.assertTrue(prompts.request("x tokens", 8_000, refresh=False).startswith(prefix))
        self.assertTrue(prompts.request("x tokens", 8_000, refresh=True).startswith(prefix))

    def test_hooks_point_at_real_commands(self):
        hooks = json.loads((ROOT / "hooks/hooks.json").read_text())["hooks"]
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
        for name, line in (("status", "status"), ("doctor", "doctor --exit-zero"), ("history", "history --exit-zero $ARGUMENTS")):
            command = (ROOT / f"commands/{name}.md").read_text()
            self.assertIn(f'```!\n"${{CLAUDE_PLUGIN_ROOT}}/contextrail/cr.py" {line}\n```', command)

    def test_handoff_command_matches_the_hook_prompt(self):
        command = (ROOT / "commands/handoff.md").read_text()
        self.assertIn(prompts.TEMPLATE, command)
        self.assertIn(prompts.RULES.format(max_chars=config.DEFAULTS["max_chars"]), command)

    def test_example_and_test_body_follow_the_template(self):
        headings = handoff.HEADING_RE.findall(prompts.TEMPLATE)
        example = handoff.narrative_of((ROOT / "examples/HANDOFF.example.md").read_text())
        for body in (VALID_BODY, example):
            self.assertEqual(handoff.HEADING_RE.findall(body), headings)
        self.assertEqual(example, VALID_BODY)
        self.assertEqual(handoff.problems(VALID_BODY, config.DEFAULTS["max_chars"]), [])

    def test_entry_point_is_executable(self):
        self.assertTrue(os.access(CR, os.X_OK))
