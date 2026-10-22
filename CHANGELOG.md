# Changelog

## Unreleased

- **Handoff template v2:** a required `## User's rules` section quotes the user's constraints, approvals and preferences in their own words. New `## Known facts` (locations, counts, names, IDs, root causes) and `## How to run` (commands, working directory, environment, guards that block actions) sections. Each open question now comes with its background, the options and a recommendation, and the first next step continues the user's latest request. New rules: only messages the user typed count as requests or approval, numbers and IDs are copied exactly, and code is named rather than cited by line number. The loaded handoff tells the next session to keep to the user's rules. The default `max_chars` is now 12,000.
- **Waits for background work:** when background tasks (a shell command, a subagent) are still running at the soft limit, ContextRail asks for the handoff once they finish, since their results would change it. It says so once per session, and asks anyway at the hard limit. Scheduled tasks such as `/loop` don't hold it.
- **Accurate "Recent checks":** each shell command is reduced to the test, lint or build command inside it, without `cd`, heredoc bodies or long prefixes. When a pipe or a later command hides the exit status, the result comes from the summary line the tool prints. If there is none, the check is shown as "ran" instead of "passed". Commands that were denied, or sent to the background, show "result unknown".
- **Edited files:** files outside the project are counted instead of listed.
- **`/contextrail:doctor`:** checks Python and how long a hook takes to start, the plugin's hooks, storage, settings (including keys and values that are ignored), when each hook last ran, whether usage can be read from the transcripts, whether the saved handoff would load, and recent errors. `cr.py doctor` exits 1 if a check fails.
- **Heartbeat:** each hook notes when it last ran in `~/.contextrail/heartbeat.json`, at most once a minute.
- **Status:** `/contextrail:status` mentions errors from the last 24 hours.
- **`/contextrail:history`:** lists a project's saved handoffs, newest first, with each one's date, size and first next step. `--restore N` makes an archived handoff current again. Archived handoffs now keep their `meta.json` beside them, so a restored one gets the drift check.
- **`contextrail-continue` (optional):** a second plugin in the marketplace, built as a mod, that clears the session and sends the resume prompt once ContextRail has asked for a handoff and saved it. It waits 10 seconds first and stays in the session when you start typing, when background or scheduled tasks are pending, or after 3 continues in a row without you. Needs a Claude Code version with mods (early access).
- **Fix:** when two handoffs were archived in the same second, the newer one could sort as older and be pruned first.
- **Fix:** `inject_on: ["resume"]` had no effect, because the SessionStart hook only ran on startup, `/clear` and compaction. The hook now runs on every session start, and `inject_on` decides.

## 0.1.0 (2026-10-22)

First release.

- **Handoff request:** the Stop hook asks for a handoff at 100k tokens or 50% of the window, whichever comes first, and again every 40k tokens if the session continues.
- **Mid-turn warning:** the PostToolUse hook tells the agent to wrap up at 130k tokens or 65% of the window.
- **Before compaction:** the PreCompact hook saves the session facts and carries over the last narrative.
- **Session start:** the SessionStart hook loads the handoff on startup, `/clear` and compaction, together with what changed in the repository since.
- **Validation:** an incomplete handoff gets one request for a fix.
- **Storage:** handoffs live in `~/.contextrail/`, with a history of previous ones.
- **Commands:** `/contextrail:handoff` and `/contextrail:status`.
