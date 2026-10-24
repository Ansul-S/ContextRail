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
        (self.home / "config.json").write_text(json.dumps({"soft_tokens": 90_000, "hard_tokens": 120_000}), encoding="utf-8")
        project_dir = self.home / "projects" / "p"
        project_dir.mkdir(parents=True)
        (project_dir / "config.json").write_text(json.dumps({"soft_tokens": 80_000}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"CONTEXTRAIL_HARD_TOKENS": "110000"}):
            cfg = config.load(project_dir)
        self.assertEqual(cfg["soft_tokens"], 80_000)
        self.assertEqual(cfg["hard_tokens"], 110_000)

    def test_bad_values_are_ignored(self):
        self.home.mkdir(parents=True)
        (self.home / "config.json").write_text(json.dumps({
            "soft_tokens": "lots", "soft_pct": 150, "enabled": "maybe", "inject_on": [1], "unknown": 1,
        }), encoding="utf-8")
        cfg = config.load()
        self.assertEqual(cfg["soft_tokens"], 100_000)
        self.assertEqual(cfg["soft_pct"], 50)
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["inject_on"], ["startup", "clear", "compact"])
        self.assertNotIn("unknown", cfg)

    def test_numbers_too_big_for_an_int_are_ignored(self):
        self.home.mkdir(parents=True)
        (self.home / "config.json").write_text('{"hard_tokens": 1e400}', encoding="utf-8")
        self.assertEqual(config.load()["hard_tokens"], 130_000)
        self.assertEqual(config.problems(), [f"{config.tilde(self.home / 'config.json')}: 'hard_tokens' has an invalid value, Infinity"])

    def test_unreadable_config_falls_back(self):
        self.home.mkdir(parents=True)
        (self.home / "config.json").write_text("{not json", encoding="utf-8")
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
        }), encoding="utf-8")
        project_dir = self.home / "projects" / "p"
        project_dir.mkdir(parents=True)
        (project_dir / "config.json").write_text("[1, 2]", encoding="utf-8")
        env = {"CONTEXTRAIL_SOFT_TOKENS": "lots", "CONTEXTRAIL_DISABLE": "1", "CONTEXTRAIL_WINDOW": "1000000"}
        with mock.patch.dict(os.environ, env):
            found = config.problems(project_dir)
        path = config.tilde(self.home / "config.json")
        self.assertEqual(found, [
            f"{path}: 'sof_tokens' is not a setting (did you mean 'soft_tokens'?)",
            f"{path}: 'soft_pct' has an invalid value, 150",
            f"{path}: 'inject_on' has unknown sources ['Resume']; use startup, resume, clear, compact",
            f"{config.tilde(project_dir / 'config.json')} must hold a JSON object",
            "CONTEXTRAIL_SOFT_TOKENS has an invalid value, \"lots\"",
            "CONTEXTRAIL_WINDOW is not a setting (did you mean 'window_tokens'?)",
        ])

    def test_problems_with_unreadable_json(self):
        self.home.mkdir(parents=True)
        (self.home / "config.json").write_text("{not json", encoding="utf-8")
        [problem] = config.problems()
        self.assertTrue(problem.startswith(f"{config.tilde(self.home / 'config.json')} is not valid JSON ("), problem)

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

    def write_config(self, **settings):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.json").write_text(json.dumps(settings), encoding="utf-8")

    def test_fallback_thresholds_when_none_are_set(self):
        cfg = config.load()
        self.assertEqual((cfg["soft_tokens"], cfg["hard_tokens"], cfg["soft_pct"], cfg["hard_pct"]), (100_000, 130_000, 50, 65))
        self.assertEqual(cfg["new_session_at"], "hard")
        self.assertTrue(config.splits(cfg))

    def test_custom_thresholds_on_each_window(self):
        self.write_config(soft_tokens=400_000, hard_tokens=550_000)
        cfg = config.load()
        self.assertEqual(config.problems(), [])
        self.assertEqual(config.thresholds(cfg, 1_000_000, 30_890), config.Thresholds(soft=430_890, hard=580_890))
        # Smaller windows keep their caps: the percentages can only bring a point forward.
        self.assertEqual(config.thresholds(cfg, 200_000, 30_890), config.Thresholds(soft=100_000, hard=130_000))
        self.assertEqual(config.thresholds(cfg, 500_000, 30_890), config.Thresholds(soft=250_000, hard=325_000))
        self.assertEqual(config.thresholds(cfg, 1_000_000, 120_000), config.Thresholds(soft=500_000, hard=650_000))

    def test_each_threshold_can_be_set_alone(self):
        self.write_config(hard_tokens=200_000)
        self.assertEqual((config.load()["soft_tokens"], config.load()["hard_tokens"]), (100_000, 200_000))
        # A raised soft_tokens moves the unset hard_tokens along, keeping the fallback gap.
        self.write_config(soft_tokens=300_000)
        cfg = config.load()
        self.assertEqual((cfg["soft_tokens"], cfg["hard_tokens"]), (300_000, 330_000))
        self.assertEqual(config.problems(), [])
        self.write_config(soft_tokens=90_000)
        self.assertEqual(config.load()["hard_tokens"], 130_000)

    def test_percentage_caps_are_settings_too(self):
        self.write_config(soft_tokens=400_000, hard_tokens=550_000, soft_pct=100, hard_pct=100)
        cfg = config.load()
        self.assertEqual(config.thresholds(cfg, 200_000, 30_890), config.Thresholds(soft=200_000, hard=200_000))
        self.assertEqual(config.thresholds(cfg, 1_000_000, 30_890), config.Thresholds(soft=430_890, hard=580_890))
        self.write_config(soft_pct=30, hard_pct=40)
        self.assertEqual(config.thresholds(config.load(), 1_000_000, 50_000), config.Thresholds(soft=150_000, hard=180_000))
        self.assertEqual(config.thresholds(config.load(), 200_000, 50_000), config.Thresholds(soft=60_000, hard=80_000))

    def test_invalid_thresholds_are_ignored_and_reported(self):
        self.write_config(soft_tokens=0, hard_tokens=-5, soft_pct=0, hard_pct=101, new_session_at="later")
        cfg = config.load()
        self.assertEqual(
            (cfg["soft_tokens"], cfg["hard_tokens"], cfg["soft_pct"], cfg["hard_pct"], cfg["new_session_at"]),
            (100_000, 130_000, 50, 65, "hard"),
        )
        reported = " ".join(config.problems())
        for key in ("soft_tokens", "hard_tokens", "soft_pct", "hard_pct", "new_session_at"):
            self.assertIn(f"'{key}' has an invalid value", reported)
        self.write_config(new_session_at=" Soft ")
        self.assertEqual(config.load()["new_session_at"], "soft")
        self.assertFalse(config.splits(config.load()))

    def test_hard_must_be_above_soft(self):
        self.write_config(soft_tokens=300_000, hard_tokens=200_000)
        self.assertEqual(config.load()["hard_tokens"], 330_000)
        self.assertEqual(config.problems(), [
            "hard_tokens (200,000) must be above soft_tokens (300,000); ContextRail uses 330,000 until it is"
        ])
        self.write_config(soft_tokens=300_000, hard_tokens=300_000)
        self.assertEqual(len(config.problems()), 1)
        # The pair is checked after every source is applied.
        self.write_config(soft_tokens=300_000, hard_tokens=400_000)
        with mock.patch.dict(os.environ, {"CONTEXTRAIL_HARD_TOKENS": "250000"}):
            self.assertEqual(config.load()["hard_tokens"], 330_000)
            self.assertIn("hard_tokens (250,000) must be above", config.problems()[0])

    def test_explain_names_what_decided_each_point(self):
        cfg = dict(config.load(), soft_tokens=400_000, hard_tokens=550_000)
        limits = config.thresholds(cfg, 1_000_000, 30_890)
        self.assertEqual(config.explain(cfg, limits, 1_000_000, "statusline", 30_890), [
            "Handoff at 430,890 (start 30,890 + soft_tokens 400,000); wrap-up at 580,890 "
            "(start + hard_tokens 550,000). Window 1,000,000 tokens, from the status line."
        ])
        limits = config.thresholds(cfg, 200_000, 30_890)
        first, warning = config.explain(cfg, limits, 200_000, "assumed", 30_890)
        self.assertEqual(first, (
            "Handoff at 100,000 (50% of the window, before start 30,890 + soft_tokens 400,000 = 430,890); "
            "wrap-up at 130,000 (65% of the window, before start + hard_tokens 550,000 = 580,890). "
            "Window 200,000 tokens, from an assumption."
        ))
        self.assertIn("Its 50% cap puts the handoff at 100,000 instead of 430,890.", warning)
        self.assertIn("window_tokens", warning)

    def test_an_assumed_window_warns_even_when_the_caps_do_not_decide(self):
        cfg = dict(config.load(), soft_tokens=50_000, hard_tokens=80_000)
        lines = config.explain(cfg, config.thresholds(cfg, 200_000, 2_000), 200_000, "assumed", 2_000)
        self.assertIn("Handoff at 52,000 (start 2,000 + soft_tokens 50,000)", lines[0])
        self.assertEqual(len(lines), 2)
        self.assertIn("The 50% and 65% caps are shares of this assumed size.", lines[1])

    def test_lightning_counts_the_whole_context(self):
        cfg = config.load()
        cfg.update(lightning_tokens=200_000)
        self.assertEqual(config.thresholds(cfg, 1_000_000, 61_454), config.Thresholds(soft=200_000, hard=230_000))
        # A session that starts near the limit still gets some work, and the window shares still cap it.
        self.assertEqual(config.thresholds(cfg, 1_000_000, 190_000), config.Thresholds(soft=210_000, hard=240_000))
        self.assertEqual(config.thresholds(cfg, 200_000, 50_000), config.Thresholds(soft=100_000, hard=130_000))
        cfg.update(lightning_tokens=600_000)
        self.assertEqual(config.thresholds(cfg, 1_000_000, 50_000), config.Thresholds(soft=500_000, hard=530_000))

    def test_lightning_is_global_only(self):
        self.home.mkdir(parents=True)
        (self.home / "config.json").write_text(json.dumps({"lightning_tokens": 200_000}), encoding="utf-8")
        project_dir = self.home / "projects" / "p"
        project_dir.mkdir(parents=True)
        (project_dir / "config.json").write_text(json.dumps({"lightning_tokens": 0, "soft_tokens": 80_000}), encoding="utf-8")
        cfg = config.load(project_dir)
        self.assertEqual((cfg["lightning_tokens"], cfg["soft_tokens"]), (200_000, 80_000))
        self.assertEqual(
            config.problems(project_dir),
            [f"{config.tilde(project_dir / 'config.json')}: 'lightning_tokens' works only in {config.tilde(self.home / 'config.json')}"],
        )
        with mock.patch.dict(os.environ, {"CONTEXTRAIL_LIGHTNING_TOKENS": "0"}):
            self.assertEqual(config.load(project_dir)["lightning_tokens"], 0)
