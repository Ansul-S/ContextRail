# ContextRail

ContextRail is a Claude Code plugin that saves a handoff before a long session stops working well, and loads it into the next session for you.

## The problem

Long coding sessions get worse well before the context window is full. The agent loses track of earlier decisions, repeats work, and makes things up. The usual fix is to stop, write notes for the next session and start fresh, but people tend to do that too late, or not at all.

## What ContextRail does

1. **It watches the context** of every session, and knows the points you set for a handoff.
2. **It asks the agent for a handoff** at that point, while the agent still has the full picture: the goal, your rules in your own words, what is done and in progress, next steps, decisions, dead ends, how to run things, open questions, and a short resume prompt.
3. **It saves the handoff** outside your repository, with facts it records itself: branch, commit, uncommitted files, files edited and test runs.
4. **It loads the handoff** into the next session, on `/clear`, a new session or after compaction, together with what changed in the repository since.

```
ContextRail: handoff saved to ~/.contextrail/projects/…/HANDOFF.md. Type /clear to continue
in a fresh session; the handoff loads automatically.
```

[`examples/HANDOFF.example.md`](examples/HANDOFF.example.md) shows a saved handoff.

## Features

- **Your own limits:** set the handoff point and the wrap-up point in tokens. Sensible fallbacks apply until you do.
- **Checkpoints, then a final handoff:** the first handoff is a checkpoint, and work goes on. At the wrap-up point the agent finishes its current step, writes the final handoff, and a new session is offered.
- **One key to continue** (optional `contextrail-continue` plugin): press y to start the new session from the handoff, or n to stay.
- **Lightning Mode** (optional): a new session starts by itself at a context size you choose.
- **Running work is safe:** nothing is ever stopped for a handoff. ContextRail waits for background tasks, and a new session is never started while they run.
- **Your decisions come first:** when the agent asks you a question, the handoff request waits for your answer until the wrap-up point, and the y/n question and the countdown never appear over it.
- **A status line** that shows how full the session is and when the next handoff comes.
- **History:** older handoffs are kept, and any of them can be restored.

## Normal mode and Lightning Mode

|  | Normal mode (the default) | Lightning Mode |
| --- | --- | --- |
| Turn it on | Nothing to do | `/contextrail:lightning 200k` |
| When the handoff comes | At your handoff point, counted from where the session started | When the context reaches the size you chose |
| After the handoff | A checkpoint: keep working. At the wrap-up point comes the final handoff, and `contextrail-continue` asks y/n | `contextrail-continue` counts down 10 seconds, then starts a new session from the handoff |
| To stay in the session | Press n or Esc, or send a prompt | Press n or Esc, or start typing |
| Turn it off | — | `/contextrail:lightning off` |

Both modes save the same handoff, and both wait while background tasks run or the agent waits for your answer.

## Install

You need Claude Code 2.x, Python 3.9 or newer as `python3` on your `PATH`, and git (optional, used for the facts and the "what changed since" check).

In Claude Code:

```
/plugin marketplace add Ansul-S/ContextRail
/plugin install contextrail@contextrail
```

For the one-key continue and Lightning Mode, also install the companion plugin:

```
/plugin install contextrail-continue@contextrail
```

Then restart Claude Code. ContextRail is now active in every project. `contextrail-continue` is a mod, a newer kind of plugin, so it needs a recent Claude Code (tested with 2.1.308 and 2.1.309). ContextRail works without it: you type `/clear` yourself.

**Nothing else to install.** ContextRail uses only Python's standard library, so there are no packages and no virtual environment, for running it or for its tests. If your `python3` comes from pyenv or another version manager, that works too; `/contextrail:doctor` shows which Python the hooks use and how fast it starts.

**Optional, recommended:** let ContextRail read the exact window size from Claude Code's status line:

```
/contextrail:doctor --setup-statusline
```

It writes a small launcher and prints the `"statusLine"` entry to put in `~/.claude/settings.json` (it never edits that file itself). Without it, ContextRail assumes a 200k window unless it can tell otherwise.

To try ContextRail without installing it, clone this repository and start Claude Code with `claude --plugin-dir /path/to/ContextRail`.

## Using it

Most of the time you do nothing. A typical session:

1. You work as usual. The status line shows the context filling up: `52k · handoff at 150k · 98k to go`.
2. At the handoff point, when the agent finishes a turn, ContextRail asks it for a checkpoint handoff. The agent writes it, ContextRail saves it and says so, and you keep working.
3. At the wrap-up point, the agent is told to finish the step it is on, and then writes the final handoff.
4. `contextrail-continue` asks: **y** starts a new session with the handoff loaded and its resume prompt sent, **n** keeps this one. Without the plugin, type `/clear` when you are ready.

Commands:

| Command | What it does |
| --- | --- |
| `/contextrail:status` | Context used, the handoff and wrap-up points and why they are there, and the saved handoff |
| `/contextrail:handoff` | Write a handoff now, at any size. Useful before a break |
| `/contextrail:lightning 200k` | Turn Lightning Mode on at that size; `off` turns it off, no argument shows it |
| `/contextrail:history` | List saved handoffs; `--restore N` makes an older one current again |
| `/contextrail:doctor` | Check the setup: Python, hooks, storage, settings, and whether the handoff would load |

## Decisions, questions and handoffs

ContextRail never answers or hides anything in Claude Code's own interface. Claude Code's question picker (the multiple-choice list the agent shows with its AskUserQuestion tool) and its permission prompts work exactly as without ContextRail.

- **A question to you comes first.** If the agent ends its turn with a question, ContextRail doesn't ask for a handoff at that stop. It shows `the handoff request waits until you have answered the question above` once, and asks at the stop after your answer. From the wrap-up point on it asks anyway; the question then goes into the handoff's Open questions, and the new session picks it up.
- **The y/n question and the Lightning countdown wait too.** They never appear while the agent waits for your answer or while background tasks run.
- **Background work is never stopped.** Below the wrap-up point, ContextRail waits for running tasks before it asks, because their results belong in the handoff. If the final handoff had to be written while tasks were still running, ContextRail asks for it again once they finish, so the next session starts with their results. Only then is a new session offered.
- **The y/n pane** takes the keyboard when the prompt box is empty. If you were typing, press ctrl+x tab to reach it. In a terminal narrower than 144 columns, the same question comes as a dialog.
- **"Stop hook error occurred"** is how Claude Code labels ContextRail's request to the agent. Nothing failed. Press ctrl+o to read the request, or run `/contextrail:status`.

## Settings

Settings go in `~/.contextrail/config.json`. Create the file if it doesn't exist. For example, for long sessions on a 1M-token window:

```json
{
  "soft_tokens": 400000,
  "hard_tokens": 550000
}
```

| Setting | Fallback | Meaning |
| --- | --- | --- |
| `soft_tokens` | 100,000 | The handoff point: this many tokens past where the session started |
| `hard_tokens` | 130,000 | The wrap-up point: the final handoff and the offer of a new session. Must be above `soft_tokens` |
| `soft_pct` / `hard_pct` | 50 / 65 | Caps as a share of the window, so small windows still hand off before auto-compaction |
| `new_session_at` | `"hard"` | Where a new session is offered: `"hard"`, or `"soft"` for after every handoff |
| `lightning_tokens` | 0 (off) | Lightning Mode's size; set it with `/contextrail:lightning` |
| `window_tokens` | detected | The window size, if ContextRail can't detect it |
| `refresh_tokens` | 40,000 | While you keep working after a handoff, ask again after this much growth |

The fallbacks are cautious starting points for people who set nothing; no setting guarantees that a model keeps working well. A session never starts empty (Claude Code's own instructions and tools often take 30-50k tokens), which is why the token points count from where the session started. `/contextrail:status` shows the points you get and what set them.

A project can have its own `~/.contextrail/projects/<project>/config.json`, and any setting can be set for one run with an environment variable, for example `CONTEXTRAIL_SOFT_TOKENS=120000`. `CONTEXTRAIL_DISABLE=1` turns ContextRail off. All settings, and how the points are worked out, are in [docs/how-it-works.md](docs/how-it-works.md).

## Running the tests

From the repository root:

```
python3 -m unittest discover -s tests -t .
```

The suite builds synthetic transcripts and throwaway git repositories in temporary folders, and never touches your real `~/.contextrail`. For the companion plugin:

```
claude plugin test mods/contextrail-continue
claude plugin validate .
claude plugin validate mods/contextrail-continue
```

[CONTRIBUTING.md](CONTRIBUTING.md) explains how to try a change in a real session.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| "Stop hook error occurred" | That is ContextRail's handoff request. Nothing failed; ctrl+o shows it. |
| No handoff request, though the session is long | Run `/contextrail:status`. If it says the window size is assumed, run `/contextrail:doctor --setup-statusline` or set `window_tokens`. The request also waits while background tasks run or the agent waits for your answer. |
| The handoff came earlier than your setting | On a small or assumed window, `soft_pct` caps it. `/contextrail:status` says which rule decided. |
| No y/n question, or no Lightning countdown | Check that `contextrail-continue` is installed and enabled, and see the previous rows: it waits for running tasks and for your answer. |
| A change or update seems ignored | Restart Claude Code fully. `/clear` doesn't reload plugin hooks. |
| Anything else | Run `/contextrail:doctor`. Hook errors go to `~/.contextrail/error.log`. |

## Where your data lives

Everything stays on your machine, under `~/.contextrail/` (or `CONTEXTRAIL_HOME`, which must be an absolute path). Nothing is written to your repository, and ContextRail makes no network requests and no extra model calls. Each project has its own folder with the current `HANDOFF.md`, its resume prompt, older handoffs in `history/`, and the session state.

## Learn more

- [docs/how-it-works.md](docs/how-it-works.md): triggers, settings, the status line, Lightning Mode and the companion plugin in detail, and an FAQ.
- [docs/eval.md](docs/eval.md): how handoffs were measured on real sessions.
- [CHANGELOG.md](CHANGELOG.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT
