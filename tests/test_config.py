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

    def test_thresholds(self):
        cfg = config.load()
        self.assertEqual(config.thresholds(cfg, 200_000), config.Thresholds(soft=100_000, hard=130_000))
        self.assertEqual(config.thresholds(cfg, 1_000_000), config.Thresholds(soft=100_000, hard=130_000))
        self.assertEqual(config.thresholds(cfg, 128_000), config.Thresholds(soft=64_000, hard=83_200))
        cfg.update(soft_tokens=300_000, hard_tokens=100_000)
        self.assertEqual(config.thresholds(cfg, 1_000_000), config.Thresholds(soft=300_000, hard=300_000))
