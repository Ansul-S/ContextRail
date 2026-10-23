import json
import os
from unittest import mock

from contextrail import config
from tests.helpers import TempHome


class ConfigTest(TempHome):
    def test_defaults(self):
        cfg = config.load()
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["soft_tokens"], 100_000)
        self.assertFalse(cfg["copy_resume_prompt"])  # set by the test environment

    def test_layers_global_project_env(self):
        self.home.mkdir(parents=True)
        (self.home / "config.json").write_text(json.dumps({"soft_tokens": 90_000, "hard_tokens": 120_000}))
        project_dir = self.home / "projects" / "p"
        project_dir.mkdir(parents=True)
        (project_dir / "config.json").write_text(json.dumps({"soft_tokens": 80_000}))
        with mock.patch.dict(os.environ, {"CONTEXTRAIL_HARD_TOKENS": "110000"}):
            cfg = config.load(project_dir)
        self.assertEqual(cfg["soft_tokens"], 80_000)
        self.assertEqual(cfg["hard_tokens"], 110_000)

    def test_bad_values_are_ignored(self):
        self.home.mkdir(parents=True)
        (self.home / "config.json").write_text(json.dumps({
            "soft_tokens": "lots", "soft_pct": 150, "enabled": "maybe", "inject_on": [1], "unknown": 1,
        }))
        cfg = config.load()
        self.assertEqual(cfg["soft_tokens"], 100_000)
        self.assertEqual(cfg["soft_pct"], 50)
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["inject_on"], ["startup", "clear", "compact"])
        self.assertNotIn("unknown", cfg)

    def test_unreadable_config_falls_back(self):
        self.home.mkdir(parents=True)
        (self.home / "config.json").write_text("{not json")
        self.assertEqual(config.load()["soft_tokens"], 100_000)

    def test_env_parsing(self):
        env = {"CONTEXTRAIL_INJECT_ON": "clear, compact", "CONTEXTRAIL_WINDOW_TOKENS": "1000000",
               "CONTEXTRAIL_ENABLED": "off"}
        with mock.patch.dict(os.environ, env):
            cfg = config.load()
        self.assertEqual(cfg["inject_on"], ["clear", "compact"])
        self.assertEqual(cfg["window_tokens"], 1_000_000)
        self.assertFalse(cfg["enabled"])

    def test_disable_switch(self):
        with mock.patch.dict(os.environ, {"CONTEXTRAIL_DISABLE": "1"}):
            self.assertFalse(config.load()["enabled"])

    def test_attended_unless_claude_code_says_otherwise(self):
        self.assertTrue(config.attended())
        for value, expected in (("1", True), ("0", False), (" 0 ", False), ("", True)):
            with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ATTENDED": value}):
                self.assertEqual(config.attended(), expected, value)
        self.assertFalse(config.load()["unattended"])
        with mock.patch.dict(os.environ, {"CONTEXTRAIL_UNATTENDED": "1"}):
            self.assertTrue(config.load()["unattended"])

    def test_problems_lists_what_load_ignores(self):
        self.assertEqual(config.problems(), [])
        self.home.mkdir(parents=True)
        (self.home / "config.json").write_text(json.dumps({
            "sof_tokens": 1, "soft_pct": 150, "inject_on": ["startup", "Resume"], "hard_tokens": 120_000,
        }))
        project_dir = self.home / "projects" / "p"
        project_dir.mkdir(parents=True)
        (project_dir / "config.json").write_text("[1, 2]")
        env = {"CONTEXTRAIL_SOFT_TOKENS": "lots", "CONTEXTRAIL_DISABLE": "1", "CONTEXTRAIL_WINDOW": "1000000"}
        with mock.patch.dict(os.environ, env):
            found = config.problems(project_dir)
        path = self.home / "config.json"
        self.assertEqual(found, [
            f"{path}: 'sof_tokens' is not a setting (did you mean 'soft_tokens'?)",
            f"{path}: 'soft_pct' has an invalid value, 150",
            f"{path}: 'inject_on' has unknown sources ['Resume']; use startup, resume, clear, compact",
            f"{project_dir / 'config.json'} must hold a JSON object",
            "CONTEXTRAIL_SOFT_TOKENS has an invalid value, \"lots\"",
            "CONTEXTRAIL_WINDOW is not a setting (did you mean 'window_tokens'?)",
        ])

    def test_problems_with_unreadable_json(self):
        self.home.mkdir(parents=True)
        (self.home / "config.json").write_text("{not json")
        [problem] = config.problems()
        self.assertTrue(problem.startswith(f"{self.home / 'config.json'} is not valid JSON ("), problem)

    def test_thresholds(self):
        cfg = config.load()
        self.assertEqual(config.thresholds(cfg, 200_000), config.Thresholds(soft=100_000, hard=130_000))
        self.assertEqual(config.thresholds(cfg, 1_000_000), config.Thresholds(soft=100_000, hard=130_000))
        self.assertEqual(config.thresholds(cfg, 128_000), config.Thresholds(soft=64_000, hard=83_200))
        cfg.update(soft_tokens=300_000, hard_tokens=100_000)
        self.assertEqual(config.thresholds(cfg, 1_000_000), config.Thresholds(soft=300_000, hard=300_000))

    def test_thresholds_count_from_the_session_start(self):
        cfg = config.load()
        self.assertEqual(config.thresholds(cfg, 1_000_000, 50_522), config.Thresholds(soft=150_522, hard=180_522))
        # A small window keeps its share caps, so it still hands off well before auto-compaction.
        self.assertEqual(config.thresholds(cfg, 200_000, 50_522), config.Thresholds(soft=100_000, hard=130_000))
        self.assertEqual(config.thresholds(cfg, 200_000, 20_000), config.Thresholds(soft=100_000, hard=130_000))
        self.assertEqual(config.thresholds(cfg, 400_000, 50_000), config.Thresholds(soft=150_000, hard=180_000))
        self.assertEqual(config.thresholds(cfg, 250_000, 50_000), config.Thresholds(soft=125_000, hard=162_500))
