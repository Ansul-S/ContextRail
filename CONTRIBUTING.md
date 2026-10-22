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

## Changing the prompt

`contextrail/prompts.py` holds the text sent to the agent. `commands/handoff.md` repeats the template and rules, and a test checks that the two match.
