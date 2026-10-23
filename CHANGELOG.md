# Changelog

## 0.5.0 (2026-10-23)

ContextRail stays out of Claude Code's way: handoffs load whole, scripted runs are left alone, handoffs before the wrap-up point are checkpoints you keep working through, and the companion plugin asks before it starts a new session. Your own trigger points are checked and explained. Lightning Mode, if you turn it on, starts a new session on its own at a context size you set.

### Upgrading

- Update both plugins, then restart Claude Code. `/clear` doesn't reload plugin hooks.
- The default `max_chars` is now 8,000 instead of 12,000. If you set it higher in a `config.json`, a long handoff loads shortened.
- ContextRail no longer runs in `claude -p` or the Agent SDK. To keep it running there, set `"unattended": true` or `CONTEXTRAIL_UNATTENDED=1`.
- `contextrail-continue` no longer has the `delay_seconds` and `max_in_a_row` settings, because it now asks every time.
- A new session is now offered at the wrap-up point (`hard_tokens`), not after the first handoff. Set `"new_session_at": "soft"` for the old behaviour.
- `soft_tokens`, `hard_tokens`, `soft_pct` and `hard_pct` of 0 are now ignored as invalid, and a `hard_tokens` you set at or below `soft_tokens` is replaced by `soft_tokens` + 30,000. `/contextrail:doctor` lists both.

### Lightning Mode

- **A new session at a context size you set:** `/contextrail:lightning 200k` turns it on. At 200k tokens of context, counted in all rather than from the session's start, ContextRail asks for the handoff. Once it is saved, `contextrail-continue` counts down 10 seconds in a blue pane and starts a new session from it. To stay, press n or Esc or send a prompt; text in the prompt box when the countdown ends also keeps the session. A session that starts near the limit still gets 20k tokens of work, and `soft_pct` still caps the limit. `/contextrail:lightning off` turns it off.
- **One setting for both plugins:** the limit is saved as `lightning_tokens` in `~/.contextrail/config.json`, and `CONTEXTRAIL_LIGHTNING_TOKENS` overrides it. A project's `config.json` can't set it, and `/contextrail:doctor` says so if one tries. `/contextrail:status` shows it.
- **The Bolt rail:** in Lightning Mode, ContextRail's status line is blue, runs between two ⚡, and ends where the new session starts: `new session at 200k · 48k to go`.

### Triggers and settings

- **Checkpoints, then the final handoff:** the handoff at the handoff point (`soft_tokens`) is now a checkpoint. It is saved, ContextRail says so, and work goes on in the same session, refreshed every `refresh_tokens`. At the wrap-up point (`hard_tokens`) ContextRail asks for the final handoff, and `contextrail-continue` offers a new session after it, still waiting while background tasks run. The new `new_session_at` setting (`"hard"`, or `"soft"` for every handoff) chooses where the offer comes. Lightning Mode always offers it at its own point.
- **Your thresholds, explained:** each request comes with a note on the handoff and wrap-up points and what set each one (your token settings or a window share), the window size and where it came from, the last saved handoff, and any tasks still running, with the reminder that nothing is stopped. `/contextrail:status` shows the same.
- **An assumed window says so:** when ContextRail can't detect the window size it assumes 200k. The request note, `/contextrail:status` and `/contextrail:doctor` (now a warning) say how far the 50% share moved the handoff, for example from 450,000 to 100,000, and how to fix it.
- **Settings are checked:** the token settings and shares must be above 0, `new_session_at` must be `"hard"` or `"soft"`, and a `hard_tokens` you set must be above `soft_tokens`. Raising only `soft_tokens` moves an unset `hard_tokens` along with it, 30,000 later.
- **Documented examples** for 200k and 1M windows and different ways of working, without claiming any number keeps a model reliable.
- **The start includes late listings:** Claude Code sometimes attaches its skill, agent and MCP listings after the first reply, about 30k tokens. The start was measured before them, so the handoff came about 30k early. When listings arrive between the first and second reply, the start is now the context at the second reply.
- **The wrap-up lets the step finish:** the wrap-up warning said "Don't start new work", and agents dropped the commands of the step they were on. It now asks them to finish that step, let running commands finish and show their results, and then stop.
- **Running tasks go into the handoff:** when ContextRail stops waiting for background tasks at the wrap-up point, the request names the tasks still running and asks for them under In progress, with how to check their results.
- **Numbers too big for an integer:** a value such as `1e400` in a `config.json` stopped ContextRail from reading its settings, so the hooks did nothing. It is now ignored like any other invalid value, and `/contextrail:doctor` lists it.

### Loading

- **A quoted tag no longer cuts a handoff short:** a handoff that mentioned `</contextrail-handoff>` in its own text, as in sessions about ContextRail, ended there, losing the sections after it. The block is now taken from tags that stand on lines of their own, with the old reading as a fallback. `contextrail-continue` reads the resume prompt the same way.
- **Handoffs load whole:** Claude Code puts at most 10,000 characters of a hook's context into the session. Past that, the agent got only a file path and a 2 KB preview, which left out most of the handoff. ContextRail now keeps what it loads under that limit. The new `max_chars` default leaves room for the facts it adds. A longer handoff is shortened in a fixed order: known facts, dead ends, decisions, how to run, done, open questions. Items go from the end of each section, each section keeps its first item as long as it can, and a note says how many were left out and where the full handoff is. `/contextrail:doctor` says when the saved handoff would load shortened.

### Measured

- **A handoff evaluation:** `python3 -m tools.eval` regenerates real handoffs at different caps, lists what the next session needed, and has a model judge which handoff covers it. [docs/eval.md](docs/eval.md) describes the method and its limits and has the numbers: on 4 cases, a 6,000-character cap covered 54% of the needs against 61% at 8,000, so `max_chars` stays at 8,000. It is a development tool and never runs inside the plugin.

### Scripted runs

- **Unattended sessions are left alone:** in `claude -p` and the Agent SDK, Claude Code sets `CLAUDE_CODE_SESSION_ATTENDED=0`, and the hooks now do nothing. There is no handoff request holding up the run's end, no handoff loaded into it, and no checkpoint replacing yours. The new `unattended` setting turns them back on. `/contextrail:doctor` warns when it runs in such a session.

### contextrail-continue

- **Asks instead of counting down:** after ContextRail saves a handoff, a pane asks "Press y to start a new Claude Code session. Press n to continue using the current session." and waits for your answer. y clears the session and sends the resume prompt. n or Esc keeps the session. On a terminal narrower than 144 columns, where Claude Code doesn't show a pane it didn't ask for, the same question comes up in Claude Code's question dialog. Sending a prompt withdraws the question. This replaces the 10-second countdown and the limit of 3 continues in a row; Lightning Mode brings the countdown back for those who want it.
- **Waits for background work:** a handoff saved while background or scheduled tasks are running is no longer dropped. A note says the question will come once they finish and the agent has shown their results, and then it does. Only a prompt you send or the end of the session cancels it. A new turn, such as a task's notification, takes an open question back until that turn ends.
- **Explains "Stop hook error occurred":** when ContextRail asks for a handoff, a toast says the label is ContextRail's request, not a failure.

### Docs

- The README explains the rail in more detail and notes the "Stop hook error" label.
- The README describes Lightning Mode, how the session's start is measured, and the new wrap-up.

## 0.4.0 (2026-10-23)

Handoffs come after a full session's work, not halfway through it.

### Upgrading

- Update both plugins, then restart Claude Code.
- If you raised `soft_tokens` or `hard_tokens` in a `config.json` to make room for the session's start, lower them again. Otherwise the start is counted twice.
- On a model with a 1M window whose name doesn't say `[1m]`, run `/contextrail:doctor --setup-statusline` or set `window_tokens`. Without either, ContextRail assumes a 200k window, and its 50% share keeps the handoff at 100k.

### Triggers

- **The triggers count from the session's start:** a session holds 30-50k tokens before any work (Claude Code's system prompt and tools, skills, MCP instructions, memory files and the loaded handoff), so on a 1M window the handoff at 100k left about 50k tokens of work. `soft_tokens`, `hard_tokens` and `checkpoint_tokens` now count from the context at the session's first reply, or at the first reply after compaction. With a 50k start, the handoff comes at about 150k and the wrap-up at 180k. `soft_pct` and `hard_pct` still cap the total, so on a 200k window the handoff stays at 100k, before auto-compaction. `/contextrail:status` shows the start, and the status line's handoff point moves with it.

## 0.3.0 (2026-10-23)

A status line that shows how far the session is from its next handoff.

### Upgrading

- Update both plugins, then restart Claude Code.
- The rail appears when ContextRail's launcher runs without a status line of its own. If you set it up in front of another status line, leave out the ` -- '…'` part of the `statusLine` command in `~/.claude/settings.json` to see the rail instead. If you have not set it up yet, run `/contextrail:doctor --setup-statusline`.

### Status line

- **Status line:** ContextRail's own status line is now a rail that shows how close the session is to its next handoff: `━━━━━━━━━●┄┄┄┄┄┄┄┄◆┄┄┄┄┄  52k · handoff at 100k · 48k to go · 5% of 1M window`, followed by the model and the project. The colour goes from green to amber near the handoff and to red past the wrap-up point. The text says what ContextRail does next: the handoff point, a request that is due or made, waiting for background tasks, a saved handoff and how long ago, or a handoff that was never written. On narrow terminals the least useful parts are left out, and `NO_COLOR` turns the colours off. It replaces `ctx 52% · 104k/200k · handoff at 100k`. A status line of your own that runs through the launcher still passes through unchanged.

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
