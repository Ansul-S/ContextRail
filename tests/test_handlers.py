import json
import os
import time
from unittest import mock

from contextrail import handlers, handoff, prompts, store, usage
from tests.helpers import START, VALID_BODY, TempHome, assistant, git, tool_result, tool_use, user, write_transcript
from tests.test_usage import append_later


def block_message(body=VALID_BODY):
    return f"Here is the handoff.\n\n<contextrail-handoff>\n{body}\n</contextrail-handoff>"


# One element of a Stop payload's background_tasks, as Claude Code 2.1.308 sends it.
TASK = {"id": "b3v0zo00w", "type": "shell", "status": "running", "description": "pytest -q", "command": "pytest -q"}


class StopTest(TempHome):
    def stop(self, **extra):
        return handlers.on_stop(self.payload("Stop", **extra))

    def project(self):
        return store.Project(self.repo)

    def test_quiet_below_the_soft_limit(self):
        for tokens in (30_000, 60_000, 99_999):
            self.at_tokens(tokens)
            self.assertIsNone(self.stop(last_assistant_message="ok"))

    def test_requests_a_handoff_at_the_soft_limit(self):
        self.at_tokens(102_000)
        out = self.stop(last_assistant_message="ok")
        self.assertEqual(out["decision"], "block")
        self.assertIn("102,000 tokens of context", out["reason"])
        self.assertIn("<contextrail-handoff>", out["reason"])
        # Before the hard point the handoff is a checkpoint: no new session is offered after it.
        self.assertIn(prompts.CHECKPOINT, out["reason"])
        self.assertIn("asking for a checkpoint handoff. Work goes on after it", out["systemMessage"])

    def test_measures_the_reply_that_ended_the_turn(self):
        # Claude Code writes the turn's last reply to the transcript a moment after the Stop hook starts.
        last = assistant(110_000, content=[{"type": "text", "text": "The three files are updated."}])
        write_transcript(self.transcript, [user(), assistant(START), assistant(90_000)])
        self.assertIsNone(self.stop(last_assistant_message="The three files are updated."))  # read at once: 90,000
        late = append_later(self.transcript, last, 0.15)
        try:
            with mock.patch.object(usage, "REPLY_WAIT", 2.0):
                out = self.stop(last_assistant_message="The three files are updated.")
        finally:
            late.join()
        self.assertEqual(out["decision"], "block")
        self.assertIn("110,000 tokens of context", out["reason"])

    def test_waits_for_the_reply_only_near_the_next_request(self):
        with mock.patch.object(usage, "REPLY_WAIT", 0.3):
            # The next request is at 100,000: the 50% cap of the assumed 200k window.
            for tokens, waits in ((30_000, False), (69_999, False), (70_000, True), (99_999, True), (100_000, False)):
                write_transcript(self.transcript, [user(), assistant(START), assistant(tokens)])
                began = time.monotonic()
                self.stop(last_assistant_message="A reply the transcript never gets.")
                self.assertEqual(time.monotonic() - began >= 0.3, waits, tokens)

    def test_token_cap_applies_to_large_windows(self):
        self.at_tokens(102_000, model="claude-opus-5-5[1m]")
        out = self.stop(last_assistant_message="ok")
        self.assertEqual(out["decision"], "block")
        self.assertIn("≈10% of a 1M window", out["reason"])

    def test_raised_caps_on_a_large_window(self):
        with mock.patch.dict(os.environ, {"CONTEXTRAIL_SOFT_TOKENS": "300000", "CONTEXTRAIL_WINDOW_TOKENS": "1000000"}):
            self.at_tokens(250_000)
            self.assertIsNone(self.stop(last_assistant_message="ok"))
            self.at_tokens(310_000)
            self.assertEqual(self.stop(last_assistant_message="ok")["decision"], "block")

    def test_counts_from_the_session_start(self):
        large = "claude-opus-5-5[1m]"
        self.at_tokens(140_000, model=large, start=50_522)
        self.assertIsNone(self.stop(last_assistant_message="ok"))
        self.assertEqual(store.load_state(self.project())["sessions"]["session-1"]["start_tokens"], 50_522)
        # The start is kept for the session, whatever the transcript's end looks like later.
        self.at_tokens(150_000, model=large, start=START)
        self.assertIsNone(self.stop(last_assistant_message="ok"))
        self.at_tokens(151_000, model=large, start=START)
        self.assertEqual(self.stop(last_assistant_message="ok")["decision"], "block")

    def test_keeps_the_start_only_once_it_is_final(self):
        large = "claude-opus-5-5[1m]"
        listing = {"type": "attachment", "isSidechain": False, "attachment": {"type": "skill_listing"}}
        write_transcript(self.transcript, [user("start"), assistant(30_726, model=large)])
        self.assertIsNone(self.stop(last_assistant_message="ok"))
        self.assertIsNone(store.load_state(self.project())["sessions"]["session-1"]["start_tokens"])
        # Claude Code sent its listings with the second request: the start is the second reply.
        write_transcript(self.transcript, [
            user("start"), assistant(30_726, model=large), listing, assistant(61_159, model=large),
            assistant(160_000, model=large),
        ])
        self.assertIsNone(self.stop(last_assistant_message="ok"))
        self.assertEqual(store.load_state(self.project())["sessions"]["session-1"]["start_tokens"], 61_159)
        self.at_tokens(162_000, model=large, start=30_726)
        self.assertEqual(self.stop(last_assistant_message="ok")["decision"], "block")

    def test_small_windows_keep_the_share_cap(self):
        self.at_tokens(101_000, start=50_522)
        self.assertEqual(self.stop(last_assistant_message="ok")["decision"], "block")

    def test_no_chained_request_when_stop_hook_active(self):
        self.at_tokens(102_000)
        self.assertIsNone(self.stop(last_assistant_message="ok", stop_hook_active=True))

    def test_full_cycle_save_then_refresh(self):
        self.at_tokens(102_000)
        self.assertEqual(self.stop(last_assistant_message="ok")["decision"], "block")

        out = self.stop(last_assistant_message=block_message(), stop_hook_active=True)
        self.assertNotIn("decision", out)
        self.assertIn("handoff saved", out["systemMessage"])
        self.assertIn("/clear", out["systemMessage"])

        project = self.project()
        document = project.handoff.read_text()
        self.assertIn("# Handoff: repo", document)
        self.assertIn("- Branch `main` at", document)
        self.assertIn("## Next steps", document)
        self.assertEqual(project.resume.read_text().strip(), VALID_BODY.split("## Resume prompt\n")[1])
        meta = json.loads(project.meta.read_text())
        self.assertEqual(meta["tokens"], 102_000)
        self.assertEqual(meta["session_id"], "session-1")

        self.assertIn("Keep working: the final handoff and the offer of a new session come at 130,000", out["systemMessage"])

        # Within refresh_tokens of the checkpoint and below the hard point: quiet. At it: the final handoff.
        self.at_tokens(120_000)
        self.assertIsNone(self.stop(last_assistant_message="ok"))
        self.at_tokens(130_000)
        out = self.stop(last_assistant_message="ok")
        self.assertEqual(out["decision"], "block")
        self.assertIn("write a fresh one", out["reason"])
        self.assertIn("This is the final handoff of this session", out["reason"])
        self.assertNotIn(prompts.CHECKPOINT, out["reason"])
        self.assertIn("asking for the final handoff", out["systemMessage"])
        out = self.stop(last_assistant_message=block_message(), stop_hook_active=True)
        self.assertIn("ContextRail: handoff saved", out["systemMessage"])

        # After the final handoff, every refresh_tokens.
        self.at_tokens(160_000)
        self.assertIsNone(self.stop(last_assistant_message="ok"))
        self.at_tokens(171_000)
        out = self.stop(last_assistant_message="ok")
        self.assertIn("This is the final handoff of this session", out["reason"])

    def test_new_session_at_soft_makes_every_handoff_final(self):
        os.environ["CONTEXTRAIL_NEW_SESSION_AT"] = "soft"
        self.at_tokens(102_000)
        out = self.stop(last_assistant_message="ok")
        self.assertNotIn(prompts.CHECKPOINT, out["reason"])
        self.assertNotIn("final handoff", out["reason"])
        self.assertTrue(out["systemMessage"].startswith("ContextRail: context is at 102,000 tokens; asking for a handoff."))
        self.assertIn("Type /clear to continue", self.stop(last_assistant_message=block_message(), stop_hook_active=True)["systemMessage"])
        self.at_tokens(130_000)
        self.assertIsNone(self.stop(last_assistant_message="ok"))
        self.at_tokens(143_000)
        self.assertIn("write a fresh one", self.stop(last_assistant_message="ok")["reason"])

    def test_lightning_offers_a_new_session_at_its_own_point(self):
        os.environ["CONTEXTRAIL_LIGHTNING_TOKENS"] = "60000"
        self.at_tokens(61_000)
        out = self.stop(last_assistant_message="ok")
        self.assertEqual(out["decision"], "block")
        self.assertNotIn(prompts.CHECKPOINT, out["reason"])

    def test_a_handoff_nobody_asked_for_is_final_past_the_hard_point(self):
        self.at_tokens(110_000)
        self.assertIn("checkpoint handoff saved", self.stop(last_assistant_message=block_message())["systemMessage"])
        self.at_tokens(135_000)
        self.assertIn("final handoff", self.stop(last_assistant_message="ok")["reason"])
        self.stop(last_assistant_message=block_message(), stop_hook_active=True)
        self.at_tokens(140_000)
        self.stop(last_assistant_message=block_message())
        entry = store.load_state(self.project())["sessions"]["session-1"]
        self.assertEqual((entry["saved_kind"], entry["saved_tokens"]), ("final", 140_000))
        self.assertIsNone(self.stop(last_assistant_message="ok"))

    def test_the_diagnostic_line_says_what_decided(self):
        os.environ.update(CONTEXTRAIL_WINDOW_TOKENS="1000000", CONTEXTRAIL_SOFT_TOKENS="400000",
                          CONTEXTRAIL_HARD_TOKENS="550000")
        self.at_tokens(401_000)
        self.assertIsNone(self.stop(last_assistant_message="ok"))
        self.at_tokens(402_000)
        lines = self.stop(last_assistant_message="ok")["systemMessage"].splitlines()
        self.assertEqual(lines[1], (
            "Handoff at 402,000 (start 2,000 + soft_tokens 400,000); wrap-up at 552,000 "
            "(start + hard_tokens 550,000). Window 1,000,000 tokens, from the window_tokens setting."
        ))
        self.assertEqual(len(lines), 2)  # no warning: the window size is known

    def test_an_assumed_window_is_explained(self):
        os.environ.update(CONTEXTRAIL_SOFT_TOKENS="400000", CONTEXTRAIL_HARD_TOKENS="550000")
        self.at_tokens(100_000)
        lines = self.stop(last_assistant_message="ok")["systemMessage"].splitlines()
        self.assertIn("Handoff at 100,000 (50% of the window, before start 2,000 + soft_tokens 400,000 = 402,000)", lines[1])
        self.assertTrue(lines[2].startswith("The window size is assumed"), lines)
        self.assertIn("puts the handoff at 100,000 instead of 402,000", lines[2])

    def test_invalid_handoff_gets_one_retry_then_saves_with_warning(self):
        self.at_tokens(102_000)
        bad = block_message("## Goal\nOnly a goal")
        out = self.stop(last_assistant_message=bad, stop_hook_active=True)
        self.assertEqual(out["decision"], "block")
        self.assertIn("'## Next steps'", out["reason"])
        self.assertFalse(self.project().handoff.exists())

        out = self.stop(last_assistant_message=bad, stop_hook_active=True)
        self.assertIn("Saved with problems", out["systemMessage"])
        self.assertIn("> Saved with problems", self.project().handoff.read_text())

    def test_checks_the_handoff_against_the_session(self):
        (self.repo / "extra.py").write_text("x = 1\n")
        write_transcript(self.transcript, [
            user(), tool_use("t1", "Write", {"file_path": str(self.repo / "extra.py")}, tokens=102_000), tool_result("t1"),
        ])
        out = self.stop(last_assistant_message=block_message(), stop_hook_active=True)
        self.assertEqual(out["decision"], "block")
        self.assertIn("- mention or explain these uncommitted files edited in this session: `extra.py`", out["reason"])
        out = self.stop(last_assistant_message=block_message(), stop_hook_active=True)
        self.assertIn("Saved with problems: mention or explain", out["systemMessage"])
        self.assertIn("> Saved with problems: mention or explain", self.project().handoff.read_text())
        self.assertIn("- Edited this session: `extra.py`", self.project().handoff.read_text())

    def test_content_checks_can_be_turned_off(self):
        (self.repo / "extra.py").write_text("x = 1\n")
        write_transcript(self.transcript, [
            user(), tool_use("t1", "Write", {"file_path": str(self.repo / "extra.py")}, tokens=102_000), tool_result("t1"),
        ])
        with mock.patch.dict(os.environ, {"CONTEXTRAIL_CONTENT_CHECKS": "0"}):
            out = self.stop(last_assistant_message=block_message(), stop_hook_active=True)
        self.assertNotIn("decision", out)
        self.assertNotIn("problems", out["systemMessage"])

    def test_manual_handoff_saved_below_the_limit(self):
        self.at_tokens(20_000)
        out = self.stop(last_assistant_message=block_message())
        self.assertIn("handoff saved", out["systemMessage"])

    def test_falls_back_to_transcript_text(self):
        write_transcript(self.transcript, [
            user(), assistant(20_000, content=[{"type": "text", "text": block_message()}]),
        ])
        out = self.stop()
        self.assertIn("handoff saved", out["systemMessage"])

    def test_gives_up_after_max_attempts(self):
        self.at_tokens(102_000)
        for _ in range(3):
            self.assertEqual(self.stop(last_assistant_message="ok")["decision"], "block")
        out = self.stop(last_assistant_message="ok")
        self.assertNotIn("decision", out)
        self.assertIn("/contextrail:handoff", out["systemMessage"])
        self.assertIsNone(self.stop(last_assistant_message="ok"))

    def test_waits_for_background_tasks_below_the_hard_limit(self):
        self.at_tokens(102_000)
        out = self.stop(last_assistant_message="ok", background_tasks=[TASK])
        self.assertNotIn("decision", out)
        self.assertEqual(out["systemMessage"].splitlines()[0], (
            "ContextRail: waiting for 1 background task (`pytest -q`) to finish before asking for a handoff, "
            "at most until 130,000 tokens. Run /contextrail:handoff to write one now."
        ))
        self.assertIn("Handoff at 100,000", out["systemMessage"])
        self.assertIsNone(self.stop(last_assistant_message="ok", background_tasks=[TASK]))
        # The task finishing wakes the session; that stop asks, and waiting used no attempt.
        out = self.stop(last_assistant_message="ok", background_tasks=[])
        self.assertEqual(out["decision"], "block")
        self.assertEqual(store.load_state(self.project())["sessions"]["session-1"]["attempts"], 1)

    def test_asks_anyway_at_the_hard_limit(self):
        self.at_tokens(131_000)
        out = self.stop(last_assistant_message="ok", background_tasks=[TASK])
        self.assertEqual(out["decision"], "block")
        self.assertIn(
            "Background tasks are still running (`pytest -q`): list them under In progress, "
            "with how to check their results.",
            out["reason"],
        )
        self.assertIn("offers a new session after it, once the background tasks finish.", out["systemMessage"])
        self.assertIn("Still running: `pytest -q`; nothing is stopped.", out["systemMessage"])
        self.at_tokens(150_000)
        self.assertNotIn("Background tasks", self.stop(last_assistant_message="ok")["reason"])

    def test_only_tasks_in_flight_hold_the_request(self):
        self.at_tokens(102_000)
        finished = [dict(TASK, status="completed"), dict(TASK, status="failed"), None, "x"]
        crons = [{"id": "c1", "schedule": "*/5 * * * *", "recurring": True, "prompt": "/babysit-prs"}]
        out = self.stop(last_assistant_message="ok", background_tasks=finished, session_crons=crons)
        self.assertEqual(out["decision"], "block")
        self.assertEqual(handlers._in_flight("not a list"), [])
        no_status = {"type": "subagent", "description": "review"}
        self.assertEqual(handlers._in_flight([dict(TASK, status="Pending"), no_status]), [dict(TASK, status="Pending"), no_status])

    def test_asks_again_once_tasks_that_ran_at_the_final_handoff_finish(self):
        self.at_tokens(131_000)
        self.assertEqual(self.stop(last_assistant_message="ok", background_tasks=[TASK])["decision"], "block")
        self.assertIn("handoff saved", self.stop(last_assistant_message=block_message(), background_tasks=[TASK])["systemMessage"])
        self.assertTrue(store.load_state(self.project())["sessions"]["session-1"]["saved_running"])
        self.assertIsNone(self.stop(last_assistant_message="Still running.", background_tasks=[TASK]))
        # The task finishing wakes the session: the agent shows the results, and that stop asks again.
        out = self.stop(last_assistant_message="pytest -q passed: 12 tests.", background_tasks=[])
        self.assertEqual(out["decision"], "block")
        self.assertTrue(out["reason"].startswith("ContextRail: this session is at"))  # the mod's request prefix
        self.assertIn("have finished: write it again with their results", out["reason"])
        self.assertNotIn(prompts.CHECKPOINT, out["reason"])
        self.assertIn("asking for the handoff again with their results", out["systemMessage"])
        self.assertIn("handoff saved", self.stop(last_assistant_message=block_message(), background_tasks=[])["systemMessage"])
        entry = store.load_state(self.project())["sessions"]["session-1"]
        self.assertFalse(entry["saved_running"])
        self.assertEqual(entry["saved_kind"], "final")
        self.assertIsNone(self.stop(last_assistant_message="ok", background_tasks=[]))

    def test_no_second_request_for_a_handoff_nobody_asked_for(self):
        self.at_tokens(20_000)
        self.stop(last_assistant_message=block_message(), background_tasks=[TASK])
        self.assertFalse(store.load_state(self.project())["sessions"]["session-1"]["saved_running"])
        self.assertIsNone(self.stop(last_assistant_message="pytest -q passed.", background_tasks=[]))

    def test_a_question_to_the_user_holds_the_request(self):
        self.at_tokens(102_000)
        out = self.stop(last_assistant_message="The limiter is in.\n\nShould 429s carry a `Retry-After` header?")
        self.assertNotIn("decision", out)
        self.assertEqual(out["systemMessage"], (
            "ContextRail: context is at 102,000 tokens; the handoff request waits until you have answered "
            "the question above."
        ))
        self.assertIsNone(self.stop(last_assistant_message="Which do you want: **(a)** or **(b)?**"))
        # The answer is in; the stop after it asks, and holding used no attempt.
        out = self.stop(last_assistant_message="Added the header; pytest -q passed.")
        self.assertEqual(out["decision"], "block")
        self.assertEqual(store.load_state(self.project())["sessions"]["session-1"]["attempts"], 1)
        self.assertFalse(store.load_state(self.project())["sessions"]["session-1"]["question_notice"])

    def test_a_question_holds_the_request_only_until_the_hard_limit(self):
        self.at_tokens(131_000)
        out = self.stop(last_assistant_message="Should I keep going?")
        self.assertEqual(out["decision"], "block")
        self.assertNotIn("repeat that question", out["reason"])

    def test_waits_for_answer(self):
        for text in ("Which do you want?", "Done.\n\n**Should I go on?**", "Pick one (a or b?)", "Ready? \n\n",
                     "Shall I run `make test`?`", "He asked: “Is it safe?”"):
            self.assertTrue(handlers.waits_for_answer(text), text)
        for text in ("Done.", "", None, "Is it safe? Yes: all 12 tests pass.", "Run it?\n\n```\nmake test\n```",
                     "Which one? I picked (a)."):
            self.assertFalse(handlers.waits_for_answer(text), text)

    def test_watchers_and_housekeeping_hold_nothing(self):
        # What held a Lightning handoff on 23 Oct: an artifact watch carried over by /clear, which never ends.
        watch = {"id": "w1", "type": "monitor", "status": "running",
                 "description": "live updates for artifact https://claude.ai/artifact/HjZTq9WgokwzJSCLS6J4 (watching)"}
        chores = [dict(watch, type=kind, description=kind) for kind in ("dream", "auto-mode scan", "memory import")]
        self.at_tokens(102_000)
        out = self.stop(last_assistant_message="ok", background_tasks=[watch, *chores])
        self.assertEqual(out["decision"], "block")
        self.assertNotIn("live updates", out["reason"] + out["systemMessage"])
        self.assertEqual(handlers._in_flight([watch, TASK, *chores]), [TASK])

    def test_waiting_notice_lists_three_tasks(self):
        tasks = [{"type": "subagent"}] + [dict(TASK, description=f"job {i} " + "x" * 80) for i in range(4)]
        notice = handlers._waiting(tasks, 130_000)
        self.assertIn("waiting for 5 background tasks (`subagent`, `job 0 xxx", notice)
        self.assertIn("x…`, `job 1 ", notice)
        self.assertIn("…` and 2 more) to finish", notice)
        self.assertNotIn("job 2", notice)

    def test_sessions_are_tracked_separately(self):
        self.at_tokens(102_000)
        self.stop(last_assistant_message=block_message())
        self.assertIsNone(self.stop(last_assistant_message="ok"))
        out = handlers.on_stop(self.payload("Stop", session_id="session-2", last_assistant_message="ok"))
        self.assertEqual(out["decision"], "block")

    def test_disabled(self):
        self.at_tokens(150_000)
        with mock.patch.dict(os.environ, {"CONTEXTRAIL_DISABLE": "1"}):
            self.assertIsNone(self.stop(last_assistant_message="ok"))
            self.assertIsNone(self.stop(last_assistant_message=block_message()))

    def test_history_rotation(self):
        with mock.patch.dict(os.environ, {"CONTEXTRAIL_HISTORY_KEEP": "2"}):
            self.at_tokens(10_000)
            for i in range(5):
                meta_path = self.project().meta
                if meta_path.exists():  # give each save a distinct timestamp
                    meta = json.loads(meta_path.read_text())
                    meta["saved_at"] = f"2026-01-0{i}T10:00:00+00:00"
                    meta_path.write_text(json.dumps(meta))
                self.stop(last_assistant_message=block_message())
        archived = sorted(p.name for p in self.project().history.glob("*.md"))
        self.assertEqual(archived, ["2026-01-03T10-00-00.md", "2026-01-04T10-00-00.md"])


class PostToolTest(TempHome):
    def post(self, **extra):
        return handlers.on_post_tool(self.payload("PostToolUse", tool_name="Bash", **extra))

    def test_a_checkpoint_does_not_hold_back_the_wrap_up(self):
        self.at_tokens(125_000)
        handlers.on_stop(self.payload("Stop", last_assistant_message=block_message()))
        self.at_tokens(131_000)
        self.assertIn("past the wrap-up point", self.post()["hookSpecificOutput"]["additionalContext"])

    def test_with_new_session_at_soft_a_fresh_handoff_holds_back_the_wrap_up(self):
        os.environ["CONTEXTRAIL_NEW_SESSION_AT"] = "soft"
        self.at_tokens(125_000)
        handlers.on_stop(self.payload("Stop", last_assistant_message=block_message()))
        self.at_tokens(131_000)
        self.assertIsNone(self.post())

    def test_warns_past_the_hard_limit_with_debounce(self):
        self.at_tokens(120_000)
        self.assertIsNone(self.post())
        self.at_tokens(131_000)
        out = self.post()
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("past the wrap-up point", context)
        # The step in hand runs to the end and the user sees its results; only new work waits.
        self.assertIn("Finish the step you are on, and don't start another", context)
        self.assertIn("let commands that are still running finish, and show the user their results", context)
        self.assertNotIn("Don't start new work", context)
        self.at_tokens(150_000)
        self.assertIsNone(self.post())
        self.at_tokens(162_000)
        self.assertIsNotNone(self.post())

    def test_counts_from_the_session_start(self):
        self.at_tokens(180_000, model="claude-opus-5-5[1m]", start=50_522)
        self.assertIsNone(self.post())
        self.at_tokens(181_000, model="claude-opus-5-5[1m]", start=50_522)
        self.assertIn("past the wrap-up point", self.post()["hookSpecificOutput"]["additionalContext"])

    def test_quiet_after_a_fresh_handoff(self):
        self.at_tokens(131_000)
        handlers.on_stop(self.payload("Stop", last_assistant_message=block_message()))
        self.at_tokens(140_000)
        self.assertIsNone(self.post())

    def test_ignores_subagents(self):
        self.at_tokens(150_000)
        self.assertIsNone(self.post(agent_id="agent-1"))


class PreCompactTest(TempHome):
    def test_saves_facts_and_carries_the_narrative(self):
        self.at_tokens(50_000)
        handlers.on_stop(self.payload("Stop", session_id="earlier", last_assistant_message=block_message()))
        project = store.Project(self.repo)
        first_saved = json.loads(project.meta.read_text())["saved_at"]

        self.at_tokens(160_000)
        out = handlers.on_pre_compact(self.payload("PreCompact", trigger="auto"))
        self.assertIn("before compaction", out["systemMessage"])
        document = project.handoff.read_text()
        self.assertIn("Saved automatically before automatic compaction", document)
        self.assertIn(f"Carried over from the previous handoff (saved {first_saved})", document)
        self.assertIn("## Next steps", document)
        self.assertEqual(json.loads(project.meta.read_text())["narrative_saved_at"], first_saved)
        self.assertEqual(len(list(project.history.glob("*.md"))), 1)

    def test_skips_when_this_session_saved_recently_and_resets_tracking(self):
        self.at_tokens(102_000)
        handlers.on_stop(self.payload("Stop", last_assistant_message=block_message()))
        self.at_tokens(120_000)
        self.assertIsNone(handlers.on_pre_compact(self.payload("PreCompact", trigger="manual")))
        # After compaction, context is small again and the next request comes at the soft limit.
        self.at_tokens(101_000)
        self.assertEqual(handlers.on_stop(self.payload("Stop", last_assistant_message="ok"))["decision"], "block")

    def test_counts_from_the_first_reply_after_compaction(self):
        large = "claude-opus-5-5[1m]"
        self.at_tokens(140_000, model=large, start=50_000)
        self.assertIsNone(handlers.on_stop(self.payload("Stop", last_assistant_message="ok")))
        self.at_tokens(149_000, model=large, start=50_000)
        handlers.on_pre_compact(self.payload("PreCompact", trigger="auto"))
        boundary = {"type": "system", "subtype": "compact_boundary", "isSidechain": False}
        compacted = [user("start"), assistant(50_000, model=large), boundary, user("summary"), assistant(66_000, model=large)]
        write_transcript(self.transcript, compacted + [assistant(160_000, model=large)])
        self.assertIsNone(handlers.on_stop(self.payload("Stop", last_assistant_message="ok")))
        write_transcript(self.transcript, compacted + [assistant(167_000, model=large)])
        self.assertEqual(handlers.on_stop(self.payload("Stop", last_assistant_message="ok"))["decision"], "block")

    def test_stub_when_no_previous_handoff(self):
        self.at_tokens(160_000)
        handlers.on_pre_compact(self.payload("PreCompact", trigger="manual"))
        document = store.Project(self.repo).handoff.read_text()
        self.assertIn("before manual compaction", document)
        self.assertIn("Not recorded", document)


class SessionEndTest(TempHome):
    def edit_session(self, tokens, start=START):
        (self.repo / "app.py").write_text("print('edited')\n")
        write_transcript(self.transcript, [
            user(), assistant(start),
            tool_use("t1", "Edit", {"file_path": str(self.repo / "app.py")}, tokens=tokens), tool_result("t1"),
        ])

    def end(self, reason="clear"):
        return handlers.on_session_end(self.payload("SessionEnd", reason=reason))

    def project(self):
        return store.Project(self.repo)

    def test_checkpoint_when_a_session_with_edits_ends_without_a_handoff(self):
        self.edit_session(45_000)
        self.assertIsNone(self.end("prompt_input_exit"))
        document = self.project().handoff.read_text()
        self.assertIn("> Session ended (`prompt_input_exit`) without a handoff.", document)
        self.assertIn("- Edited this session: `app.py`", document)
        self.assertIn("Not recorded: this handoff was saved automatically when the session ended.", document)
        # It loads into the next session, and a second end of the same session adds nothing.
        context = handlers.on_session_start(self.payload("SessionStart", source="clear"))
        self.assertIn("Session ended (`prompt_input_exit`)", context["hookSpecificOutput"]["additionalContext"])
        self.end()
        self.assertEqual(store.archived(self.project()), [])

    def test_carries_the_last_narrative_over(self):
        self.at_tokens(20_000)
        handlers.on_stop(self.payload("Stop", session_id="earlier", last_assistant_message=block_message()))
        self.edit_session(45_000)
        self.end()
        document = self.project().handoff.read_text()
        self.assertIn(handoff.CARRIED_OVER, document)
        self.assertTrue(handoff.narrative_of(document).endswith(VALID_BODY))
        self.assertEqual(len(store.archived(self.project())), 1)

    def test_no_checkpoint_after_a_recent_handoff_or_below_the_minimum(self):
        self.edit_session(102_000)
        handlers.on_stop(self.payload("Stop", last_assistant_message=block_message()))
        saved = self.project().handoff.read_text()
        self.edit_session(120_000)
        self.end()
        self.assertEqual(self.project().handoff.read_text(), saved)
        self.edit_session(150_000)  # past refresh_tokens since that handoff
        self.end()
        self.assertIn("Session ended (`clear`)", self.project().handoff.read_text())
        other = self.payload("SessionEnd", session_id="short", reason="clear")
        self.edit_session(29_000)
        project_before = self.project().handoff.read_text()
        handlers.on_session_end(other)
        self.assertEqual(self.project().handoff.read_text(), project_before)

    def test_minimum_counts_from_the_session_start(self):
        self.edit_session(79_000, start=50_000)
        self.end()
        self.assertFalse(self.project().handoff.exists())
        self.edit_session(81_000, start=50_000)
        self.end()
        self.assertTrue(self.project().handoff.exists())

    def test_no_checkpoint_without_edits_or_out_of_time(self):
        self.at_tokens(45_000)
        self.end()
        self.assertFalse(self.project().handoff.exists())
        self.edit_session(45_000)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS": "500"}):
            self.end()
        self.assertFalse(self.project().handoff.exists())
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS": "5000"}):
            self.end()
        self.assertTrue(self.project().handoff.exists())


class SessionStartTest(TempHome):
    def project(self):
        return store.Project(self.repo)

    def start(self, source):
        return handlers.on_session_start(self.payload("SessionStart", source=source))

    def save(self):
        self.at_tokens(102_000)
        handlers.on_stop(self.payload("Stop", last_assistant_message=block_message()))

    def test_nothing_to_load(self):
        self.assertIsNone(self.start("startup"))

    def test_loads_on_startup_clear_and_compact(self):
        self.save()
        for source in ("startup", "clear", "compact"):
            out = self.start(source)
            context = out["hookSpecificOutput"]["additionalContext"]
            self.assertTrue(context.startswith("<contextrail-previous-handoff"))
            self.assertIn("## Next steps", context)
            self.assertIn("Keep to the user's rules", context)
            self.assertIn("- None detected.", context)
            self.assertNotIn("<contextrail-handoff>", context)
            self.assertNotIn(prompts.SHORTENED, context)
            self.assertIn(handoff.narrative_of(self.project().handoff.read_text()), context)
            self.assertIn("loaded the handoff saved just now (no repo changes since).", out["systemMessage"])

    def test_a_long_handoff_loads_shortened_under_claude_codes_limit(self):
        facts = "".join(f"- Fact {i}: the limiter keeps a bucket per key, measured in run {i}.\n" for i in range(150))
        body = VALID_BODY.replace("## Known facts\n", "## Known facts\n" + facts)
        self.at_tokens(102_000)
        with mock.patch.dict(os.environ, {"CONTEXTRAIL_MAX_CHARS": "50000"}):
            handlers.on_stop(self.payload("Stop", last_assistant_message=block_message(body)))
        self.assertGreater(handoff.units(self.project().handoff.read_text()), handlers.MAX_CONTEXT)
        out = self.start("clear")
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(handoff.units(context), handlers.MAX_CONTEXT)
        self.assertIn(f"- {prompts.SHORTENED}: ", context)
        self.assertIn(f"Read {self.project().handoff} when you need them.", context)
        self.assertIn("- Fact 0: ", context)
        self.assertRegex(context, r"\n- … \d+ more in the full handoff\n")
        for key in ("goal", "users rules", "in progress", "next steps", "verify first", "resume prompt"):
            self.assertIn(handoff.sections(VALID_BODY)[key], context)
        self.assertTrue(out["systemMessage"].endswith("(no repo changes since), shortened to fit Claude Code's limit for hook context."))

    def test_not_on_resume(self):
        self.save()
        self.assertIsNone(self.start("resume"))

    def test_reports_drift(self):
        self.save()
        (self.repo / "app.py").write_text("print('v2')\n")
        git(self.repo, "commit", "-qam", "follow-up fix")
        out = self.start("clear")
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("1 new commit(s) since the handoff: ", context)
        self.assertIn("- Files changed since the handoff (1): app.py. Tracked files: 1 file changed,", context)
        self.assertIn("- Start by running the Verify first commands.\n", context)
        self.assertIn("1 file(s) changed since", out["systemMessage"])

    def test_flags_items_naming_a_changed_file(self):
        (self.repo / "api").mkdir()
        for name in ("api/limits.py", "api/app.py", "README.md"):
            (self.repo / name).write_text("x = 1\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "api")
        self.save()
        for name in ("api/limits.py", "README.md"):
            (self.repo / name).write_text("x = 2\n")
        git(self.repo, "commit", "-qam", "tweak")
        context = self.start("clear")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("- Files changed since the handoff (2): README.md, api/limits.py.", context)
        self.assertIn(
            '- These items mention files that changed since the handoff; re-check them: "`TokenBucket` in '
            '`api/limits.py`; `pytest -q tests/test_lim…".\n', context
        )
        self.assertNotIn("create_app", context.split("<!-- contextrail:narrative -->")[0])

    def test_drift_block_stays_under_1000_characters(self):
        folder = self.repo / ("deeply/nested/" * 3)
        folder.mkdir(parents=True)
        names = [f"module_with_a_long_name_{i}.py" for i in range(30)]
        for name in names[:5]:
            (folder / name).write_text("draft\n")  # uncommitted at the handoff, committed later
        done = "".join(f"- Finished the long description of the work in {name}.\n" for name in names)
        self.at_tokens(102_000)
        handlers.on_stop(self.payload("Stop", last_assistant_message=block_message(
            VALID_BODY.replace("## In progress\n", f"## In progress\n{done}"))))
        git(self.repo, "checkout", "-qb", "feature/" + "very-long-branch-name-" * 4)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "Commit the drafts as they were")
        for i, name in enumerate(names[5:]):
            (folder / name).write_text(f"n = {i}\n")
            git(self.repo, "add", ".")
            git(self.repo, "commit", "-qm", f"Change number {i} with a commit subject long enough to need cutting")
        context = self.start("clear")["hookSpecificOutput"]["additionalContext"]
        block = context.split("Changes in the repository since the handoff:\n")[1].split("\n\n")[0]
        for start in ("- Branch changed", "- 26 new commit(s)", "- No longer uncommitted", "- Files changed", "- These items"):
            self.assertIn(start, block)
        self.assertIn("- Start by running the Verify first commands.", block)
        self.assertLessEqual(len(block), 1_000, block)

    def test_skips_stale_handoffs(self):
        self.save()
        meta_path = store.Project(self.repo).meta
        meta = json.loads(meta_path.read_text())
        meta["saved_at"] = "2020-01-01T00:00:00+00:00"
        meta_path.write_text(json.dumps(meta))
        self.assertIsNone(self.start("startup"))

    def test_same_project_from_a_subdirectory(self):
        self.save()
        sub = self.repo / "src"
        sub.mkdir()
        out = handlers.on_session_start(self.payload("SessionStart", source="startup", cwd=str(sub)))
        self.assertIsNotNone(out)


class AgeTest(TempHome):
    def test_describe_age(self):
        self.assertEqual(handlers.describe_age(None), "at an unknown time")
        self.assertEqual(handlers.describe_age(30), "just now")
        self.assertEqual(handlers.describe_age(600), "10 minutes ago")
        self.assertEqual(handlers.describe_age(3_600), "1 hour ago")
        self.assertEqual(handlers.describe_age(3 * 86_400), "3 days ago")


class HeartbeatTest(TempHome):
    def beats(self):
        return json.loads((self.home / handlers.HEARTBEAT).read_text())

    def test_written_at_most_once_a_minute(self):
        handlers.heartbeat("Stop")
        first = self.beats()["Stop"]
        (self.home / handlers.HEARTBEAT).write_text(json.dumps({"Stop": first - 30}))
        handlers.heartbeat("Stop")
        self.assertEqual(self.beats(), {"Stop": first - 30})
        (self.home / handlers.HEARTBEAT).write_text(json.dumps({"Stop": first - 61}))
        handlers.heartbeat("Stop")
        handlers.heartbeat("PreCompact")
        self.assertEqual(sorted(self.beats()), ["PreCompact", "Stop"])
        self.assertGreaterEqual(self.beats()["Stop"], first)
