import csv
import io
import json
import os
import re
import time
from pathlib import Path
from unittest import mock

from contextrail import config, handoff, prompts, store
from tests.helpers import VALID_BODY, TempHome, assistant, tool_result, tool_use, user, write_transcript
from tools.eval import __main__ as cli
from tools.eval import cases, report, steps
from tools.eval.steps import EvalError, Harness

A = "aaaaaaaa-0000-4000-8000-000000000001"
B = "bbbbbbbb-0000-4000-8000-000000000002"
FORK = "f0f0f0f0-0000-4000-8000-000000000003"
REPLY = "cccccccc-0000-4000-8000-000000000004"
KEY = "aaaaaaaa-cccccccc"
USAGE_LINE = "50,000 tokens of context (≈25% of a 200k window, assumed)"
# Verdicts the fake judge gives each handoff, by what it says.
VERDICTS = {"variant 6000": ["covered", "missing"], "variant 8000": ["partial", "wrong"]}


def line(entry, uuid=None, at=None, **extra):
    entry = dict(entry, **extra)
    if uuid:
        entry["uuid"] = uuid
    if at:
        entry["timestamp"] = at
    return entry


def result(text, cost=0.5, is_error=False):
    return json.dumps({"type": "result", "is_error": is_error, "result": text, "duration_ms": 2_000,
                       "total_cost_usd": cost, "num_turns": 1, "usage": {"output_tokens": 10}})


def judged(body):
    return next((verdicts for mark, verdicts in VERDICTS.items() if mark in body), ["covered", "covered"])


class FakeClaude:
    """Stands in for `claude`: handoffs that name their cap, two needs, and verdicts
    that follow what each handoff says."""

    def __init__(self):
        self.calls = []
        self.handoff_reply = None
        self.fail = False

    def __call__(self, argv, stdin, cwd, env):
        self.calls.append((argv, stdin, Path(cwd), env))
        if argv == ["claude", "--version"]:
            return "9.9.9 (Claude Code)\n"
        if self.fail:
            return result("API Error: overloaded", is_error=True)
        if "--resume" in argv and stdin == steps.NOTES_PROMPT:
            return result("Notes: plain notes about the limiter.")
        if "--resume" in argv:
            cap = int(re.search(r"Stay under ([\d,]+)", stdin).group(1).replace(",", ""))
            return result(self.handoff_reply or f"<contextrail-handoff>\n{VALID_BODY}\n\nvariant {cap}\n</contextrail-handoff>")
        if "<session-b>" in stdin:
            return result(json.dumps({"needs": [
                {"id": 7, "kind": "used", "need": "The default limit is 600 requests per minute per key.", "evidence": "a"},
                {"id": 9, "kind": "rechecked", "need": "The limiter runs before auth.", "evidence": "b"},
            ]}))
        first = re.search(r"<handoff-1>\n(.*?)\n</handoff-1>", stdin, re.S).group(1)
        second = re.search(r"<handoff-2>\n(.*?)\n</handoff-2>", stdin, re.S).group(1)
        one, two = judged(first), judged(second)
        score = [report.coverage(one), report.coverage(two)]
        better = "tie" if score[0] == score[1] else "1" if score[0] > score[1] else "2"
        rows = [{"id": number, "h1": x, "h2": y} for number, x, y in zip((1, 2), one, two)]
        return result("Verdicts:\n```json\n" + json.dumps({"verdicts": rows, "better": better}) + "\n```")

    def model_calls(self):
        return [call for call in self.calls if "-p" in call[0]]


class EvalTest(TempHome):
    def setUp(self):
        super().setUp()
        folder = self.claude / "projects" / store.claude_slug(self.repo)
        folder.mkdir(parents=True)
        request = cases.FEEDBACK + prompts.request(USAGE_LINE, 12_000, refresh=False)
        self.a_path = folder / f"{A}.jsonl"
        write_transcript(self.a_path, [
            line(user("add rate limiting"), "u-1", "2026-10-20T09:50:00.000Z"),
            line(assistant(50_000, model="claude-opus-5-5"), FORK, "2026-10-20T09:55:00.000Z",
                 cwd=str(self.repo), effort="xhigh"),
            line(dict(user(request), isMeta=True), "r-1", "2026-10-20T09:55:01.000Z"),
            line(assistant(52_000, model="claude-opus-5-5", content=[
                {"type": "text", "text": f"<contextrail-handoff>\n{VALID_BODY}\n</contextrail-handoff>"}]),
                REPLY, "2026-10-20T10:00:00.000Z"),
        ])
        document = (
            "# Handoff: repo\n\n_Saved 2026-10-20T15:30:00+05:30, at 52,000 tokens._\n\n"
            "## Facts (recorded by ContextRail)\n- Branch `main` at `abc1234` initial commit\n\n"
            f"{handoff.NARRATIVE_MARK}\n\n{VALID_BODY}\n"
        )
        loaded = prompts.loaded("2026-10-20T15:30:00+05:30", "just now", [], [], document)
        self.b_path = folder / f"{B}.jsonl"
        write_transcript(self.b_path, [
            {"type": "attachment", "attachment": {
                "type": "hook_additional_context", "hookEvent": "SessionStart", "content": [loaded]}},
            user("continue with the limiter"),
            tool_use("t1", "Bash", {"command": "pytest -q"}),
            tool_result("t1", text="F" * 1_000),
            assistant(60_000, content=[{"type": "text", "text": "The limiter runs before auth; fixing the order."}]),
            dict(user(cases.FEEDBACK + prompts.request("90,000 tokens", 8_000, refresh=False)), isMeta=True),
            assistant(61_000, content=[{"type": "text", "text": "AFTER THE REQUEST"}]),
        ])
        old = time.time() - 2 * 3600
        os.utime(self.b_path, (old, old))
        self.fake = FakeClaude()
        self.harness = Harness(config.home() / "eval", runner=self.fake)
        self.doc = self.tmp / "eval.md"
        self.doc.write_text(f"# Evaluation\n\nBefore.\n\n{report.START}\nold numbers\n{report.END}\n\nAfter.\n", encoding="utf-8")

    def main(self, *args):
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", out):
            takes_project = args[0] in ("cases", "run")
            code = cli.main([*args, "--project", str(self.repo)] if takes_project else list(args), self.harness)
        return code, out.getvalue()

    def case(self):
        found, notes = cases.discover(self.repo)
        self.assertEqual(len(found), 1, notes)
        return found[0]

    def test_finds_the_case(self):
        case = self.case()
        self.assertEqual(case.key, KEY)
        self.assertEqual((case.a, case.b, case.fork_uuid, case.asked_by), (A, B, FORK, "hook"))
        self.assertEqual((case.model, case.effort, case.saved_cap, case.usage_line), ("claude-opus-5-5", "xhigh", 12_000, USAGE_LINE))
        self.assertEqual((case.tokens, case.messages, case.cwd), (50_000, 1, str(self.repo)))
        self.assertEqual(case.saved_body, VALID_BODY)
        self.assertTrue(cases.facts_part(case.loaded).startswith("# Handoff: repo"))

    def test_finds_a_request_sent_as_stop_feedback(self):
        entries = [json.loads(raw) for raw in self.a_path.read_text(encoding="utf-8").splitlines()]
        request = prompts.request(USAGE_LINE, 12_000, refresh=False)
        entries[2] = {"type": "attachment", "uuid": "r-1", "timestamp": "2026-10-20T09:55:01.000Z", "attachment": {
            "type": "hook_additional_context", "hookEvent": "Stop", "hookName": "Stop", "content": [request]}}
        write_transcript(self.a_path, entries)
        case = self.case()
        self.assertEqual((case.asked_by, case.saved_cap, case.usage_line), ("hook", 12_000, USAGE_LINE))

    def test_run_makes_each_call_once_then_reports(self):
        code, out = self.main("run", "--caps", "6000,8000")
        self.assertEqual(code, 0, out)
        folder = self.harness.root / "cases" / KEY
        names = sorted(path.name for path in folder.iterdir())
        self.assertEqual(names, sorted([
            "case.json", "gen-6000.json", "gen-8000.json", "needs.json",
            *[f"judge-{x}-{y}.json" for x in ("saved", "6000", "8000") for y in ("saved", "6000", "8000") if x != y],
        ]))
        calls = self.fake.model_calls()
        self.assertEqual(len(calls), 9)

        argv, stdin, cwd, env = calls[0]
        self.assertEqual(argv[argv.index("--resume") + 1], A)
        self.assertEqual(argv[argv.index("--resume-session-at") + 1], FORK)
        self.assertIn("--fork-session", argv)
        self.assertIn("--no-session-persistence", argv)
        self.assertEqual(argv[argv.index("--model") + 1:argv.index("--model") + 4], ["claude-opus-5-5", "--effort", "xhigh"])
        self.assertEqual(cwd, self.repo)
        self.assertTrue(stdin.startswith(f"Stop hook feedback:\nContextRail: this session is at {USAGE_LINE}."))
        self.assertIn("Stay under 6,000 characters", stdin)
        self.assertEqual(env["CONTEXTRAIL_DISABLE"], "1")
        self.assertEqual(env["CONTEXTRAIL_HOME"], str(self.harness.sandbox))
        self.assertEqual(env["CONTEXTRAIL_COPY_RESUME_PROMPT"], "0")
        self.assertEqual(env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")
        self.assertIn("Stay under 8,000 characters", calls[1][1])
        for argv, _stdin, cwd, _ in calls[2:]:
            self.assertEqual(argv[argv.index("--tools") + 1], "")
            self.assertEqual(argv[argv.index("--model") + 1], "opus")
            self.assertEqual(cwd, self.harness.work)
            self.assertNotIn("--resume", argv)
        self.assertIn("<loaded-handoff>", calls[2][1])
        self.assertNotIn("used", calls[3][1].split("<needs>")[1])  # the judge doesn't see the kinds

        needs = json.loads((folder / "needs.json").read_text(encoding="utf-8"))["needs"]
        self.assertEqual([(item["id"], item["kind"]) for item in needs], [(1, "used"), (2, "rechecked")])
        self.assertTrue(json.loads((folder / "gen-6000.json").read_text(encoding="utf-8"))["reply"].startswith("<contextrail-handoff>"))
        judgment = json.loads((folder / "judge-saved-6000.json").read_text(encoding="utf-8"))
        self.assertEqual(judgment["verdicts"][1], {"id": 2, "saved": "covered", "6000": "missing"})
        self.assertEqual(judgment["better"], "saved")
        run = json.loads((self.harness.root / "run.json").read_text(encoding="utf-8"))
        self.assertEqual((run["calls"], run["error"], run["claude"], run["cases"]), (9, None, "9.9.9 (Claude Code)", [KEY]))

        # A second run finds everything done.
        self.assertEqual(self.main("run")[0], 0)
        self.assertEqual(len(self.fake.model_calls()), 9)

        code, out = self.main("report")
        self.assertEqual(code, 0, out)
        self.assertIn("1 cases, 2 needs. Handoffs written by claude-opus-5-5 (xhigh effort); needs and verdicts by opus.", out)
        self.assertIn("Cost as Claude Code reported it: $4.50.", out)
        self.assertIn("| Saved (cap 12,000) | 100% | 100% | 0.0 |", out)
        self.assertIn("| Cap 6,000 | 50% | 0% | 0.0 |", out)
        self.assertIn("| Cap 8,000 | 25% | 0% | 1.0 |", out)
        self.assertIn("| 0 of 1 | 2 s |", out)
        self.assertIn("| Saved (cap 12,000) vs Cap 6,000 | 2 – 0 | 0 |", out)
        self.assertIn("| Cap 6,000 vs Cap 8,000 | 2 – 0 | 0 |", out)
        self.assertIn("| 1 | 50,000 | 1 | 2 | 100% | 50% | 25% |", out)
        self.assertNotIn("rate limiting", out.lower())  # numbers only

    def test_baselines_are_judged_against_the_saved_handoff_with_their_own_material(self):
        code, out = self.main("run", "--caps", "6000", "--baselines", "notes,facts")
        self.assertEqual(code, 0, out)
        folder = self.harness.root / "cases" / KEY
        self.assertTrue((folder / "gen-notes.json").exists())
        names = {path.name for path in folder.glob("judge-*.json")}
        self.assertEqual(names, {f"judge-{x}-{y}.json" for x, y in [
            ("saved", "6000"), ("6000", "saved"), ("saved", "notes"), ("notes", "saved"), ("saved", "facts"), ("facts", "saved")]})
        calls = self.fake.model_calls()
        self.assertEqual(len(calls), 9)  # 1 regeneration, the notes, the needs, 6 judgings
        notes_call = next(call for call in calls if call[1] == steps.NOTES_PROMPT)
        self.assertIn("--fork-session", notes_call[0])
        judgings = [call[1] for call in calls if "<handoff-1>" in call[1]]
        with_baseline = [stdin for stdin in judgings if "plain notes" in stdin or "<facts>" not in stdin]
        self.assertEqual(len(with_baseline), 4)
        for stdin in with_baseline:
            self.assertNotIn("<facts>", stdin)  # no shared facts: each document carries what it gets
            self.assertIn("everything the next session would get", stdin)
        saved_side = next(stdin for stdin in with_baseline if "plain notes" in stdin)
        self.assertIn("# Handoff: repo", saved_side)  # the saved handoff comes with its recorded facts
        self.assertEqual(json.loads((folder / "judge-saved-notes.json").read_text(encoding="utf-8"))["facts"], "with each")
        self.assertEqual(json.loads((folder / "judge-saved-6000.json").read_text(encoding="utf-8"))["facts"], "shared")

        code, out = self.main("report")
        self.assertEqual(code, 0, out)
        self.assertIn("| Plain notes | ", out)
        self.assertIn("| Facts only | ", out)
        self.assertIn("| – |", out)  # baselines aren't held to the template
        self.assertIn("| Saved (cap 12,000) vs Plain notes |", out)
        self.assertNotIn("| Cap 6,000 vs Plain notes |", out)

    def test_split_is_fixed_by_the_key(self):
        keys = [f"{n:08x}-{n * 7:08x}" for n in range(400)]
        splits = [cases.split(key) for key in keys]
        self.assertEqual(splits, [cases.split(key) for key in keys])
        self.assertTrue(0.2 < splits.count("tune") / len(keys) < 0.4)
        found = cases.split(KEY)
        self.assertEqual(self.main("run", "--split", found, "--dry-run")[1].splitlines()[0][:9], "1 case(s)")
        other = "tune" if found == "test" else "test"
        self.assertIn("0 case(s)", self.main("run", "--split", other, "--dry-run")[1])

    def test_blinded_export_and_agreement(self):
        self.main("run", "--caps", "6000", "--baselines", "notes")
        out = self.tmp / "review"
        code, printed = self.main("export", "--out", str(out), "--seed", "7")
        self.assertEqual(code, 0, printed)
        self.assertEqual(sorted(path.name for path in out.iterdir()), ["README.txt", "case-01.md", "sheet.csv"])
        key = json.loads((self.tmp / "review.key.json").read_text(encoding="utf-8"))
        labels = key["cases"]["01"]["labels"]
        self.assertEqual(sorted(labels.values()), ["6000", "notes", "saved"])
        packet = (out / "case-01.md").read_text(encoding="utf-8")
        for variant in ("saved", "6000", "notes"):
            self.assertNotIn(f"Document {variant}", packet)
        self.assertNotIn(KEY, packet + (out / "sheet.csv").read_text(encoding="utf-8"))

        with open(out / "sheet.csv", newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], ["case", "need", "text", "A", "B", "C"])
        letter = {variant: name for name, variant in labels.items()}
        filled = [rows[0]]
        for row in rows[1:]:
            if row[1] != "best":
                row[3:6] = ["covered", "covered", "covered"]
                row[3 + "ABC".index(letter["6000"])] = "missing"
            filled.append(row)
        for name in ("ana", "ben"):
            with open(self.tmp / f"{name}.csv", "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerows(filled)
        code, printed = self.main("agreement", str(self.tmp / "ana.csv"), str(self.tmp / "ben.csv"), "--key", str(self.tmp / "review.key.json"))
        self.assertEqual(code, 0, printed)
        self.assertIn("ana: 6000 0%, notes 100%, saved 100%", printed)
        self.assertIn("ana and ben: 6 items, 100% the same verdict, kappa 1.00", printed)
        self.assertIn("ana and the model judge: 6 items", printed)

        bad = [row[:] for row in filled]
        bad[1][3] = "great"
        with open(self.tmp / "bad.csv", "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(bad)
        code, printed = self.main("agreement", str(self.tmp / "bad.csv"), "--key", str(self.tmp / "review.key.json"))
        self.assertEqual(code, 1)
        self.assertIn("'great' is not one of covered, partial, missing, wrong", printed)

    def test_kappa(self):
        from tools.eval import review
        self.assertEqual(review.kappa([("covered", "covered"), ("missing", "missing")]), 1.0)
        self.assertAlmostEqual(review.kappa([("covered", "missing"), ("missing", "covered")]), -1.0)
        self.assertIsNone(review.kappa([]))

    def test_report_writes_only_between_the_markers(self):
        self.main("run")
        run = json.loads((self.harness.root / "run.json").read_text(encoding="utf-8"))
        text = report.render(report.load(self.harness, run), run)
        report.write(self.doc, text)
        written = self.doc.read_text(encoding="utf-8")
        self.assertTrue(written.startswith("# Evaluation\n\nBefore.\n\n<!-- eval:results -->\nRun of "))
        self.assertTrue(written.endswith("|\n<!-- /eval:results -->\n\nAfter.\n"))
        self.assertNotIn("old numbers", written)
        self.doc.write_text("no markers here", encoding="utf-8")
        with self.assertRaises(EvalError):
            report.write(self.doc, text)

    def test_stops_at_the_first_error(self):
        self.fake.fail = True
        code, out = self.main("run")
        self.assertEqual(code, 1)
        self.assertIn("Stopped: claude failed: API Error: overloaded", out)
        self.assertEqual(len(self.fake.model_calls()), 1)
        self.assertFalse((self.harness.root / "cases" / KEY / "gen-6000.json").exists())
        run = json.loads((self.harness.root / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(run["calls"], 0)
        self.assertIn("overloaded", run["error"])

    def test_a_reply_without_a_handoff_is_kept(self):
        self.fake.handoff_reply = "Sorry, I can't write that."
        with self.assertRaises(EvalError) as caught:
            steps.generate(self.harness, self.case(), 6_000)
        self.assertIn("no handoff block in the reply at cap 6000", str(caught.exception))
        raw = self.harness.root / "cases" / KEY / "gen-6000.raw.txt"
        self.assertEqual(raw.read_text(encoding="utf-8"), "Sorry, I can't write that.")

    def test_missing_verdicts_stop_the_judging(self):
        case = self.case()
        steps.needs(self.harness, case)
        steps.generate(self.harness, case, 6_000)
        reply = result('{"verdicts": [{"id": 1, "h1": "covered", "h2": "great"}], "better": "1"}')
        self.harness.runner = lambda *args: reply
        with self.assertRaises(EvalError) as caught:
            steps.judge(self.harness, case, "saved", "6000")
        self.assertIn("no valid verdicts for needs [1, 2]", str(caught.exception))
        self.assertFalse(steps.judge_path(self.harness, case, "saved", "6000").exists())

    def test_dry_run_makes_no_calls_and_writes_nothing(self):
        code, out = self.main("run", "--dry-run", "--cases", KEY)
        self.assertEqual(code, 0)
        self.assertIn("1 case(s), caps 6,000, 8,000, judge opus: 9 call(s) to make.", out)
        self.assertIn(f"  {KEY}: regenerate the handoff at cap 6,000", out)
        self.assertIn(f"  {KEY}: judge 8000 (shown first) against 6000", out)
        self.assertEqual(self.fake.calls, [])
        self.assertFalse(self.harness.root.exists())

    def test_case_selection(self):
        self.assertIn("unknown case keys nope", self.main("run", "--cases", "nope")[1])
        self.assertIn("more than --limit 1", self.main("run", "--cases", f"{KEY},{KEY}", "--limit", "1")[1])
        self.assertIn("0 call(s)", self.main("run", "--dry-run", "--limit", "0")[1])

    def test_condense_reads_b_up_to_its_own_request(self):
        text = steps.condense(self.b_path)
        self.assertEqual(text.splitlines(), [
            "USER: continue with the limiter",
            'TOOL Bash: {"command": "pytest -q"}',
            "RESULT: " + "F" * 400 + " [… 600 more]",
            "AGENT: The limiter runs before auth; fixing the order.",
        ])
        cut = steps.condense(self.b_path, limit=40)
        self.assertTrue(cut.startswith("USER: continue with the limiter\nTOOL Bas"))
        self.assertTrue(cut.endswith("characters, is cut]"))

    def test_parse_json(self):
        self.assertEqual(steps.parse_json('{"a": 1}'), {"a": 1})
        self.assertEqual(steps.parse_json('Here:\n```json\n{"a": 2}\n```\nDone.'), {"a": 2})
        self.assertEqual(steps.parse_json('The answer is {"a": 3} as asked.'), {"a": 3})
        with self.assertRaises(EvalError):
            steps.parse_json("no json at all")

    def test_coverage(self):
        self.assertEqual(report.coverage(["covered", "partial", "missing", "wrong"]), 0.375)
        self.assertEqual(report.coverage(["covered", "covered"]), 1.0)
        self.assertEqual(report.coverage([]), 0.0)

    def test_claude_output_shapes(self):
        verbose = json.dumps([{"type": "system"}, json.loads(result("hello"))])
        self.harness.runner = lambda *args: verbose
        self.assertEqual(self.harness.claude([], "", self.harness.work)["result"], "hello")
        self.harness.runner = lambda *args: "not json"
        with self.assertRaises(EvalError):
            self.harness.claude([], "", self.harness.work)
