# ContextRail

ContextRail saves a handoff before a Claude Code session gets too long to work well, and loads it into the next session automatically.

Long coding sessions get worse well before the context window is full. The agent loses track of earlier decisions, repeats work, and makes things up. The usual fix is to stop, write notes for the next session, and start fresh. People tend to do it too late, or not at all.

ContextRail is a Claude Code plugin that does it for you:

1. **It notices** when the session reaches the point where quality starts to drop (100k tokens by default).
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

- **At 100k tokens**, when the agent finishes a turn, ContextRail asks it for a handoff. The agent replies with it, and ContextRail saves it and tells you.
- **If background tasks are still running** (a test run in the background, a subagent), ContextRail waits until they finish, because their results belong in the handoff. It says so once, and stops waiting at 130k.
- **Type `/clear`.** The new session starts with the handoff loaded. Paste the resume prompt from your clipboard, or just say "continue".
- **If you keep going instead**, ContextRail asks for a fresh handoff every 40k tokens.
- **At 130k tokens in the middle of a long turn**, the agent is told to finish the current step and stop starting new work.
- **Before compaction**, ContextRail saves the facts and carries over the last narrative, so a compacted session still gets a handoff.
- **When a session ends without a handoff** (you `/clear` or exit before 100k), and it edited files and reached 30k tokens, ContextRail saves a checkpoint the same way, so the next session still knows where you left off. Claude Code gives this about 1.5 seconds and doesn't run it at all after a crash or a killed terminal, so it is a safety net, not a replacement for the handoff.

Commands:

| Command | What it does |
| --- | --- |
| `/contextrail:handoff` | Write a handoff now, at any context size. Useful before a break. |
| `/contextrail:status` | Show context usage, trigger points, the saved handoff, and what changed since it was saved. |
| `/contextrail:doctor` | Check the setup: Python, the hooks, storage and settings, when each hook last ran, whether usage can be read, and whether the saved handoff would load. `--setup-statusline` sets up the optional status-line wrapper. |
| `/contextrail:history` | List the saved handoffs, newest first, with each one's first next step. `/contextrail:history --restore N` makes an archived one current again. |

## Hands-free continue (optional)

ContextRail leaves `/clear` to you, because hooks can't run slash commands. The optional `contextrail-continue` plugin does it for you: once ContextRail has asked for a handoff and saved it, the plugin waits 10 seconds, runs `/clear`, and sends the handoff's resume prompt, so the work carries on in a fresh session with the handoff loaded.

```
/plugin install contextrail-continue@contextrail
```

It is a mod, a newer kind of Claude Code plugin written in TypeScript. Mods are in early access, so this needs a recent Claude Code; it was tested with 2.1.308. ContextRail itself works without it.

It stays in the current session instead when:

- you start typing during the 10 seconds, or send a prompt;
- background tasks or scheduled tasks (such as `/loop`) are still pending;
- it has already continued 3 times in a row without a prompt from you.

| Setting | Default | Meaning |
| --- | --- | --- |
| `delay_seconds` | 10 | How long to wait after the handoff is saved. |
| `max_in_a_row` | 3 | How many times to continue without a prompt from you in between. |
| `after_manual` | off | Also continue after a handoff you asked for with `/contextrail:handoff`. |

The settings are offered when you install it, and are in `/config` afterwards.

## When it triggers, and why

| Setting | Default | Meaning |
| --- | --- | --- |
| `soft_tokens` / `soft_pct` | 100,000 / 50% | Ask for a handoff at whichever comes first. |
| `hard_tokens` / `hard_pct` | 130,000 / 65% | Tell the agent mid-turn to wrap up. Also the longest ContextRail waits for background tasks. |
| `refresh_tokens` | 40,000 | If you keep working after a handoff, ask again after this much growth. |

Why these numbers:

- **Usable space is smaller than the window.** Claude Code reserves roughly 16% of the window for auto-compaction. 50% of the raw window is about 60% of the space you can actually use.
- **The agent needs to still be sharp.** Asking at 50% leaves headroom for writing the handoff (3-5k tokens) while the agent is still at its best. Auto-compaction fires around 83%, which is too late for a good summary.
- **Absolute tokens matter more than percentages.** Quality declines gradually with length, whatever the window size. On a 1M-token window, 50% would mean 500k tokens, far past the point where long sessions get unreliable. That is why the token cap usually decides.
- **These are starting points, not laws.** Raise them if your sessions hold up longer.

Context usage is read from the session transcript, the same way Claude Code computes it. Hook payloads do not include the window size. Without the status-line wrapper below, ContextRail assumes 200k unless the model name says `[1m]` or usage has already passed 200k. Several current models have a 1M window without saying so in their name, and then the percentages ContextRail shows are too high. The token caps still apply, so the default triggers are unchanged. But if you raise the caps on such a model, set up the wrapper or set `window_tokens`.

### Exact window size (optional)

Claude Code tells the status line the window size, but not the hooks. ContextRail can sit in front of your status line, note the size for its hooks, and pass your status line through unchanged:

```
/contextrail:doctor --setup-statusline
```

This writes a small launcher to `~/.contextrail/bin/statusline` and prints the `"statusLine"` entry to put in `~/.claude/settings.json`, with your current status line already filled in. ContextRail never edits that file; the agent can add the entry for you if you say yes. If you have no status line, ContextRail shows its own: `ctx 52% · 104k/200k · handoff at 100k`. The launcher finds the plugin after updates, and if ContextRail is removed it runs your status line on its own.

## Configuration

Settings are applied in this order, later ones winning:

1. built-in defaults
2. `~/.contextrail/config.json`
3. `~/.contextrail/projects/<project>/config.json`
4. environment variables (`CONTEXTRAIL_` plus the key in upper case, for example `CONTEXTRAIL_SOFT_TOKENS=120000`)

```json
{
  "soft_tokens": 100000,
  "hard_tokens": 130000,
  "window_tokens": null,
  "refresh_tokens": 40000,
  "max_chars": 12000,
  "content_checks": true,
  "checkpoint_tokens": 30000,
  "max_attempts": 3,
  "inject_on": ["startup", "clear", "compact"],
  "max_age_days": 30,
  "copy_resume_prompt": true,
  "history_keep": 20,
  "enabled": true
}
```

- `max_chars` caps the part the agent writes, which keeps the handoff to about 3k tokens when it is loaded.
- `content_checks` holds the handoff against what ContextRail recorded before saving it: uncommitted files edited in the session that it never mentions, a Done section with no evidence, a first next step with nothing concrete in it, and a Done item that says a check passed when the facts show it failed. A gap gets the one fix request.
- `checkpoint_tokens` is the context size a session must reach, having edited files, to get a checkpoint when it ends without a handoff.
- `inject_on` sets which session starts load the handoff (`startup`, `clear`, `compact`, `resume`).
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
Hooks can't run slash commands, and leaving `/clear` to you means you decide when to switch. If you'd rather it switched by itself, install the optional `contextrail-continue` plugin (see Hands-free continue).

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
commands/              /contextrail:handoff, :status, :doctor and :history
contextrail/cr.py      entry point for hooks, the status line and the status, doctor and history commands
contextrail/*.py       usage, facts, handoff, storage, config, prompts, status, doctor, history and statusline
mods/                  contextrail-continue, the optional mod that clears and continues
tests/                 unittest suite with synthetic transcripts
```

## License

MIT
