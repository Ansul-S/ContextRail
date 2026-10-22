# ContextRail

ContextRail saves a handoff before a Claude Code session gets too long to work well, and loads it into the next session automatically.

Long coding sessions get worse well before the context window is full. The agent loses track of earlier decisions, repeats work, and makes things up. The usual fix is to stop, write notes for the next session, and start fresh. People tend to do it too late, or not at all.

ContextRail is a Claude Code plugin that does it for you:

1. **It notices** when the session reaches the point where quality starts to drop (100k tokens by default).
2. **It asks the agent for a handoff** while the agent still has the full picture: goal, done, in progress, next steps, decisions, dead ends, and a short resume prompt.
3. **It saves the handoff** outside your repository, with facts it records itself: branch, commit, uncommitted files, files edited, and test runs.
4. **It loads the handoff** into the next session. That happens on `/clear`, on a new session, or after compaction, together with a list of what changed in the repository since.

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
- **Type `/clear`.** The new session starts with the handoff loaded. Paste the resume prompt from your clipboard, or just say "continue".
- **If you keep going instead**, ContextRail asks for a fresh handoff every 40k tokens.
- **At 130k tokens in the middle of a long turn**, the agent is told to finish the current step and stop starting new work.
- **Before compaction**, ContextRail saves the facts and carries over the last narrative, so a compacted session still gets a handoff.

Commands:

| Command | What it does |
| --- | --- |
| `/contextrail:handoff` | Write a handoff now, at any context size. Useful before a break. |
| `/contextrail:status` | Show context usage, trigger points, the saved handoff, and what changed since it was saved. |

## When it triggers, and why

| Setting | Default | Meaning |
| --- | --- | --- |
| `soft_tokens` / `soft_pct` | 100,000 / 50% | Ask for a handoff at whichever comes first. |
| `hard_tokens` / `hard_pct` | 130,000 / 65% | Tell the agent mid-turn to wrap up. |
| `refresh_tokens` | 40,000 | If you keep working after a handoff, ask again after this much growth. |

Why these numbers:

- **Usable space is smaller than the window.** Claude Code reserves roughly 16% of the window for auto-compaction. 50% of the raw window is about 60% of the space you can actually use.
- **The agent needs to still be sharp.** Asking at 50% leaves headroom for writing the handoff (3-5k tokens) while the agent is still at its best. Auto-compaction fires around 83%, which is too late for a good summary.
- **Absolute tokens matter more than percentages.** Quality declines gradually with length, whatever the window size. On a 1M-token window, 50% would mean 500k tokens, far past the point where long sessions get unreliable. That is why the token cap usually decides.
- **These are starting points, not laws.** Raise them if your sessions hold up longer.

Context usage is read from the session transcript, the same way Claude Code computes it. Hook payloads do not include the window size. ContextRail assumes 200k unless the model name says `[1m]` or usage has already passed 200k. If you raise the caps on a 1M-window model, also set `window_tokens`.

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
  "max_chars": 8000,
  "max_attempts": 3,
  "inject_on": ["startup", "clear", "compact"],
  "max_age_days": 30,
  "copy_resume_prompt": true,
  "history_keep": 20,
  "enabled": true
}
```

- `max_chars` caps the part the agent writes, which keeps the handoff to about 2-3k tokens when it is loaded.
- `inject_on` sets which session starts load the handoff (`startup`, `clear`, `compact`, `resume`).
- To turn ContextRail off everywhere, set `CONTEXTRAIL_DISABLE=1`. To turn it off for one project, put `{"enabled": false}` in that project's `config.json`.

## Where files live

All data stays on your machine, under `~/.contextrail/` (or `CONTEXTRAIL_HOME`). Nothing is written to your repository. The plugin makes no network requests and no extra model calls.

```
~/.contextrail/projects/<project-path-slug>/
  HANDOFF.md          current handoff (overwritten each time)
  RESUME_PROMPT.txt   the resume prompt
  history/            previous handoffs (never loaded automatically)
  meta.json           git state at save time, used for drift checks
  state.json          per-session trigger state
  config.json         optional per-project settings
```

A project is the git repository root, or the folder itself if it isn't a repository. Sessions started in subfolders share the project's handoff.

## FAQ

**Why not just rely on auto-compaction?**
Compaction is a generic summary written at the last moment, and you stay in the same long session. A handoff is written earlier, follows a fixed structure, separates verified from unverified work, and keeps decisions and dead ends that summaries tend to drop. The two work together: ContextRail also saves before compaction and reloads afterwards.

**Why doesn't it run `/clear` itself?**
Hooks can't run slash commands. Leaving `/clear` to you also means you decide when to switch.

**What if the agent ignores the request?**
ContextRail asks at most three times per session, then tells you to run `/contextrail:handoff` when you are ready. An incomplete handoff gets one request for a fix. After that it is saved anyway, with a warning.

**Does it work alongside other context monitors (such as GSD's)?**
Yes. ContextRail fires earlier than most warnings, so the handoff usually exists by the time they appear. Some monitors tell the agent not to write handoffs unless asked. ContextRail's request says the user set it up, which counts as asking.

**Hooks feel slow.**
Each hook is one short Python process, usually under 0.1 s, even with transcripts of 60 MB or more. Version-manager shims such as pyenv can add about 0.1 s per call.

**Windows?**
It should work if `python3` is on your `PATH`. File locking and the clipboard use fallbacks. It is not yet tested in CI.

**Will it overwrite my notes?**
The previous `HANDOFF.md` moves to `history/` each time a new one is saved. The last 20 are kept.

## Development

```
python3 -m unittest discover -s tests -t .
```

Everything is standard-library Python 3.9+, with no dependencies. To try changes in a real session, run `claude --plugin-dir .`. See [CONTRIBUTING.md](CONTRIBUTING.md).

```
.claude-plugin/        plugin and marketplace manifests
hooks/hooks.json       Stop, PostToolUse, PreCompact and SessionStart hooks
commands/              /contextrail:handoff and /contextrail:status
contextrail/cr.py      entry point for hooks and the status command
contextrail/*.py       usage, facts, handoff, storage, config and prompts
tests/                 unittest suite with synthetic transcripts
```

## License

MIT
