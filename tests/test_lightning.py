import io
import json
import os
from contextlib import redirect_stdout
from unittest import mock

from contextrail import config, lightning, status
from tests.helpers import TempHome


class LightningTest(TempHome):
    def run_main(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = lightning.main(list(args))
        return code, out.getvalue()

    def config_file(self):
        return json.loads((self.home / "config.json").read_text(encoding="utf-8"))

    def enable_continue(self, on=True):
        self.claude.mkdir(parents=True, exist_ok=True)
        (self.claude / "settings.json").write_text(json.dumps({"enabledPlugins": {"contextrail-continue@contextrail": on}}), encoding="utf-8")

    def test_parse(self):
        for text, tokens in (("200k", 200_000), ("0.2M", 200_000), ("200000", 200_000), ("200,000", 200_000),
                             (" 1.5m ", 1_500_000), ("off", 0), ("OFF", 0), ("0", 0)):
            self.assertEqual(lightning.parse(text), tokens, text)
        for text in ("", "on", "-5k", "200kk", "1e5", "k", "nan"):
            self.assertIsNone(lightning.parse(text), text)

    def test_sets_and_turns_off_keeping_other_settings(self):
        self.home.mkdir(parents=True)
        (self.home / "config.json").write_text(json.dumps({"refresh_tokens": 30_000}), encoding="utf-8")
        self.enable_continue()
        code, out = self.run_main("200k")
        self.assertEqual(code, 0)
        self.assertEqual(self.config_file(), {"refresh_tokens": 30_000, "lightning_tokens": 200_000})
        self.assertTrue(out.startswith("Lightning Mode is on at 200k. Once the context reaches 200,000 tokens"), out)
        self.assertNotIn("contextrail-continue", out)
        self.assertEqual(config.load()["lightning_tokens"], 200_000)
        self.assertIn("Lightning Mode is on at 200k.", self.run_main()[1])
        code, out = self.run_main("off")
        self.assertEqual((code, self.config_file()["lightning_tokens"]), (0, 0))
        self.assertTrue(out.startswith("Lightning Mode is off."), out)

    def test_bad_input_changes_nothing(self):
        for args in (("soon",), ("200k", "300k"), ("5k",)):
            code, out = self.run_main(*args)
            self.assertEqual(code, 2, args)
            self.assertEqual(self.run_main("--exit-zero", *args)[0], 0, args)
        self.assertIn("too small", out)
        self.assertFalse((self.home / "config.json").exists())
        self.home.mkdir(parents=True)
        (self.home / "config.json").write_text("{broken", encoding="utf-8")
        code, out = self.run_main("200k")
        self.assertEqual(code, 1)
        self.assertIn("left as it is", out)
        self.assertEqual((self.home / "config.json").read_text(encoding="utf-8"), "{broken")

    def test_notes_what_it_needs_and_what_overrides_it(self):
        self.enable_continue(False)
        with mock.patch.dict(os.environ, {"CONTEXTRAIL_LIGHTNING_TOKENS": "0"}):
            out = self.run_main("200k")[1]
        self.assertIn("needs the contextrail-continue plugin", out)
        self.assertIn("CONTEXTRAIL_LIGHTNING_TOKENS=0 is set, and it overrides", out)

    def test_status_shows_it(self):
        self.run_main("200k")
        out = io.StringIO()
        with redirect_stdout(out):
            status.main([str(self.repo)])
        self.assertIn("Lightning: on at 200,000", out.getvalue())
