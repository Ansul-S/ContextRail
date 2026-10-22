import tempfile
import unittest
from pathlib import Path

from contextrail import usage
from tests.helpers import assistant, tool_result, user, write_transcript


class ReverseLinesTest(unittest.TestCase):
    def test_yields_lines_last_first_across_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.txt"
            lines = [f"line {i} " + "x" * (i * 37 % 500) for i in range(2000)]
            path.write_text("\n".join(lines) + "\n")
            original = usage.CHUNK
            usage.CHUNK = 1024
            try:
                got = [line.decode() for line in usage.reverse_lines(str(path))]
            finally:
                usage.CHUNK = original
            self.assertEqual(got, list(reversed(lines)))

    def test_line_longer_than_a_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.txt"
            long_line = "y" * 10_000
            path.write_text(f"first\n{long_line}\nlast")
            original = usage.CHUNK
            usage.CHUNK = 512
            try:
                got = [line.decode() for line in usage.reverse_lines(str(path))]
            finally:
                usage.CHUNK = original
            self.assertEqual(got, ["last", long_line, "first"])

    def test_scan_limit_drops_the_partial_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.txt"
            path.write_text("a" * 100 + "\nshort\n")
            got = [line.decode() for line in usage.reverse_lines(str(path), max_bytes=20)]
            self.assertEqual(got, ["short"])


class LastUsageTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "t.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_takes_latest_main_thread_message(self):
        write_transcript(self.path, [
            user(), assistant(50_000), tool_result("t1", text="z" * 300_000),
            assistant(90_000, sidechain=True),
        ])
        got = usage.last_usage(str(self.path))
        self.assertEqual(got.tokens, 50_000)
        self.assertEqual((got.window, got.window_source), (200_000, "assumed"))
        self.assertEqual(round(got.pct), 25)

    def test_skips_messages_without_usage(self):
        synthetic = assistant(0)
        synthetic["message"]["usage"] = {"input_tokens": 0, "output_tokens": 0}
        write_transcript(self.path, [assistant(70_000), synthetic])
        self.assertEqual(usage.last_usage(str(self.path)).tokens, 70_000)

    def test_missing_or_empty_transcript(self):
        self.assertIsNone(usage.last_usage(str(self.path)))
        self.assertIsNone(usage.last_usage(None))
        write_transcript(self.path, [user()])
        self.assertIsNone(usage.last_usage(str(self.path)))

    def test_window_resolution(self):
        self.assertEqual(usage.resolve_window("claude-opus-5-5", 10, 500_000), (500_000, "config"))
        self.assertEqual(usage.resolve_window("claude-opus-5-5[1m]", 10, None), (1_000_000, "model"))
        self.assertEqual(usage.resolve_window("claude-opus-5-5", 250_000, None), (1_000_000, "observed"))
        self.assertEqual(usage.resolve_window("claude-opus-5-5", 10, None), (200_000, "assumed"))

    def test_describe(self):
        line = usage.Usage(tokens=104_213, model="m", window=200_000, window_source="assumed").describe()
        self.assertEqual(line, "104,213 tokens of context (≈52% of a 200k window, assumed)")
        line = usage.Usage(tokens=100_000, model="m", window=1_000_000, window_source="config").describe()
        self.assertEqual(line, "100,000 tokens of context (≈10% of a 1M window)")


class LastAssistantTextTest(unittest.TestCase):
    def test_joins_text_blocks_of_the_latest_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            write_transcript(path, [
                assistant(10, content=[{"type": "text", "text": "old"}]),
                user(),
                assistant(10, content=[{"type": "text", "text": "part one"}], message_id="m2"),
                assistant(10, content=[{"type": "text", "text": "part two"}], message_id="m2"),
                assistant(10, content=[{"type": "text", "text": "side"}], sidechain=True),
            ])
            self.assertEqual(usage.last_assistant_text(str(path)), "part one\npart two")


if __name__ == "__main__":
    unittest.main()
