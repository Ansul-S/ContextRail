import json
import os
from unittest import mock

from contextrail import handlers, store
from tests.helpers import VALID_BODY, TempHome, assistant, git, user, write_transcript


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
        self.assertIn("asking for a handoff", out["systemMessage"])

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

        # Within refresh_tokens of the save: quiet. Past it: ask again.
        self.at_tokens(130_000)
        self.assertIsNone(self.stop(last_assistant_message="ok"))
        self.at_tokens(143_000)
        out = self.stop(last_assistant_message="ok")
        self.assertEqual(out["decision"], "block")
        self.assertIn("write a fresh one", out["reason"])

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
        self.assertEqual(out["systemMessage"], (
            "ContextRail: waiting for 1 background task (`pytest -q`) to finish before asking for a handoff, "
            "at most until 130,000 tokens. Run /contextrail:handoff to write one now."
        ))
        self.assertIsNone(self.stop(last_assistant_message="ok", background_tasks=[TASK]))
        # The task finishing wakes the session; that stop asks, and waiting used no attempt.
        out = self.stop(last_assistant_message="ok", background_tasks=[])
        self.assertEqual(out["decision"], "block")
        self.assertEqual(store.load_state(self.project())["sessions"]["session-1"]["attempts"], 1)

    def test_asks_anyway_at_the_hard_limit(self):
        self.at_tokens(131_000)
        self.assertEqual(self.stop(last_assistant_message="ok", background_tasks=[TASK])["decision"], "block")

    def test_only_tasks_in_flight_hold_the_request(self):
        self.at_tokens(102_000)
        finished = [dict(TASK, status="completed"), dict(TASK, status="failed"), None, "x"]
        crons = [{"id": "c1", "schedule": "*/5 * * * *", "recurring": True, "prompt": "/babysit-prs"}]
        out = self.stop(last_assistant_message="ok", background_tasks=finished, session_crons=crons)
        self.assertEqual(out["decision"], "block")
        self.assertEqual(handlers._in_flight("not a list"), [])
        no_status = {"type": "subagent", "description": "review"}
        self.assertEqual(handlers._in_flight([dict(TASK, status="Pending"), no_status]), [dict(TASK, status="Pending"), no_status])

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

    def test_warns_past_the_hard_limit_with_debounce(self):
        self.at_tokens(120_000)
        self.assertIsNone(self.post())
        self.at_tokens(131_000)
        out = self.post()
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn("past the wrap-up point", out["hookSpecificOutput"]["additionalContext"])
        self.at_tokens(150_000)
        self.assertIsNone(self.post())
        self.at_tokens(162_000)
        self.assertIsNotNone(self.post())

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

    def test_stub_when_no_previous_handoff(self):
        self.at_tokens(160_000)
        handlers.on_pre_compact(self.payload("PreCompact", trigger="manual"))
        document = store.Project(self.repo).handoff.read_text()
        self.assertIn("before manual compaction", document)
        self.assertIn("Not recorded", document)


class SessionStartTest(TempHome):
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
            self.assertIn("loaded the handoff saved just now (no repo changes since)", out["systemMessage"])

    def test_not_on_resume(self):
        self.save()
        self.assertIsNone(self.start("resume"))

    def test_reports_drift(self):
        self.save()
        (self.repo / "app.py").write_text("print('v2')\n")
        git(self.repo, "commit", "-qam", "follow-up fix")
        out = self.start("clear")
        self.assertIn("1 new commit(s) since the handoff: ", out["hookSpecificOutput"]["additionalContext"])
        self.assertIn("1 repo change(s) since", out["systemMessage"])

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
