import json
import tempfile
import threading
import time
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

    def test_entries_of_the_wrong_shape_are_skipped(self):
        write_transcript(self.path, [
            assistant(70_000, content=[{"type": "text", "text": "reply"}], message_id="m1"),
            {"type": "assistant", "message": "usage"},
            {"type": "assistant", "message": {"usage": "assistant", "content": 5}},
            {"type": "assistant", "message": {"usage": {"input_tokens": "9", "cache_read_input_tokens": True}, "content": [{"type": "text", "text": 7}]}},
        ])
        self.assertEqual(usage.last_usage(str(self.path)).tokens, 70_000)
        self.assertEqual(usage.session_start(str(self.path)), (70_000, False))
        self.assertEqual(usage.last_assistant_text(str(self.path)), "reply")

    def test_claude_version_of_the_latest_entries(self):
        write_transcript(self.path, [
            dict(user(), version="2.1.100"), dict(assistant(10), version="2.1.163"), dict(user(), version="next"),
        ])
        self.assertEqual(usage.claude_version(str(self.path)), (2, 1, 163))
        write_transcript(self.path, [user()])
        self.assertIsNone(usage.claude_version(str(self.path)))
        self.assertIsNone(usage.claude_version(None))

    def test_window_resolution(self):
        self.assertEqual(usage.resolve_window("claude-opus-5-5", 10, 500_000), (500_000, "config"))
        self.assertEqual(usage.resolve_window("claude-opus-5-5", 10, 500_000, 200_000), (500_000, "config"))
        self.assertEqual(usage.resolve_window("claude-opus-5-5[1m]", 10, None, 200_000), (200_000, "statusline"))
        self.assertEqual(usage.resolve_window("claude-opus-5-5[1m]", 10, None), (1_000_000, "model"))
        self.assertEqual(usage.resolve_window("claude-opus-5-5", 250_000, None), (1_000_000, "observed"))
        self.assertEqual(usage.resolve_window("claude-opus-5-5", 10, None), (200_000, "assumed"))

    def test_describe(self):
        line = usage.Usage(tokens=104_213, model="m", window=200_000, window_source="assumed").describe()
        self.assertEqual(line, "104,213 tokens of context (≈52% of a 200k window, assumed)")
        line = usage.Usage(tokens=100_000, model="m", window=1_000_000, window_source="config").describe()
        self.assertEqual(line, "100,000 tokens of context (≈10% of a 1M window)")


# The line Claude Code 2.1.308 writes where it compacts a transcript, trimmed.
COMPACT_BOUNDARY = {
    "type": "system", "subtype": "compact_boundary", "isSidechain": False,
    "compactMetadata": {"trigger": "auto", "preTokens": 490_032, "postTokens": 21_306},
}


def listing(kind="skill_listing"):
    """A line where Claude Code 2.1.309 lists skills, agents, MCP instructions or deferred tools, trimmed."""
    return {
        "parentUuid": "d1db09c2", "isSidechain": False, "type": "attachment",
        "attachment": {"type": kind, "content": "- gsd-help: Show available GSD commands", "isInitial": True},
    }


class StartTokensTest(LastUsageTest):
    def test_first_main_thread_reply(self):
        synthetic = assistant(0)
        synthetic["message"]["usage"] = {"input_tokens": 0, "output_tokens": 0}
        write_transcript(self.path, [
            user(), tool_result("t0", text="what compact_boundary means"), assistant(30_000, sidechain=True),
            synthetic, assistant(50_522), user(), assistant(90_000),
        ])
        self.assertEqual(usage.start_tokens(str(self.path)), 50_522)

    def test_first_reply_after_the_latest_compaction(self):
        write_transcript(self.path, [
            user(), assistant(50_000), assistant(400_000), COMPACT_BOUNDARY, user(), assistant(70_000),
            assistant(300_000), COMPACT_BOUNDARY, user(), assistant(66_212), assistant(80_000),
        ])
        self.assertEqual(usage.start_tokens(str(self.path)), 66_212)
        write_transcript(self.path, [user(), assistant(50_000), COMPACT_BOUNDARY, user()])
        self.assertIsNone(usage.start_tokens(str(self.path)))

    def test_missing_or_empty_transcript(self):
        self.assertIsNone(usage.start_tokens(str(self.path)))
        self.assertIsNone(usage.start_tokens(None))
        write_transcript(self.path, [user()])
        self.assertIsNone(usage.start_tokens(str(self.path)))

    def test_final_once_the_next_reply_exists(self):
        # One reply is written as a line per content block, all with its message id.
        write_transcript(self.path, [user(), listing(), assistant(51_438, message_id="m1"), assistant(51_438, message_id="m1")])
        self.assertEqual(usage.session_start(str(self.path)), (51_438, False))
        write_transcript(self.path, [
            user(), listing(), assistant(51_438, message_id="m1"), tool_result("t1"), assistant(58_267), listing(),
        ])
        self.assertEqual(usage.session_start(str(self.path)), (51_438, True))

    def test_listings_sent_after_the_first_reply(self):
        # As in a session started by contextrail-continue's prompt on Claude Code 2.1.309.
        entries = [user(), assistant(30_726, message_id="m1"), assistant(30_726, message_id="m1"), tool_result("t1")]
        entries += [listing(kind) for kind in usage.LISTINGS] + [assistant(61_159), assistant(64_435)]
        write_transcript(self.path, entries)
        self.assertEqual(usage.session_start(str(self.path)), (61_159, True))
        self.assertEqual(usage.start_tokens(str(self.path)), 61_159)
        write_transcript(self.path, entries[:-2])
        self.assertEqual(usage.session_start(str(self.path)), (30_726, False))

    def test_only_listing_attachments_move_the_start(self):
        sidechain = dict(listing(), isSidechain=True)
        write_transcript(self.path, [
            user(), assistant(30_726), tool_result("t1", text='{"type": "skill_listing"}'), sidechain, assistant(36_000),
        ])
        self.assertEqual(usage.session_start(str(self.path)), (30_726, True))
        write_transcript(self.path, [
            user(), assistant(50_000), COMPACT_BOUNDARY, user(), assistant(20_000), listing(), assistant(45_000),
        ])
        self.assertEqual(usage.session_start(str(self.path)), (45_000, True))


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


def append_later(path: Path, entry: dict, seconds: float) -> threading.Thread:
    """Append a transcript line after a delay, as Claude Code writes the turn's last reply
    a moment after the Stop hook starts."""
    def write():
        time.sleep(seconds)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    thread = threading.Thread(target=write)
    thread.start()
    return thread


class AwaitReplyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "t.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_returns_at_once_when_the_reply_is_there(self):
        write_transcript(self.path, [user(), assistant(10, content=[{"type": "text", "text": "All done."}])])
        began = time.monotonic()
        self.assertTrue(usage.await_reply(str(self.path), "All done."))
        self.assertLess(time.monotonic() - began, 0.5)

    def test_ignores_whitespace_and_how_blocks_are_joined(self):
        write_transcript(self.path, [
            user(),
            assistant(10, content=[{"type": "text", "text": "First part."}], message_id="m1"),
            assistant(10, content=[{"type": "text", "text": "Second  part."}], message_id="m1"),
        ])
        self.assertTrue(usage.await_reply(str(self.path), "First part.\n\nSecond part.", timeout=0))

    def test_waits_for_a_reply_written_late(self):
        write_transcript(self.path, [user(), assistant(90_000, content=[{"type": "text", "text": "Reading the files."}])])
        late = append_later(self.path, assistant(110_000, content=[{"type": "text", "text": "The files are updated."}]), 0.15)
        try:
            began = time.monotonic()
            self.assertTrue(usage.await_reply(str(self.path), "The files are updated.", timeout=2))
            self.assertGreaterEqual(time.monotonic() - began, 0.1)
        finally:
            late.join()
        self.assertEqual(usage.last_usage(str(self.path)).tokens, 110_000)

    def test_gives_up_after_the_wait(self):
        write_transcript(self.path, [user(), assistant(10, content=[{"type": "text", "text": "Something else."}])])
        began = time.monotonic()
        self.assertFalse(usage.await_reply(str(self.path), "The reply that never comes.", timeout=0.1))
        self.assertGreaterEqual(time.monotonic() - began, 0.1)

    def test_nothing_to_compare(self):
        write_transcript(self.path, [user(), assistant(10)])
        for transcript, message in ((str(self.path), ""), (str(self.path), " \n"), (None, "ok"), ("", "ok")):
            self.assertFalse(usage.await_reply(transcript, message, timeout=5))


if __name__ == "__main__":
    unittest.main()
