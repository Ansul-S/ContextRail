# Handoff evaluation

ContextRail asks the agent for a handoff under a size cap, `max_chars`. A tighter cap loads into fewer tokens in every next session, but can leave out what that session needs. This evaluation measures that trade-off on real sessions, so the default cap rests on numbers rather than a guess.

It is a development tool in `tools/eval/`. It never runs inside the plugin, uses only the Python standard library, and calls `claude -p`, so a run uses your Claude Code quota.

## Method

1. **Cases.** A case is a session A that saved a handoff, and the session B that loaded it next and worked from it. `tools/eval/cases.py` finds them in Claude Code's transcripts. A B that may still be running, a scripted B, and a B with no message from the user are left out.
2. **Regenerated handoffs.** For each cap, A is forked at its last reply before the handoff request (`claude -p --resume A --resume-session-at <reply> --fork-session`), with the same model and effort, and sent ContextRail's request for that cap. The fork saves no transcript, and ContextRail is turned off inside it. The handoff A actually saved is the `saved` variant.
3. **Needs.** One model call reads the handoff B loaded and a condensed copy of B up to its own handoff: messages, the agent's text, tool calls, and tool results cut to 400 characters. It lists up to 25 things B needed from the previous session: information B used from the handoff, state it had to work out again, things the user had to explain again, and mistakes a handoff could have prevented.
4. **Verdicts.** A judge model compares two handoffs at a time against the needs. It sees the recorded facts both would load with, and not where each need came from. For each need and each handoff it answers covered, partial, missing or wrong, then says which handoff serves the next session better. Every pair is judged in both orders, to cancel out a preference for the first or second position.
5. **Numbers.**
   - Coverage is (covered + ½ × partial) ÷ needs, averaged over the judgings of a handoff, then over cases.
   - Gaps covered is the same, counted only over needs the loaded handoff did not meet (everything but what B used from it).
   - Wrong per case counts the needs a handoff contradicts.
   - Over a limit counts handoffs that ContextRail would have sent back for a fix: a missing section, over the cap, or a resume prompt that is too long.

## Running it

From the repository root:

```
python3 -m tools.eval cases
python3 -m tools.eval run --cases KEY,KEY --caps 6000,8000 --dry-run
python3 -m tools.eval run --cases KEY,KEY --caps 6000,8000
python3 -m tools.eval report --write
```

- `cases` lists the cases found in the current folder's sessions, and why others were left out.
- `run` regenerates, lists needs and judges. Without `--cases` it takes the oldest cases, up to `--limit` (5). `--judge` picks the model for the needs and the verdicts (default `opus`). `--dry-run` lists the calls without making them.
- A run skips every result that already exists, stops at the first error, and records what it did in `run.json`. Run it again to carry on.
- `report` prints the numbers of the last run; `--write` puts them in the section below.

Data stays outside the repository, in `~/.contextrail/eval/` (or under `CONTEXTRAIL_HOME`): `cases/<key>/` holds `case.json`, `gen-<cap>.json`, `needs.json` and `judge-<first>-<second>.json`. These hold session content. The report holds numbers only.

## Limits

- **Few cases, one project, one person.** The numbers describe these sessions, not handoffs in general.
- **The needs lean towards the saved handoff.** What B used from the handoff it loaded is, by definition, in that handoff. "Gaps covered" leaves those needs out.
- **A fork is not the original moment.** The forked session rebuilds its system prompt now, so details such as the current git status can differ from when A wrote its handoff. Claude Code's auto-memory is turned off in every call (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`), because today's memory knows what happened after A.
- **A model judges a model.** Verdicts can be wrong; judging each pair in both orders reduces position bias, not every bias.

## Results

**What the 0.5.0 run decided:**
- `max_chars` stays at 8,000. At 6,000, coverage fell from 61% to 54%, and the 6,000 handoff never won a judging against the 8,000 one.
- The saved handoffs, written under the old 12,000 cap, came out level with 8,000 on coverage (62% against 61%) and were preferred in 6 of 8 judgings. Part of that is the lean described above.
- Agents overshoot the cap: 2 of 4 handoffs at each cap were over it, by 8% to 26%. ContextRail asks once for a shorter version, which this run leaves out, so in use these cost one more turn.
- In case 3, both regenerated handoffs quoted the closing tag `</contextrail-handoff>` in their text, and ContextRail's parser ended the handoff there, losing its last four sections; that is the other "over a limit" in each row. 0.5.0 fixes the parser. Both caps lost the same, so the comparison between them holds: without case 3, coverage is 55% at 6,000 and 63% at 8,000.
- One regeneration (case 4, cap 8,000) took a tool call before answering, so it may have read files as they are now.

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
