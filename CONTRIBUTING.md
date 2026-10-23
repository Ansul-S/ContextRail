# Contributing

Thanks for helping. Bug reports with a short description of what happened are just as welcome as pull requests. A copy of `~/.contextrail/error.log` helps too, if there is one.

## Ground rules

- **Standard library only, Python 3.9+.** The plugin runs on whatever `python3` is on the user's machine, including macOS's built-in one.
- **Hooks never break a session.** Every handler returns quietly on bad input. Unexpected errors go to `~/.contextrail/error.log`, never to Claude Code.
- **Keep the handoff small.** Anything loaded at session start costs context in every future session.
- **Facts come from scripts, judgment from the agent.** Git state, edited files and test runs are recorded by ContextRail, never written by the model.
- **Nothing is written to the user's repository.**

## Running the tests

```
python3 -m unittest discover -s tests -t .
```

The tests build synthetic transcripts and throwaway git repositories, and set `CONTEXTRAIL_HOME` to a temporary folder.

## Trying a change in Claude Code

```
CONTEXTRAIL_HOME=/tmp/cr-home CONTEXTRAIL_SOFT_TOKENS=1000 claude --plugin-dir /path/to/contextrail
```

A low `CONTEXTRAIL_SOFT_TOKENS` makes the handoff request fire on the first turn. Use `/contextrail:status` to see what ContextRail sees, and `/contextrail:doctor` to check the setup.

To try Lightning Mode, set `CONTEXTRAIL_LIGHTNING_TOKENS=60000` instead. The handoff comes at 60k tokens of context, or 20k past the session's start if that is later, and an installed `contextrail-continue` counts down to a new session. The variable overrides `lightning_tokens` in `config.json`, so your own setting stays as it is.

ContextRail does nothing in sessions nobody is watching, so a scripted end-to-end run with `claude -p` also needs `CONTEXTRAIL_UNATTENDED=1`, and `CONTEXTRAIL_COPY_RESUME_PROMPT=0` to leave your clipboard alone.

## The mod

`mods/contextrail-continue/` is an optional plugin of a newer kind, a TypeScript hooks module (a mod), that asks whether to start a new session after a handoff, and clears and continues on yes. It recognises ContextRail's handoff request by its opening words, and a checkpoint request by `prompts.CHECKPOINT`, after which it offers no new session; Python tests keep both in step. For Lightning Mode it reads `lightning_tokens` itself, the way `contextrail/config.py` does: the environment variable when it holds a usable value, else `config.json` under `CONTEXTRAIL_HOME`. A change to how `config.py` reads numbers needs the same change in `tokensOf` in `register.tsx`, and the mod's tests list the values both must agree on. Check it with:

```
claude plugin validate mods/contextrail-continue
claude plugin test mods/contextrail-continue
```

## Changing the prompt

`contextrail/prompts.py` holds the text sent to the agent. `commands/handoff.md` repeats the template and rules, and a test checks that the two match.

## Measuring handoffs

`tools/eval/` regenerates real handoffs at different `max_chars` caps and judges them against what the next session needed; [docs/eval.md](docs/eval.md) explains the method. It calls `claude -p`, so a run uses your Claude Code quota (the 0.5.0 run, 36 calls, was about $41 as Claude Code reports cost). Start with `python3 -m tools.eval cases`, then `run --dry-run`. Its tests, in `tests/test_eval.py`, use a fake `claude` and call no model.
