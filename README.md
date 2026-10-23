# ContextRail

ContextRail saves a handoff before a Claude Code session gets too long to work well, and loads it into the next session automatically.

Long coding sessions get worse well before the context window is full. The agent loses track of earlier decisions, repeats work, and makes things up. The usual fix is to stop, write notes for the next session, and start fresh. People tend to do it too late, or not at all.

ContextRail is a Claude Code plugin that does it for you:

1. **It notices** when the session reaches the point where quality starts to drop (by default, once it has grown 100k tokens past where it started).
2. **It asks the agent for a handoff** while the agent still has the full picture: the goal, your rules in your own words, what is done and in progress, next steps, decisions, dead ends, facts already found, how to run things, open questions with options, and a short resume prompt.
3. **It saves the handoff** outside your repository, with facts it records itself: branch, commit, uncommitted files, files edited, and test runs.
4. **It loads the handoff** into the next session. That happens on `/clear`, on a new session, or after compaction, together with what changed in the repository since and which of the handoff's claims to re-check.

You type `/clear` when it tells you to. Everything else is automatic.

```
ContextRail: handoff saved to ~/.contextrail/projects/…/HANDOFF.md. Type /clear to continue
in a fresh session; the handoff loads automatically. The resume prompt is on your clipboard.
```

See [`examples/HANDOFF.example.md`](examples/HANDOFF.example.md) for a saved handoff.

## Install

You need Claude Code 2.x, Python 3.9 or newer as `python3` on your `PATH`, and git (optional, used for the facts and drift checks).

In Claude Code:

```
/plugin marketplace add Ansul-S/ContextRail
/plugin install contextrail@contextrail
```

Restart Claude Code. ContextRail is now active in every project.

To try it without installing, clone the repository and start Claude Code with `claude --plugin-dir /path/to/contextrail`.

## Using it

Most of the time you do nothing:

- **At the handoff point** (by default 100k tokens into the session, counted from where it started), when the agent finishes a turn, ContextRail asks it for a checkpoint handoff. The agent replies with it, and ContextRail saves it and tells you. Work goes on in the same session. Claude Code labels the request "Stop hook error occurred", because it reaches the agent the way a Stop hook's objection does. Nothing has failed.
- **If background tasks are still running** (a test run in the background, a subagent), ContextRail waits until they finish, because their results belong in the handoff. It says so once, and stops waiting at the wrap-up point. Then the handoff lists the tasks still running, with how to check their results. Nothing is ever stopped to make room for a handoff.
- **While you keep going**, ContextRail refreshes the checkpoint every 40k tokens.
- **At the wrap-up point** (by default 130k into the session), in the middle of a long turn, the agent is told to finish the step it is on and not start another: run the commands the step still needs, let running commands finish, and show you their results. When the turn ends, ContextRail asks for the final handoff, and `contextrail-continue` (below) offers a new session.
- **Type `/clear`** at any time after a handoff is saved. The new session starts with the handoff loaded. Paste the resume prompt from your clipboard, or just say "continue".
- **Every request says why.** The note ContextRail shows with it gives the handoff and wrap-up points, what set each one (your token settings or a window cap), the window size and where it came from, and any tasks still running.
- **Before compaction**, ContextRail saves the facts and carries over the last narrative, so a compacted session still gets a handoff.
- **When a session ends without a handoff** (you `/clear` or exit before the request), and it edited files and grew by 30k tokens, ContextRail saves a checkpoint the same way, so the next session still knows where you left off. Claude Code gives this about 1.5 seconds and doesn't run it at all after a crash or a killed terminal, so it is a safety net, not a replacement for the handoff.
- **Scripted runs are left alone.** In `claude -p` and the Agent SDK nobody is there to type `/clear`, and a handoff written by a script would replace yours, so ContextRail does nothing there. Set `unattended` to `true` to have it run in them too.

Commands:

| Command | What it does |
| --- | --- |
| `/contextrail:handoff` | Write a handoff now, at any context size. Useful before a break. |
| `/contextrail:status` | Show context usage, trigger points, the saved handoff, and what changed since it was saved. |
| `/contextrail:doctor` | Check the setup: Python, the hooks, storage and settings, when each hook last ran, whether usage can be read, and whether the saved handoff would load. `--setup-statusline` sets up the optional status-line wrapper. |
| `/contextrail:history` | List the saved handoffs, newest first, with each one's first next step. `/contextrail:history --restore N` makes an archived one current again. |
| `/contextrail:lightning` | Turn on Lightning Mode at a context size, for example `/contextrail:lightning 200k`, or turn it off with `off`. See Lightning Mode. |

## One-key continue (optional)

ContextRail leaves `/clear` to you, because hooks can't run slash commands. The optional `contextrail-continue` plugin asks you instead. Once ContextRail has asked for the final handoff at the wrap-up point and saved it, a small pane opens. Checkpoint handoffs before that point get no question, so work goes on undisturbed; set `new_session_at` to `"soft"` to be asked after every handoff instead.

```
ContextRail saved the handoff.
Press y to start a new Claude Code session. Press n to continue using the current session.
y: Start a new session   n: Keep this session
```

- **y** runs `/clear` and sends the handoff's resume prompt, so the work carries on in a fresh session with the handoff loaded.
- **n**, or Esc, keeps the current session. The handoff stays saved, and `/clear` loads it whenever you are ready.

The question waits for your answer, however long that takes. In Lightning Mode it counts down instead (see below).

```
/plugin install contextrail-continue@contextrail
```

It is a mod, a newer kind of Claude Code plugin written in TypeScript. Mods are in early access, so this needs a recent Claude Code; it was tested with 2.1.308. ContextRail itself works without it.

- **Narrow terminals:** Claude Code only shows a pane it didn't ask for in a terminal at least 144 columns wide. In a narrower one, the plugin asks the same question in Claude Code's question dialog.
- **The keys** work while the pane has the keyboard. It takes the keyboard when the prompt box is empty. If you were typing, press ctrl+x tab to reach it.
- **The question is withdrawn** when you send a prompt. You stay in the current session.
- **It waits for background work:** while background or scheduled tasks (such as `/loop`) are still pending, it holds the question until they finish and the agent has shown their results. A new turn, such as a task's notification, takes an open question back until that turn ends.
- **It explains the label:** when ContextRail asks for a handoff, the plugin shows a note that the "Stop hook error" above is ContextRail's request.

| Setting | Default | Meaning |
| --- | --- | --- |
| `after_manual` | off | Also ask after a handoff you asked for with `/contextrail:handoff`. |

The setting is offered when you install the plugin, and is in `/config` afterwards.

## Lightning Mode (optional)

Lightning Mode starts a new session on its own once the context reaches a size you choose:

```
/contextrail:lightning 200k
```

- **At 200k tokens of context**, counted in all rather than from the session's start, ContextRail asks for the handoff as usual.
- **Once it is saved**, `contextrail-continue` counts down 10 seconds in a blue pane, then runs `/clear` and sends the resume prompt.
- **To stay**, press n or Esc, or send a prompt. Text in the prompt box when the countdown ends also keeps the session. y starts the new session at once.
- **ContextRail's rail turns blue** (see The rail), between two bolts, and ends where the new session starts:

  ```
  ⚡━━━━━━━━━━━━━━━━━━●┄┄┄┄┄⚡  152k · new session at 200k · 48k to go · 15% of 1M window · Opus 5.5 · my-project
  ```

The limit can be written 200k, 0.2M, 200000 or 200,000, and must be at least 10k. A session that starts near it still gets 20k tokens of work before the handoff. `soft_pct` still caps it, so on a 200k window the handoff comes at 100k at the latest. The wrap-up warning comes 30k after the handoff point, the gap between `hard_tokens` and `soft_tokens`.

`/contextrail:lightning off` turns it off, and `/contextrail:lightning` alone shows the setting. It is saved as `lightning_tokens` in `~/.contextrail/config.json`, so it applies in every project. On a terminal too narrow for the pane, a note says when the new session starts; start typing to stay. Without `contextrail-continue`, the handoff is still saved at the limit, and `/clear` continues from it.

## When it triggers, and why

The two points are yours to set, in `~/.contextrail/config.json`, to suit your model, its window and how you work. The defaults are fallbacks for when you set nothing.

| Setting | Fallback | Meaning |
| --- | --- | --- |
| `soft_tokens` / `soft_pct` | 100,000 / 50% | The handoff point: this many tokens past the session's start, or this share of the window, whichever comes first. Before the wrap-up point, the handoff is a checkpoint and work goes on. |
| `hard_tokens` / `hard_pct` | 130,000 / 65% | The wrap-up point, worked out the same way: the agent is told mid-turn to wrap up, ContextRail asks for the final handoff, and a new session is offered. Also the longest ContextRail waits for background tasks. |
| `new_session_at` | `"hard"` | Where `contextrail-continue` offers a new session: at the wrap-up point, or `"soft"` for after every handoff, as before 0.5.0. Lightning Mode always offers it at its own point. |
| `refresh_tokens` | 40,000 | While you keep working after a handoff, ask again after this much growth. |

How they combine:

```
handoff point = min(start + soft_tokens, window × soft_pct %)
wrap-up point = min(start + hard_tokens, window × hard_pct %), and never before the handoff point
```

- **Your token settings decide** whenever the window is big enough. The shares are a guard for small windows: they can only bring a point forward, never push it back. To loosen a guard, raise `soft_pct` or `hard_pct` (up to 100).
- **Each setting can be set on its own.** If you raise only `soft_tokens` past the fallback `hard_tokens`, the wrap-up point follows it, 30,000 later.
- **Settings are checked.** `soft_tokens` and `hard_tokens` must be above 0, and the shares 1 to 100; an invalid value is ignored. If you set `hard_tokens` yourself and it is not above `soft_tokens`, ContextRail uses `soft_tokens` + 30,000 until you fix it. `/contextrail:doctor` and `/contextrail:status` say so.
- **`/contextrail:status` shows the result**, with what decided each point.

Examples (starting points to try, not guarantees: no setting keeps a model from making mistakes, and where long sessions start to slip depends on the model and the task):

| Your setup | `config.json` | Handoff and wrap-up, with a 50k start |
| --- | --- | --- |
| 200k window, the fallbacks | nothing | 100k and 130k: the shares decide |
| 1M window, long working sessions | `{"soft_tokens": 400000, "hard_tokens": 550000}` | 450k and 600k |
| 1M window, shorter sessions | `{"soft_tokens": 150000, "hard_tokens": 200000}` | 200k and 250k |
| 200k window, more room before the handoff | `{"soft_pct": 60, "hard_pct": 70}` | 120k and 140k |
| Quick tasks, a fresh session at a fixed size | `/contextrail:lightning 150k` | 150k, then a new session (see Lightning Mode) |

On a 1M window, the large settings only work when ContextRail knows the window size (see below). Otherwise it assumes 200k, and the shares of that bring the handoff to 100k.

Why the fallbacks are what they are:

- **Sessions don't start empty.** Claude Code's system prompt and tools, skills, MCP instructions, `CLAUDE.md` files and the loaded handoff often add up to 30-50k tokens before any work. So the token settings count from the context at the session's first reply, or the first one after compaction. Claude Code sometimes sends its skill, agent and MCP listings only after the first reply, which can add about 30k tokens. Then the start is the context at the second reply. With a 50k start, the handoff comes at about 150k and the wrap-up at 180k. `/contextrail:status` shows the start.
- **Usable space is smaller than the window.** Claude Code reserves roughly 16% of the window for auto-compaction. 50% of the raw window is about 60% of the space you can actually use.
- **The agent needs to still be sharp.** Asking at 50% leaves headroom for writing the handoff (3-5k tokens) while the agent is still at its best. Auto-compaction fires around 83%, which is too late for a good summary.
- **Absolute tokens and shares do different jobs.** Answer quality can decline gradually as a session grows, whatever the window size, but where that starts depends on the model and the task, and nobody has a number that holds for all of them. The fallback token settings are cautious; on a 1M window they decide, well before the 50% share (500k). On a 200k window the shares decide instead (100k and 130k), so a session still hands off before auto-compaction, wherever it started.
- **These are starting points, not laws.** Raise them if your sessions hold up longer, lower them if they don't.

Context usage is read from the session transcript, the same way Claude Code computes it for `/context`: the context sent with the latest reply. Hook payloads do not include the window size. ContextRail takes it from `window_tokens` if set, else from the status-line wrapper below, else from a `[1m]` in the model name, else from usage that has already passed 200k. Failing all of those it assumes 200k. Several current models have a 1M window without saying so in their name. With an assumed window, `/contextrail:doctor` warns, and the note with each request and `/contextrail:status` say how far the 50% share moved the handoff, for example "Its 50% cap puts the handoff at 100,000 instead of 450,000". Set up the wrapper or set `window_tokens` to fix it.

### Exact window size (optional)

Claude Code tells the status line the window size, but not the hooks. ContextRail can sit in front of your status line, note the size for its hooks, and pass your status line through unchanged:

```
/contextrail:doctor --setup-statusline
```

This writes a small launcher to `~/.contextrail/bin/statusline` and prints the `"statusLine"` entry to put in `~/.claude/settings.json`, with your current status line already filled in. ContextRail never edits that file; the agent can add the entry for you if you say yes. The launcher finds the plugin after updates, and if ContextRail is removed it runs your status line on its own.

### The rail

If you have no status line, the launcher shows ContextRail's own: a rail that fills up as the session's context grows, so you can see at a glance how far the session is from its next handoff.

```
━━━━━━●┄┄┄┄┄┄┄┄┄┄┄┄┄◆┄┄┄  52k · handoff at 150k · 98k to go · 5% of 1M window · Opus 5.5 · my-project
```

Read it from left to right:

- **The rail** runs from an empty context to the wrap-up point, 130k tokens past the session's start by default. It starts part-filled, because a session never starts empty.
  - `━` is context already used, and `●` is where the session is now.
  - `┄` is the room left, and `◆` is where ContextRail will ask for the next handoff.
  - After a handoff is saved, `◇` marks where it was written, `◆` moves to the next request (40k later by default), and the rail reaches past it.
- **The token count** is the context the session holds now.
- **What happens next:**

  | Text | Meaning |
  | --- | --- |
  | `handoff at 150k · 98k to go` | Nothing to do yet. |
  | `◆ handoff due` | The session has passed the request point. The request goes out when the agent next finishes a turn. |
  | `◷ waiting for background tasks` | The request is due, but tasks are still running, and their results belong in the handoff. |
  | `◆ handoff requested` | The agent has been asked and is writing it. |
  | `✓ saved 3m ago · next at 192k` | A handoff is saved. Type `/clear` to continue from it, or keep working until the next request. |
  | `✗ no handoff written · /contextrail:handoff` | The agent ignored three requests. Ask for one yourself. |
  | `ContextRail off` | It is turned off for this project. |

- **Then** the share of the window in use, the model and the project.

The rail and the token count are green while there is plenty of room. They turn amber as the handoff gets close and red at the wrap-up point. On a narrow terminal, the project, the model and the window share are left out in that order, then the rail gets shorter. `NO_COLOR` turns the colours off.

To use ContextRail's line in place of your own, leave out the ` -- '…'` part of the launcher command.

## Configuration

Settings are applied in this order, later ones winning:

1. built-in defaults
2. `~/.contextrail/config.json`
3. `~/.contextrail/projects/<project>/config.json`
4. environment variables (`CONTEXTRAIL_` plus the key in upper case, for example `CONTEXTRAIL_SOFT_TOKENS=120000`)

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

- `soft_tokens`, `soft_pct`, `hard_tokens`, `hard_pct` and `new_session_at` are explained in When it triggers, and why. `window_tokens` sets the window size when ContextRail can't detect it.
- `max_chars` caps the part the agent writes, at 8,000 characters (about 2k tokens) by default. [docs/eval.md](docs/eval.md) compares it with 6,000 on real sessions. Together with the facts ContextRail adds, that fits Claude Code's 10,000-character limit for what a hook loads into a session. A longer handoff loads shortened, with the least important items left out first (known facts, dead ends, decisions) and a pointer to the full file. `/contextrail:doctor` says when that happens.
- `unattended` lets ContextRail run in sessions nobody is watching, such as `claude -p` and the Agent SDK. It is off by default, so a scripted run neither asks for a handoff nor replaces yours.
- `content_checks` holds the handoff against what ContextRail recorded before saving it: uncommitted files edited in the session that it never mentions, a Done section with no evidence, a first next step with nothing concrete in it, and a Done item that says a check passed when the facts show it failed. A gap gets the one fix request.
- `checkpoint_tokens` is how far a session must grow past its start, having edited files, to get a checkpoint when it ends without a handoff.
- `inject_on` sets which session starts load the handoff (`startup`, `clear`, `compact`, `resume`).
- `lightning_tokens` turns on Lightning Mode at that many tokens of context; 0 is off. It works only in `~/.contextrail/config.json` and `CONTEXTRAIL_LIGHTNING_TOKENS`, so ContextRail and `contextrail-continue` read the same value. `/contextrail:doctor` flags it in a project's `config.json`.
- To turn ContextRail off everywhere, set `CONTEXTRAIL_DISABLE=1`. To turn it off for one project, put `{"enabled": false}` in that project's `config.json`.

## Where files live

All data stays on your machine, under `~/.contextrail/` (or `CONTEXTRAIL_HOME`). Nothing is written to your repository. The plugin makes no network requests and no extra model calls.

```
~/.contextrail/projects/<project-path-slug>/
  HANDOFF.md          current handoff (overwritten each time)
  RESUME_PROMPT.txt   the resume prompt
  history/            previous handoffs, each with its meta (never loaded automatically)
  meta.json           git state and file fingerprints at save time, for the drift check
  state.json          per-session trigger state
  config.json         optional per-project settings
```

`~/.contextrail/` itself also holds `error.log`, for errors in the hooks, `heartbeat.json`, which records when each hook last ran, and `plugin-root`, where the plugin is installed. With the status-line wrapper, it also holds `bin/statusline` and `sessions/`, the window size of each session (kept for 7 days).

A project is the git repository root, or the folder itself if it isn't a repository. Sessions started in subfolders share the project's handoff.

## FAQ

**Why not just rely on auto-compaction?**
Compaction is a generic summary written at the last moment, and you stay in the same long session. A handoff is written earlier, follows a fixed structure, separates verified from unverified work, and keeps decisions and dead ends that summaries tend to drop. The two work together: ContextRail also saves before compaction and reloads afterwards.

**Why doesn't it run `/clear` itself?**
Hooks can't run slash commands, and leaving `/clear` to you means you decide when to switch. If you'd rather switch with one key, install the optional `contextrail-continue` plugin (see One-key continue).

**What if the agent ignores the request?**
ContextRail asks at most three times per session, then tells you to run `/contextrail:handoff` when you are ready. An incomplete handoff, or one that leaves out what the session's facts show (see `content_checks`), gets one request for a fix. After that it is saved anyway, with a warning.

**Does it work alongside other context monitors (such as GSD's)?**
Yes. ContextRail fires earlier than most warnings, so the handoff usually exists by the time they appear. Some monitors tell the agent not to write handoffs unless asked. ContextRail's request says the user set it up, which counts as asking.

**What does "what changed since" cover?**
When a handoff is saved, ContextRail notes the commit and a fingerprint of each uncommitted file. When the next session loads it, ContextRail lists the new commits, which files changed since (committed or not, new, deleted or reverted), and the Done and In progress items that name one of those files, and tells the agent to re-check them and run the Verify first commands. A file that was already uncommitted and hasn't changed doesn't count. The whole list stays under about 1,000 characters.

**How does it know whether a check passed?**
It picks the test, lint or build command out of each shell command, skipping `cd`, variable assignments and heredoc bodies. If the command's exit status belongs to that check, it uses the exit status. If a pipe (`| tail`) or a later command hides it, ContextRail reads the summary line that the tool prints (unittest, pytest, jest, vitest, go, cargo, mypy, ruff and others). If there is no summary line, the check is listed as "ran", not "passed".

**Something seems off.**
Run `/contextrail:doctor`. It lists each check as ok, warn or FAIL, with what to fix. From a terminal, `python3 /path/to/contextrail/contextrail/cr.py doctor` does the same and exits 1 if a check fails. Errors from the hooks go to `~/.contextrail/error.log`, and `/contextrail:status` mentions them when there were any in the last 24 hours.

**Hooks feel slow.**
Each hook is one short Python process, usually under 0.1 s, even with transcripts of 60 MB or more. Version-manager shims such as pyenv can add about 0.15 s per call. `/contextrail:doctor` measures it.

**Windows?**
It should work if `python3` is on your `PATH`. File locking and the clipboard use fallbacks. It is not yet tested in CI. The optional status-line wrapper's launcher is a shell script, so it works on macOS and Linux only.

**Will it overwrite my notes?**
The previous `HANDOFF.md` moves to `history/` each time a new one is saved. The last 20 are kept. `/contextrail:history` lists them, and `--restore N` brings one back; the one it replaces moves to `history/` in turn.

## Development

```
python3 -m unittest discover -s tests -t .
```

Everything is standard-library Python 3.9+, with no dependencies. To try changes in a real session, run `claude --plugin-dir .`. See [CONTRIBUTING.md](CONTRIBUTING.md).

```
.claude-plugin/        plugin and marketplace manifests
hooks/hooks.json       Stop, PostToolUse, PreCompact, SessionStart and SessionEnd hooks
commands/              /contextrail:handoff, :status, :doctor, :history and :lightning
contextrail/cr.py      entry point for hooks, the status line and the status, doctor, history and lightning commands
contextrail/*.py       usage, facts, handoff, storage, config, prompts, status, doctor, history, lightning and statusline
mods/                  contextrail-continue, the optional mod that offers a new session after the final handoff
tests/                 unittest suite with synthetic transcripts
tools/eval/            handoff evaluation on real sessions (development only; uses Claude Code quota)
docs/eval.md           how the evaluation works, and its latest numbers
```

## License

MIT
