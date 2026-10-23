import unittest

from contextrail import handoff
from contextrail.facts import Activity, GitState
from tests.helpers import VALID_BODY


class ExtractTest(unittest.TestCase):
    def test_last_block_wins(self):
        text = "<contextrail-handoff>old</contextrail-handoff> then <contextrail-handoff>\nnew\n</contextrail-handoff>"
        self.assertEqual(handoff.extract(text), "new")

    def test_a_tag_quoted_inside_the_handoff_does_not_end_it(self):
        body = (
            "## Goal\nFix the parser.\n\n## Known facts\n- A stray `</contextrail-handoff>` cut handoffs short; "
            "so did `<contextrail-handoff>` in prose.\n\n## Resume prompt\nContinue the parser fix."
        )
        text = f"Here it is.\n<contextrail-handoff>\n{body}\n</contextrail-handoff>\nWaiting on you."
        self.assertEqual(handoff.extract(text), body)
        self.assertEqual(handoff.resume_prompt(handoff.extract(text)), "Continue the parser fix.")
        self.assertEqual(handoff.extract(text.replace("\n", "\r\n")), body.replace("\n", "\r\n"))

    def test_tags_inside_a_line_still_count_when_none_stand_alone(self):
        self.assertEqual(handoff.extract("<contextrail-handoff>## Goal\nx</contextrail-handoff>"), "## Goal\nx")

    def test_no_block(self):
        self.assertIsNone(handoff.extract("nothing here"))
        self.assertIsNone(handoff.extract(None))
        self.assertIsNone(handoff.extract("<contextrail-handoff> unterminated"))


class SectionsTest(unittest.TestCase):
    def test_parses_and_normalizes_headings(self):
        body = "## Goal\nShip it\n## Next:\n1. a\n## WIP ##\nhalf\n### Sub\nkept inside\n## Resume\nGo"
        found = handoff.sections(body)
        self.assertEqual(found["goal"], "Ship it")
        self.assertEqual(found["next steps"], "1. a")
        self.assertEqual(found["in progress"], "half\n### Sub\nkept inside")
        self.assertEqual(handoff.resume_prompt(body), "Go")

    def test_apostrophes_and_aliases(self):
        body = "## User’s rules\nask first\n## Constraints\nno deps\n## Facts\n3 routes\n## Run\n`make test`"
        self.assertEqual(handoff._key("User's rules"), "users rules")
        found = handoff.sections(body)
        self.assertEqual(found["users rules"], "no deps")  # the later heading wins
        self.assertEqual(found["known facts"], "3 routes")
        self.assertEqual(found["how to run"], "`make test`")

    def test_valid_body_has_no_problems(self):
        self.assertEqual(handoff.problems(VALID_BODY, 12_000), [])
        for heading in ("## Users rules", "## User rules", "## Constraints"):
            self.assertEqual(handoff.problems(VALID_BODY.replace("## User's rules", heading), 12_000), [])

    def test_user_rules_are_required(self):
        without = VALID_BODY.replace("## User's rules", "## Notes")
        self.assertEqual(handoff.problems(without, 12_000), ["missing or empty: '## User's rules'"])
        empty = VALID_BODY.split("## User's rules")[0] + "## User's rules\n\n## Done" + VALID_BODY.split("## Done")[1]
        self.assertEqual(handoff.problems(empty, 12_000), ["missing or empty: '## User's rules'"])

    def test_reports_missing_long_and_resume(self):
        body = "## Goal\nx\n## Done\n\n## Resume prompt\n" + "r" * 900
        issues = handoff.problems(body, 500)
        self.assertEqual(len(issues), 3)
        self.assertIn("'## Done'", issues[0])
        self.assertIn("'## In progress'", issues[0])
        self.assertIn("'## Next steps'", issues[0])
        self.assertIn("too long", issues[1])
        self.assertIn("Resume prompt", issues[2])


class MentionsTest(unittest.TestCase):
    def test_done_and_in_progress_items_naming_a_file(self):
        body = (
            "## Done\n- Limiter in `api/limits.py`, tests pass.\n- Docs in README.md.\n- Fixed app.py.\n"
            "## In progress\n1. Wiring `app.py:create_app()`\n2. myapp.py and app.pyc untouched\n"
            "## Next steps\n1. Edit api/app.py\n"
        )
        self.assertEqual(
            handoff.mentions(body, ["src/api/limits.py", "api/app.py"]),
            ["Limiter in `api/limits.py`, tests pass.", "Fixed app.py.", "Wiring `app.py:create_app()`"],
        )
        self.assertEqual(handoff.mentions(body, []), [])
        self.assertEqual(handoff.mentions(body, ["setup.cfg"]), [])

    def test_long_items_keep_the_file_name(self):
        late = "## Done\n- " + "A long account of what was done and checked, " * 2 + "ending in `api/limits.py` and more text"
        (excerpt,) = handoff.mentions(late, ["api/limits.py"])
        self.assertEqual(len(excerpt), handoff.EXCERPT_CHARS)
        self.assertTrue(excerpt.startswith("…") and excerpt.endswith("api/limits.py…"))


class ContentChecksTest(unittest.TestCase):
    # The facts of the example handoff: what was edited, what is uncommitted, what ran.
    EDITED = ["api/limits.py", "tests/test_limits.py", "api/app.py"]
    DIRTY = ["api/app.py", "tests/test_app.py"]
    CHECKS = [("pytest -q tests/test_limits.py", "passed"), ("pytest -q", "failed")]

    def issues(self, body=VALID_BODY, edited=EDITED, dirty=DIRTY, checks=CHECKS):
        return handoff.content_problems(body, Activity(edited=list(edited), checks=list(checks)), list(dirty))

    def test_the_example_matches_its_facts(self):
        self.assertEqual(self.issues(), [])
        self.assertEqual(handoff.problems(VALID_BODY, 12_000, Activity(edited=self.EDITED), self.DIRTY), [])

    def test_uncommitted_edits_the_narrative_never_names(self):
        edited = self.EDITED + ["lib/old.py", "api/settings.py", "docs/limits.md", "web/a.ts", "web/b.ts", "new/c.py",
                                "new/d.py", "x.py"]
        dirty = self.DIRTY + ["docs/limits.md", "web/a.ts", "web/b.ts", "new/", "x.py"]
        (issue,) = self.issues(edited=edited, dirty=dirty)
        # settings.py is named and lib/old.py committed; files under new/ count as uncommitted.
        self.assertEqual(issue, (
            "mention or explain these uncommitted files edited in this session: "
            "`docs/limits.md`, `web/a.ts`, `web/b.ts`, `new/c.py`, `new/d.py` and 1 more"
        ))
        self.assertEqual(self.issues(body=VALID_BODY + "\n- Also touched `docs/limits.md`, `x.py` and web/.",
                                     edited=edited, dirty=dirty[:-2] + ["x.py"]), [])

    def test_names_without_the_extension_count_and_test_files_are_left_out(self):
        edited = ["contextrail/handoff.py", "CHANGELOG.md", "contextrail/config.py", "tests/test_x.py", "web/a.spec.ts",
                  "pkg/x_test.go", "contextrail/store.py"]
        body = VALID_BODY + "\n- `handoff.render` and config.load changed; CHANGELOG updated."
        (issue,) = self.issues(body=body, edited=edited, dirty=edited)
        self.assertTrue(issue.endswith(": `contextrail/store.py`"), issue)

    def test_done_without_evidence(self):
        vague = VALID_BODY.replace("`pytest -q tests/test_limits.py` passed (12 tests)", "works now")
        self.assertEqual(len(self.issues(body=vague)), 1)
        self.assertIn("'## Done' gives no evidence", self.issues(body=vague)[0])
        # One piece of evidence covers the section: handoffs often list the work, then the test run.
        listed = vague.replace("works now.", "works now.\n- `slugify` added.\n- `pytest -q` ran: 14 passed.")
        self.assertEqual(self.issues(body=listed, checks=[]), [])
        nothing = VALID_BODY.replace("- `TokenBucket` in `api/limits.py`;", "- None yet;", 1)
        self.assertEqual(self.issues(body=nothing.replace("passed (12 tests)", "")), [])

    def test_vague_first_step(self):
        first = VALID_BODY.split("## Next steps\n")[1].splitlines()[0]
        for vague in ("1. Continue with the work.", "1. Finish the feature and test it"):
            self.assertEqual(self.issues(body=VALID_BODY.replace(first, vague)),
                             ["make step 1 of '## Next steps' concrete: name the file, function or command"])
        for concrete in ("1. Wait for the user's instructions.", '1. "add the 429 test": write it', "1. Fix api/app.py",
                         "1. Rename create_app()", "1. Close issue #151", "1. Start P0-3", "1. Read limits.py"):
            self.assertEqual(self.issues(body=VALID_BODY.replace(first, concrete)), [], concrete)

    def test_done_contradicts_a_failed_check(self):
        quoted = VALID_BODY.replace("`pytest -q tests/test_limits.py` passed", "`pytest -q` passed")
        generic = VALID_BODY.replace("`pytest -q tests/test_limits.py` passed (12 tests).", "all the tests pass.")
        expected = ["the facts show `pytest -q` failed, but '## Done' says it passed: correct that, or run it again"]
        self.assertEqual(self.issues(body=quoted), expected)
        self.assertEqual(self.issues(body=generic), expected)
        # A narrower run that passed, a failed lint run, or a failure a later test run passed after: no contradiction.
        self.assertEqual(self.issues(), [])
        self.assertEqual(self.issues(body=generic, checks=[("ruff check .", "failed")]), [])
        fixed = [("pytest -q", "failed"), ("pytest -q tests/test_limits.py", "passed")]
        self.assertEqual(self.issues(body=generic, checks=fixed), [])
        self.assertEqual(self.issues(body=quoted, checks=fixed), expected)


class RenderTest(unittest.TestCase):
    def test_facts_on_top_narrative_below(self):
        git = GitState(branch="main", head="abcdef1234567", subject="Add limiter", dirty=["api/app.py"], diff_hash="h")
        checks = [("pytest -q", "passed"), ("npm run build", "failed"), ("pnpm lint", "ran")]
        activity = Activity(edited=["api/app.py"], outside=2, checks=checks)
        doc = handoff.render("api", "2026-10-22T18:00:00+05:30", "100 tokens", git, activity, VALID_BODY, ["note"])
        self.assertTrue(doc.startswith("# Handoff: api\n"))
        self.assertIn("- Branch `main` at `abcdef123` Add limiter", doc)
        self.assertIn("- Uncommitted (1): `api/app.py`", doc)
        self.assertIn("- Edited this session: `api/app.py` and 2 files outside the project\n", doc)
        self.assertIn("- Recent checks: `pytest -q` passed; `npm run build` failed; `pnpm lint` ran\n", doc)
        self.assertIn("> note", doc)
        self.assertEqual(handoff.narrative_of(doc), VALID_BODY)

    def test_not_a_repo(self):
        doc = handoff.render("x", "t", "u", None, Activity(), VALID_BODY, [])
        self.assertIn("- Not a git repository.", doc)
        self.assertNotIn("Edited this session", doc)

    def test_long_and_outside_edit_lists(self):
        many = Activity(edited=[f"f{i}.py" for i in range(17)], outside=1)
        doc = handoff.render("x", "t", "u", None, many, VALID_BODY, [])
        self.assertIn("`f2.py`, ", doc)
        self.assertIn("`f16.py`, 2 more and 1 file outside the project\n", doc)
        self.assertNotIn("`f1.py`", doc)
        only_outside = handoff.render("x", "t", "u", None, Activity(outside=3), VALID_BODY, [])
        self.assertIn("- Edited this session: 3 files outside the project\n", only_outside)


class ShortenTest(unittest.TestCase):
    def document(self, body):
        return handoff.render("p", "t", "u", None, Activity(), body, [])

    def body_with(self, section, items):
        heading = f"## {section}\n"
        start = VALID_BODY.index(heading) + len(heading)
        end = VALID_BODY.index("\n\n## ", start)
        return VALID_BODY[:end] + "".join(f"\n{item}" for item in items) + VALID_BODY[end:]

    def test_units_count_utf16(self):
        self.assertEqual(handoff.units("a≈😀"), 4)

    def test_fits_already(self):
        document = self.document(VALID_BODY)
        self.assertEqual(handoff.shorten(document, handoff.units(document)), document)

    def test_cuts_the_end_of_the_least_needed_section_first(self):
        facts = [f"- Fact {i}: " + "detail " * 12 + "\n  - a nested note" for i in range(40)]
        document = self.document(self.body_with("Known facts", facts))
        limit = handoff.units(document) - 1_500
        short = handoff.shorten(document, limit)
        self.assertLessEqual(handoff.units(short), limit)
        found = handoff.sections(handoff.narrative_of(short))
        kept = found["known facts"].splitlines()
        self.assertTrue(kept[0].startswith("- 3 routes need the limiter"))
        self.assertRegex(kept[-1], r"^- … \d+ more in the full handoff$")
        self.assertEqual(short.count("- Fact "), short.count("  - a nested note"))  # an item goes with its nested lines
        for key, text in handoff.sections(VALID_BODY).items():
            if key != "known facts":
                self.assertEqual(found[key], text)

    def test_first_items_go_last_and_in_order(self):
        body = VALID_BODY.replace("## Known facts\n", "## Known facts\n- " + "a long fact " * 250 + "\n")
        document = self.document(body)
        short = handoff.shorten(document, handoff.units(document) - 2_000)
        found = handoff.sections(handoff.narrative_of(short))
        self.assertEqual(found["known facts"], "- … 4 more in the full handoff")
        self.assertTrue(found["how to run"].endswith("\n- … 1 more in the full handoff"))
        for key in ("dead ends", "decisions", "done", "open questions", "in progress", "next steps"):
            self.assertEqual(found[key], handoff.sections(VALID_BODY)[key])

    def test_cuts_the_text_when_trimming_is_not_enough(self):
        body = VALID_BODY.replace("## In progress\n", "## In progress\n- " + "😀" * 3_000 + "\n")
        short = handoff.shorten(self.document(body), 3_000)
        self.assertLessEqual(handoff.units(short), 3_000)
        self.assertTrue(short.startswith("# Handoff: p"))
        self.assertTrue(short.endswith("\n" + handoff.CUT_OFF))
        self.assertEqual(handoff.shorten(self.document(body), 10), "")


class FallbackTest(unittest.TestCase):
    def test_carries_previous_narrative_once(self):
        first = handoff.fallback_body(VALID_BODY, "yesterday")
        self.assertTrue(first.startswith(handoff.CARRIED_OVER + " (saved yesterday)"))
        second = handoff.fallback_body(first, "yesterday")
        self.assertEqual(second.count(handoff.CARRIED_OVER), 1)
        self.assertTrue(second.endswith(VALID_BODY))

    def test_stub_when_nothing_to_carry(self):
        body = handoff.fallback_body("", None)
        self.assertEqual(
            handoff.problems(body, 12_000), ["missing or empty: '## User's rules', '## Done', '## In progress'"]
        )
        self.assertTrue(handoff.resume_prompt(body))


if __name__ == "__main__":
    unittest.main()
