# Handoff evaluation

ContextRail asks the agent for a handoff under a size cap, `max_chars`. A tighter cap loads into fewer tokens in every next session, but can leave out what that session needs. This evaluation measures how much of what the next session needed a handoff gave it, on real sessions, so choices such as the default cap rest on numbers rather than a guess.

It is a development tool in `tools/eval/`. It never runs inside the plugin, uses only the Python standard library, and calls `claude -p`, so a run uses your Claude Code quota.

**Versions.** Three numbers describe a result, and the report gives each one: the date of the run, the version of ContextRail whose prompts were used, and the benchmark version (`BENCHMARK` in `tools/eval/steps.py`), which changes when the method does. Benchmark 1 is the method of the run below. Benchmark 2 adds the baselines, the held-out split and the human review, and judges ContextRail's own handoffs exactly as benchmark 1 did, so those numbers stay comparable.

## What it measures, and what it doesn't

It measures **document coverage**: how many of the things session B needed a handoff written at A's last moment gives, and how many it gets wrong. It doesn't measure outcomes: whether B finished its task, how long B took to its first correct action, how much work B repeated, how often the user had to explain again, or the cost and time a handoff adds to a session. Those need sessions replayed live from each handoff, with people or a fixed task to judge the result, and this tool doesn't do that yet. Nothing here shows that ContextRail prevents hallucination, and the docs make no such claim.

## Method

1. **Cases.** A case is a session A that saved a handoff, and the session B that loaded it next and worked from it. `tools/eval/cases.py` finds them in Claude Code's transcripts. A B that may still be running, a scripted B, and a B with no message from the user are left out. It reads requests in both forms Claude Code stores them in: a blocked stop's "Stop hook feedback" message, and Stop feedback (2.1.163 and later).
2. **Split.** Each case is either a tuning case (30%) or a held-out one, fixed by a hash of its key (`cases.split`), so a case never changes sides. Tune prompts and thresholds on the tuning cases only, and judge the result on the held-out ones: `run --split tune` or `--split test`. `cases` shows each case's split, and the report gives coverage on the held-out cases alone.
3. **Regenerated handoffs.** For each cap, A is forked at its last reply before the handoff request (`claude -p --resume A --resume-session-at <reply> --fork-session`), with the same model and effort, and sent ContextRail's request for that cap. The fork saves no transcript, and ContextRail is turned off inside it. The handoff A actually saved is the `saved` variant.
4. **Baselines** (`run --baselines notes,facts`). ContextRail's handoff is a narrative the agent writes plus facts ContextRail records itself. The baselines show what each part adds:
   - `notes`: at the same fork point, A is asked plainly to "write notes for whoever continues this work in a new session", with no template, no rules and no recorded facts. This is the handoff a user would get by asking for one without ContextRail.
   - `facts`: the facts ContextRail records, alone, without the narrative. No model call makes it.

   Each baseline is judged against the saved handoff, in both orders. Two more baselines are out of this tool's reach. Carrying on in the full context has no document to judge, and Claude Code's own compaction can't be reproduced reliably from a forked `claude -p`. Comparing against those needs the live outcome study above.
5. **Needs.** One model call reads the handoff B loaded and a condensed copy of B up to its own handoff: messages, the agent's text, tool calls, and tool results cut to 400 characters. It lists up to 25 things B needed from the previous session: information B used from the handoff, state it had to work out again, things the user had to explain again, and mistakes a handoff could have prevented.
6. **Verdicts.** A judge model compares two handoffs at a time against the needs. Between two of ContextRail's handoffs it sees the recorded facts both would load with. Against a baseline, each side is shown with everything the next session would get: the saved handoff with its facts, the notes alone. It doesn't see where each need came from. For each need and each handoff it answers covered, partial, missing or wrong, then says which handoff serves the next session better. Every pair is judged in both orders, to cancel out a preference for the first or second position.
7. **Numbers.**
   - Coverage is (covered + ½ × partial) ÷ needs, averaged over the judgings of a handoff, then over cases.
   - Gaps covered is the same, counted only over needs the loaded handoff did not meet (everything but what B used from it).
   - Wrong per case counts the needs a handoff contradicts.
   - Over a limit counts handoffs that ContextRail would have sent back for a fix: a missing section, over the cap, or a resume prompt that is too long. Baselines aren't held to the template, so their cell is "–".
8. **People.** `export --out DIR` writes the needs and every handoff of each case of the last run, under letters in a random order, plus one sheet to fill in and instructions. The key that maps the letters back goes to `DIR.key.json`, outside the folder, so give reviewers the folder only. `agreement SHEET.csv ... --key DIR.key.json` reads the filled-in sheets and prints the coverage each reviewer found per handoff, and how often reviewers agree with each other and with the model judge (share of items and Cohen's kappa). Report disagreement as it comes out. Blinding is partial: plain notes look different from ContextRail's sections, so it hides which cap or version a document is, not always whether it is a baseline.

## Running it

From the repository root:

```
python3 -m tools.eval cases
python3 -m tools.eval run --split test --caps 6000,8000 --baselines notes,facts --dry-run
python3 -m tools.eval run --split test --caps 6000,8000 --baselines notes,facts
python3 -m tools.eval report --write
python3 -m tools.eval export --out ~/review
python3 -m tools.eval agreement ~/review-ana.csv ~/review-ben.csv --key ~/review.key.json
```

- `cases` lists the cases found in the current folder's sessions with their split, and why others were left out.
- `run` regenerates, writes the baselines, lists needs and judges. Without `--cases` it takes the oldest cases of the split, up to `--limit` (5). `--judge` picks the model for the needs and the verdicts (default `opus`). `--dry-run` lists the calls without making them: check the count before a real run. Each baseline adds 2 judgings per case, and `notes` one fork as well.
- A run skips every result that already exists, stops at the first error, and records what it did in `run.json`, with the benchmark and ContextRail versions. Run it again to carry on.
- `report` prints the numbers of the last run; `--write` puts them in the section below.

Data stays outside the repository, in `~/.contextrail/eval/` (or under `CONTEXTRAIL_HOME`): `cases/<key>/` holds `case.json`, `gen-<cap>.json`, `gen-notes.json`, `needs.json` and `judge-<first>-<second>.json`. These hold session content, and so do the review sheets. The report holds numbers only. `/contextrail:purge --everything` deletes the eval data with everything else.

## Targets for a release

These are proposed gates for claiming that ContextRail helps. None of them has been measured yet:
- On the held-out cases, ContextRail's handoff covers more of the needs than the best baseline, with no more wrong items, and people reviewing a sample agree (kappa above 0.6) before the number is published.
- At least 90% of the needs marked critical by reviewers are covered, with no wrong one left unresolved in the reviewed sample.
- An outcome study (above) shows less re-explaining or a faster first correct action than the best baseline, by more than the time and tokens the handoff costs.
- If an adaptive trigger policy is tried, it beats the fixed thresholds on the held-out cases, or the fixed thresholds stay.

## Limits

- **Few cases, one project, one person.** The run below has 4 cases from this repository's own development. The numbers describe these sessions, not handoffs in general. A dataset worth publishing needs many more transitions, from several projects and people, including hard ones: failed tests, interrupted changes, background work, stale files, unanswered questions.
- **The needs lean towards the saved handoff.** What B used from the handoff it loaded is, by definition, in that handoff. "Gaps covered" leaves those needs out.
- **A fork is not the original moment.** The forked session rebuilds its system prompt now, so details such as the current git status can differ from when A wrote its handoff. Claude Code's auto-memory is turned off in every call (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`), because today's memory knows what happened after A.
- **A model judges a model.** Verdicts can be wrong. Judging each pair in both orders reduces position bias, not every bias, and the human review exists to measure how far the judge can be trusted.

## Results

**Run of 23 October 2026, benchmark 1, ContextRail 0.5.0 prompts** (Claude Code 2.1.309). What it decided:
- `max_chars` stays at 8,000. At 6,000, coverage fell from 61% to 54%, and the 6,000 handoff never won a judging against the 8,000 one.
- The saved handoffs, written under the old 12,000 cap, came out level with 8,000 on coverage (62% against 61%) and were preferred in 6 of 8 judgings. Part of that is the lean described above.
- Agents overshoot the cap: 2 of 4 handoffs at each cap were over it, by 8% to 26%. ContextRail asks once for a shorter version, which this run leaves out, so in use these cost one more turn.
- In case 3, both regenerated handoffs quoted the closing tag `</contextrail-handoff>` in their text, and ContextRail's parser ended the handoff there, losing its last four sections; that is the other "over a limit" in each row. 0.5.0 fixes the parser. Both caps lost the same, so the comparison between them holds: without case 3, coverage is 55% at 6,000 and 63% at 8,000.
- One regeneration (case 4, cap 8,000) took a tool call before answering, so it may have read files as they are now.
- It had no baselines and no held-out split, and no person reviewed its verdicts. It says nothing about ContextRail against plain notes or compaction.

<!-- eval:results -->
Run of 2026-10-23 with Claude Code 2.1.309: 4 cases, 91 needs. Handoffs written by claude-opus-5-5 (xhigh effort); needs and verdicts by opus. Each pair of handoffs was judged in both orders. Cost as Claude Code reported it: $41.36.

| Handoff | Coverage | Gaps covered | Wrong per case | Mean length | Over a limit | Mean time |
|---|--:|--:|--:|--:|--:|--:|
| Saved (cap 12,000) | 62% | 14% | 0.5 | 8,942 | 0 of 4 | – |
| Cap 6,000 | 54% | 10% | 0.5 | 6,395 | 3 of 4 | 38 s |
| Cap 8,000 | 61% | 20% | 0.5 | 8,065 | 3 of 4 | 44 s |

| Pair | Wins | Ties |
|---|--:|--:|
| Saved (cap 12,000) vs Cap 6,000 | 8 – 0 | 0 |
| Saved (cap 12,000) vs Cap 8,000 | 6 – 2 | 0 |
| Cap 6,000 vs Cap 8,000 | 0 – 6 | 2 |

| Case | Context at the handoff | Messages in B | Needs | Saved (cap 12,000) | Cap 6,000 | Cap 8,000 |
|---|--:|--:|--:|--:|--:|--:|
| 1 | 113,041 | 5 | 24 | 68% | 57% | 61% |
| 2 | 186,842 | 3 | 25 | 51% | 47% | 52% |
| 3 | 190,148 | 1 | 18 | 58% | 51% | 53% |
| 4 | 162,696 | 1 | 24 | 72% | 61% | 77% |
<!-- /eval:results -->
