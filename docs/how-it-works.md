# How ContextRail works

The [README](../README.md) covers installing and using ContextRail. This page has the details: when it triggers and why, every setting, the status line, the companion plugin, Lightning Mode, and an FAQ.

## The hooks

ContextRail is a set of Claude Code hooks, each a short Python process:

- **Stop** (when the agent finishes a turn): asks for a handoff when one is due, and saves the handoff when the agent replies with one.
- **PostToolUse** (after each tool call): past the wrap-up point, tells the agent mid-turn to finish the step it is on.
- **PreCompact**: saves the recorded facts before compaction, carrying over the last narrative.
- **SessionStart**: loads the handoff into a new session, with what changed in the repository since.
- **SessionEnd**: saves a checkpoint of the facts when a session that edited files ends without a handoff.

The agent writes its handoff as a `<contextrail-handoff>` block in its reply. ContextRail checks it, adds the facts it recorded itself, and saves it.

## When it triggers, and why

| Setting | Fallback | Meaning |
| --- | --- | --- |
| `soft_tokens` / `soft_pct` | 100,000 / 50% | The handoff point: this many tokens past the session's start, or this share of the window, whichever comes first. Before the wrap-up point, the handoff is a checkpoint and work goes on. |
| `hard_tokens` / `hard_pct` | 130,000 / 65% | The wrap-up point, worked out the same way: the agent is told mid-turn to wrap up, ContextRail asks for the final handoff, and a new session is offered. Also the longest ContextRail waits for background tasks or for your answer to a question. |
| `new_session_at` | `"hard"` | Where `contextrail-continue` offers a new session: at the wrap-up point, or `"soft"` for after every handoff. Lightning Mode always offers it at its own point. |
| `refresh_tokens` | 40,000 | While you keep working after a handoff, ask again after this much growth. |

How they combine:

```
handoff point = min(start + soft_tokens, window × soft_pct %)
wrap-up point = min(start + hard_tokens, window × hard_pct %), and never before the handoff point
```

- **Your token settings decide** whenever the window is big enough. The shares are a guard for small windows: they can only bring a point forward, never push it back. To loosen a guard, raise `soft_pct` or `hard_pct` (up to 100).
- **Each setting can be set on its own.** If you raise only `soft_tokens` past the fallback `hard_tokens`, the wrap-up point follows it, 30,000 later.
- **Settings are checked.** `soft_tokens` and `hard_tokens` must be above 0, and the shares 1 to 100; an invalid value is ignored. If you set `hard_tokens` yourself and it is not above `soft_tokens`, ContextRail uses `soft_tokens` + 30,000 until you fix it. `/contextrail:doctor` and `/contextrail:status` say so.
- **Every request says why.** Its note gives both points, what set each one, the window size and where it came from, and any tasks still running. Claude Code keeps that note behind ctrl+o, because it labels the request "Stop hook error occurred"; `/contextrail:status` shows the same.

Examples. These are starting points to try, not guarantees: no setting keeps a model from making mistakes, and where long sessions start to slip depends on the model and the task.

| Your setup | `config.json` | Handoff and wrap-up, with a 50k start |
| --- | --- | --- |
| 200k window, the fallbacks | nothing | 100k and 130k: the shares decide |
| 1M window, long working sessions | `{"soft_tokens": 400000, "hard_tokens": 550000}` | 450k and 600k |
| 1M window, shorter sessions | `{"soft_tokens": 150000, "hard_tokens": 200000}` | 200k and 250k |
| 200k window, more room before the handoff | `{"soft_pct": 60, "hard_pct": 70}` | 120k and 140k |
| Quick tasks, a fresh session at a fixed size | `/contextrail:lightning 150k` | 150k, then a new session |

On a 1M window, the large settings only work when ContextRail knows the window size (see below). Otherwise it assumes 200k, and the shares of that bring the handoff to 100k.

Why the fallbacks are what they are:

- **Sessions don't start empty.** Claude Code's system prompt and tools, skills, MCP instructions, `CLAUDE.md` files and the loaded handoff often add up to 30-50k tokens before any work. So the token settings count from the context at the session's first reply, or the first one after compaction. Claude Code sometimes sends its skill, agent and MCP listings only after the first reply, which can add about 30k tokens; then the start is the context at the second reply. `/contextrail:status` shows the start.
- **Usable space is smaller than the window.** Claude Code reserves roughly 16% of the window for auto-compaction, so 50% of the raw window is about 60% of the space you can actually use.
- **The agent needs to still be sharp.** Asking at 50% leaves headroom for writing the handoff (3-5k tokens) while the agent is still at its best. Auto-compaction fires around 83%, which is too late for a good summary.
- **Absolute tokens and shares do different jobs.** Answer quality can decline gradually as a session grows, whatever the window size, but where that starts depends on the model and the task. The fallback token settings are cautious; on a 1M window they decide, well before the 50% share. On a 200k window the shares decide instead, so a session still hands off before auto-compaction.

### What waits

A request is held back, never dropped, in three cases. Each wait ends at the wrap-up point at the latest:

- **Background tasks are running** (a test run in the background, a subagent): their results belong in the handoff, and finishing wakes the session, whose next stop asks. ContextRail says so once.
- **The agent is waiting for your answer:** its reply ends with a question (the last line ends with `?`). ContextRail says once that the request waits, and asks at the stop after your answer.
- **The final handoff was written while tasks still ran** (at the wrap-up point the request comes anyway): once they finish, ContextRail asks for the handoff again with their results, so the next session has them.

### Measuring the context

Context usage is read from the session transcript, the way Claude Code computes it for `/context`: the context sent with the latest reply. Claude Code writes the reply that ends a turn to the transcript around the time the Stop hook runs, sometimes only after it. Within 30k tokens of the next request point, the Stop hook waits up to 0.5 s for that reply; elsewhere it doesn't wait, so turns end as fast as without it. When the reply arrives only after the hook, the request can come one turn late.

Hook payloads don't include the window size. ContextRail takes it from `window_tokens` if set, else from the status-line wrapper below, else from a `[1m]` in the model name, else from usage that has already passed 200k. Failing all of those it assumes 200k. Several current models have a 1M window without saying so in their name. With an assumed window, `/contextrail:doctor` warns, and the request note and `/contextrail:status` say how far the 50% share moved the handoff.

## The status line

Claude Code tells the status line the window size, but not the hooks. ContextRail can sit in front of your status line, note the size for its hooks, and pass your status line through unchanged:

```
/contextrail:doctor --setup-statusline
```

This writes a small launcher to `~/.contextrail/bin/statusline` and prints the `"statusLine"` entry to put in `~/.claude/settings.json`, with your current status line already filled in. ContextRail never edits that file. The launcher finds the plugin after updates, and if ContextRail is removed it runs your status line on its own.

If you have no status line, the launcher shows ContextRail's own: a rail that fills up as the session grows.

```
━━━━━━●┄┄┄┄┄┄┄┄┄┄┄┄┄◆┄┄┄  52k · handoff at 150k · 98k to go · 5% of 1M window · Opus 5.5 · my-project
```

- **The rail** runs from an empty context to the wrap-up point. `━` is context used, `●` is where the session is now, `┄` is the room left, and `◆` is where ContextRail will ask for the next handoff. After a save, `◇` marks where it was written.
- **What happens next:**

  | Text | Meaning |
  | --- | --- |
  | `handoff at 150k · 98k to go` | Nothing to do yet. |
  | `◆ handoff due` | The session has passed the request point. The request goes out when the agent next finishes a turn. |
  | `◷ waiting for background tasks` | The request is due, but tasks are still running. |
  | `◆ handoff requested` | The agent has been asked and is writing it. |
  | `✓ saved 3m ago · next at 192k` | A handoff is saved. Type `/clear` to continue from it, or keep working. |
  | `✗ no handoff written · /contextrail:handoff` | The agent ignored three requests. Ask for one yourself. |
  | `ContextRail off` | It is turned off for this project. |

- **Colours:** green while there is plenty of room, amber as the handoff gets close, red at the wrap-up point. `NO_COLOR` turns them off. On a narrow terminal, the project, the model and the window share are left out in that order, then the rail gets shorter.

## contextrail-continue

ContextRail leaves `/clear` to you, because hooks can't run slash commands. The optional `contextrail-continue` plugin asks you instead, once ContextRail has saved the final handoff:

```
ContextRail saved the handoff.
Press y to start a new Claude Code session. Press n to continue using the current session.
y: Start a new session   n: Keep this session
```

- **y** runs `/clear` and sends the handoff's resume prompt, so the work carries on in a fresh session with the handoff loaded.
- **n**, or Esc, keeps the current session. The handoff stays saved, and `/clear` loads it whenever you are ready.
- **Sending a prompt** withdraws the question; you stay in the current session.
- **It waits for running work and for your answers.** While background or scheduled tasks (such as `/loop`) are pending, it holds the question until they finish and their results are shown. At a stop where the agent waits for your answer, it holds it too.
- **Narrow terminals:** Claude Code only shows a pane it didn't ask for in a terminal at least 144 columns wide. In a narrower one, the plugin asks the same question in Claude Code's question dialog.
- **It explains the label:** when ContextRail asks for a handoff, the plugin notes that the "Stop hook error" above is ContextRail's request.

| Setting | Default | Meaning |
| --- | --- | --- |
| `after_manual` | off | Also ask after a handoff you asked for with `/contextrail:handoff`. |

## Lightning Mode

```
/contextrail:lightning 200k
```

- **At 200k tokens of context**, counted in all rather than from the session's start, ContextRail asks for the handoff.
- **Once it is saved**, `contextrail-continue` counts down 10 seconds in a blue pane, then runs `/clear` and sends the resume prompt.
- **To stay**, press n or Esc, or send a prompt. Text in the prompt box when the countdown ends also keeps the session. y starts the new session at once.
- **The rail turns blue**, between two bolts, and ends where the new session starts: `⚡━━━━━━━━━━━━●┄┄┄┄⚡  152k · new session at 200k · 48k to go`.

The limit can be written 200k, 0.2M, 200000 or 200,000, and must be at least 10k. A session that starts near it still gets 20k tokens of work first. `soft_pct` still caps it. The wrap-up warning comes `hard_tokens` − `soft_tokens` (30k with the fallbacks) after the Lightning point. It is saved as `lightning_tokens` in `~/.contextrail/config.json`, so it applies in every project; `CONTEXTRAIL_LIGHTNING_TOKENS` overrides it. Without `contextrail-continue`, the handoff is still saved at the limit, and `/clear` continues from it.

## All settings

Settings are applied in this order, later ones winning: built-in defaults, `~/.contextrail/config.json`, `~/.contextrail/projects/<project>/config.json`, then environment variables (`CONTEXTRAIL_` plus the key in upper case).

```json
{
  "soft_tokens": 100000,
  "soft_pct": 50,
  "hard_tokens": 130000,
  "hard_pct": 65,
  "new_session_at": "hard",
  "window_tokens": null,
  "refresh_tokens": 40000,
  "max_chars": 8000,
  "content_checks": true,
  "checkpoint_tokens": 30000,
  "max_attempts": 3,
  "inject_on": ["startup", "clear", "compact"],
  "max_age_days": 30,
  "copy_resume_prompt": true,
  "history_keep": 20,
  "unattended": false,
  "lightning_tokens": 0,
  "enabled": true
}
```

- `max_chars` caps the part the agent writes (8,000 characters, about 2k tokens). Together with the facts ContextRail adds, that fits Claude Code's 10,000-character limit for what a hook loads into a session. A longer handoff loads shortened, least important items first, with a pointer to the full file. [eval.md](eval.md) compares caps on real sessions.
- `content_checks` holds the handoff against what ContextRail recorded: uncommitted files edited in the session that it never mentions, a Done section with no evidence, a first next step with nothing concrete in it, and a Done item that says a check passed when the facts show it failed. A gap gets one fix request.
- `checkpoint_tokens` is how far a session must grow past its start, having edited files, to get a checkpoint when it ends without a handoff.
- `max_attempts` is how often one session is asked before ContextRail gives up and tells you to run `/contextrail:handoff`.
- `inject_on` sets which session starts load the handoff (`startup`, `clear`, `compact`, `resume`). `max_age_days` skips older handoffs.
- `copy_resume_prompt` puts the resume prompt on your clipboard when a handoff is saved.
- `unattended` lets ContextRail run in sessions nobody is watching, such as `claude -p` and the Agent SDK. Off by default, so a scripted run neither asks for a handoff nor replaces yours.
- `lightning_tokens` works only in `~/.contextrail/config.json` and `CONTEXTRAIL_LIGHTNING_TOKENS`, so both plugins read the same value.
- `CONTEXTRAIL_DISABLE=1` turns ContextRail off everywhere; `{"enabled": false}` in a project's `config.json` turns it off there. `CONTEXTRAIL_HOME` moves the data folder; use an absolute path.

## Where files live

```
~/.contextrail/projects/<project-path-slug>/
  HANDOFF.md          current handoff (overwritten each time)
  RESUME_PROMPT.txt   the resume prompt
  history/            previous handoffs, each with its meta (never loaded automatically)
  meta.json           git state and file fingerprints at save time, for the drift check
  state.json          per-session trigger state
  config.json         optional per-project settings
```

`~/.contextrail/` also holds `config.json`, `error.log` (errors in the hooks), `heartbeat.json` (when each hook last ran) and `plugin-root` (where the plugin is installed). With the status-line wrapper it also holds `bin/statusline` and `sessions/`, the window size of each session, kept for 7 days. A project is the git repository root, or the folder itself outside a repository; sessions started in subfolders share it.

## FAQ

**Why not just rely on auto-compaction?**
Compaction is a generic summary written at the last moment, and you stay in the same long session. A handoff is written earlier, follows a fixed structure, separates verified from unverified work, and keeps decisions and dead ends that summaries tend to drop. The two work together: ContextRail also saves before compaction and reloads afterwards.

**Does ContextRail change how Claude Code asks me things?**
No. Claude Code's question picker and permission prompts are Claude Code's own, and ContextRail neither answers nor hides them. Whether the agent uses the picker or asks in plain text is the model's choice. What ContextRail does is keep out of the way: no handoff request at a stop where the agent waits for your answer (until the wrap-up point), and no y/n question or countdown over it.

**What if the agent ignores the request?**
ContextRail asks at most three times per session, then tells you to run `/contextrail:handoff` when you are ready. An incomplete handoff, or one that leaves out what the session's facts show, gets one request for a fix. After that it is saved anyway, with a warning.

**What does "what changed since" cover?**
When a handoff is saved, ContextRail notes the commit and a fingerprint of each uncommitted file. When the next session loads it, ContextRail lists the new commits, which files changed since, and the Done and In progress items that name one of those files, and tells the agent to re-check them and run the Verify first commands.

**How does it know whether a check passed?**
It picks the test, lint or build command out of each shell command. If the command's exit status belongs to that check, it uses the exit status. If a pipe or a later command hides it, ContextRail reads the summary line the tool prints (unittest, pytest, jest, vitest, go, cargo, mypy, ruff and others). Without one, the check is listed as "ran", not "passed".

**Hooks feel slow.**
Each hook is one short Python process, usually under 0.1 s. Version-manager shims such as pyenv can add about 0.15 s per call; `/contextrail:doctor` measures it.

**Windows?**
It should work if `python3` is on your `PATH`; file locking and the clipboard use fallbacks. It is not tested in CI. The status-line launcher is a shell script, so it works on macOS and Linux only.

**Will it overwrite my notes?**
The previous `HANDOFF.md` moves to `history/` each time a new one is saved; the last 20 are kept. `/contextrail:history` lists them, and `--restore N` brings one back.

## Layout of the repository

```
.claude-plugin/        plugin and marketplace manifests
hooks/hooks.json       Stop, PostToolUse, PreCompact, SessionStart and SessionEnd hooks
commands/              /contextrail:handoff, :status, :doctor, :history and :lightning
contextrail/cr.py      entry point for the hooks, the status line and the commands
contextrail/*.py       usage, facts, handoff, storage, config, prompts, status, doctor, history, lightning, statusline
mods/                  contextrail-continue, the optional mod
tests/                 unittest suite with synthetic transcripts
tools/eval/            handoff evaluation on real sessions (development only; uses Claude Code quota)
docs/                  this page and eval.md
```
