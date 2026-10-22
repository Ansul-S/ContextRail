import unittest

from contextrail import handoff
from contextrail.facts import Activity, GitState
from tests.helpers import VALID_BODY


class ExtractTest(unittest.TestCase):
    def test_last_block_wins(self):
        text = "<contextrail-handoff>old</contextrail-handoff> then <contextrail-handoff>\nnew\n</contextrail-handoff>"
        self.assertEqual(handoff.extract(text), "new")

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
