# Changelog

## 0.2.0 (2026-10-23)

Better handoffs, facts you can trust, and fewer ways to lose work.

### Upgrading

- Restart Claude Code after updating, so the new SessionEnd hook is registered. `/contextrail:doctor` confirms the hooks run.
- Handoffs saved by 0.1.0 still load. Their drift check compares git state only, as before.
- A handoff now needs a `## User's rules` section ("None stated" is fine), and the default `max_chars` is 12,000 instead of 8,000.
- Optional: `/contextrail:doctor --setup-statusline` for the exact window size, and `/plugin install contextrail-continue@contextrail` for hands-free continue.

### Handoffs

- **Handoff template v2:** a required `## User's rules` section quotes the user's constraints, approvals and preferences in their own words. New `## Known facts` (locations, counts, names, IDs, root causes) and `## How to run` (commands, working directory, environment, guards that block actions) sections. Each open question now comes with its background, the options and a recommendation, and the first next step continues the user's latest request. New rules: only messages the user typed count as requests or approval, numbers and IDs are copied exactly, and code is named rather than cited by line number. The loaded handoff tells the next session to keep to the user's rules. The default `max_chars` is now 12,000.
- **Handoff checked against the facts:** before saving, ContextRail compares the handoff with what it recorded in the session. It asks for a fix (within the existing single fix request) when uncommitted files edited in the session are never mentioned, when Done gives no evidence at all, when the first next step names no file, function or command, or when a Done item says a check passed that the facts show failed. The checks are deliberately loose: of 11 real handoffs they were tried on, one would have drawn a request, for a file it really left out. Turn them off with `"content_checks": false`.
- **Checkpoint when a session ends without a handoff:** a new SessionEnd hook saves the session's facts, with the last narrative carried over, when the session edited files, reached `checkpoint_tokens` (30,000), and saved no handoff since the last `refresh_tokens`. A `/clear` before the soft limit no longer loses track of the work. Claude Code stops SessionEnd hooks after about 1.5 seconds, so a checkpoint that can't be written well inside that is skipped rather than left half-written; crashes don't run the hook at all.

### Facts and drift

- **Accurate "Recent checks":** each shell command is reduced to the test, lint or build command inside it, without `cd`, heredoc bodies or long prefixes. When a pipe or a later command hides the exit status, the result comes from the summary line the tool prints. If there is none, the check is shown as "ran" instead of "passed". Commands that were denied, or sent to the background, show "result unknown".
- **Edited files:** files outside the project are counted instead of listed.
- **Drift check names files and claims:** a saved handoff now notes a fingerprint of each uncommitted file. The next session is told which files changed since (committed or not, new, deleted or reverted), which Done and In progress items name one of them, to re-check those, and to start with the Verify first commands. Files that were uncommitted at the handoff and are unchanged don't count, so a `/clear` right after a handoff still reports no changes. The list stays under about 1,000 characters. Handoffs saved before this version get the previous, git-state-only check.

### Triggers and window size

- **Waits for background work:** when background tasks (a shell command, a subagent) are still running at the soft limit, ContextRail asks for the handoff once they finish, since their results would change it. It says so once per session, and asks anyway at the hard limit. Scheduled tasks such as `/loop` don't hold it.
- **Exact window size (optional):** `/contextrail:doctor --setup-statusline` writes a launcher that sits in front of your status line, records the window size Claude Code reports there, and passes your status line through unchanged. The hooks then use that size, so percentages are right on models with a 1M window, and raised caps work. It prints the settings entry instead of editing `settings.json`. With no status line of your own, it shows `ctx 52% · 104k/200k · handoff at 100k`. `/contextrail:status` and the doctor say where the window size came from.

### Commands

- **`/contextrail:doctor`:** checks Python and how long a hook takes to start, the plugin's hooks, storage, settings (including keys and values that are ignored), when each hook last ran, whether usage can be read from the transcripts, whether the saved handoff would load, and recent errors. `cr.py doctor` exits 1 if a check fails.
- **`/contextrail:history`:** lists a project's saved handoffs, newest first, with each one's date, size and first next step. `--restore N` makes an archived handoff current again. Archived handoffs now keep their `meta.json` beside them, so a restored one gets the drift check.
- **Status:** `/contextrail:status` mentions errors from the last 24 hours.
- **Heartbeat:** each hook notes when it last ran in `~/.contextrail/heartbeat.json`, at most once a minute.

### Optional plugin

- **`contextrail-continue` (optional):** a second plugin in the marketplace, built as a mod, that clears the session and sends the resume prompt once ContextRail has asked for a handoff and saved it. It waits 10 seconds first and stays in the session when you start typing, when background or scheduled tasks are pending, or after 3 continues in a row without you. Needs a Claude Code version with mods (early access).

### Fixes

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
