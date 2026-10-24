# Compatibility

## Claude Code

| | Oldest supported | Why | Tested in CI |
|---|---|---|---|
| ContextRail | 2.1.163 | Stop feedback (`hookSpecificOutput.additionalContext`) arrived in 2.1.163, so a request isn't labelled "Stop hook error". The Stop payload's `background_tasks` and `session_crons` arrived in 2.1.145. | 2.1.163 and the latest: both plugins' manifests and hooks validated |
| contextrail-continue | 2.1.286 | It is a hooks module. The validator accepts it from 2.1.275, and from 2.1.286 hooks modules are on without `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1`. | 2.1.286 and the latest: validated, and its 50 tests run with `claude plugin test` |

Older versions: ContextRail reads the Claude Code version from the session's transcript. Below 2.1.163 it sends requests as a blocked stop, which Claude Code shows as "Stop hook error occurred" even though nothing failed, and `/contextrail:doctor` warns. Set `request_style` to `block` or `feedback` to choose for yourself.

How these were found (24 October 2026): each version was installed from npm into a scratch folder and run with an empty home, with `claude plugin validate` on both plugins and `claude plugin test` on the companion. Claude Code 2.1.274 rejects the companion ("session.end is not an event"), and 2.1.280 to 2.1.285 run its tests only with that variable set.

Real sessions: `tools/integration.py` runs two `claude -p` sessions. In the first, ContextRail asks for a handoff and saves it; in the second, the agent answers from the handoff it loaded. It passed on 2.1.310 (Haiku, 24 October 2026). The request arrived as hook feedback, and Claude Code reported no hook error. It calls the model, so CI runs it only when started by hand (`.github/workflows/integration.yml`, with an `ANTHROPIC_API_KEY` secret).

## Platforms

| | macOS | Linux | Windows |
|---|---|---|---|
| Hooks, handoffs, drift check | Supported | Supported | Not supported yet |
| Private files (0700 / 0600) | Yes | Yes | Profile ACLs only |
| Locking | `fcntl` | `fcntl` | `msvcrt` |
| Clipboard copy of the resume prompt | `pbcopy` | `wl-copy`, `xclip` or `xsel` | `clip` |
| Status-line launcher | Yes | Yes | No (a shell script) |
| Python test suite in CI | 3.9 and 3.13 | 3.9 and 3.13 | 3.9 and 3.13, allowed to fail |
| Real Claude Code sessions | Tested: interactive end-to-end runs and `tools/integration.py` | Not yet: the manual integration workflow runs on Linux | Not tested |

Windows: the hooks run `python3`, which a standard Windows install doesn't provide, and nothing has been tried in a real Claude Code session there. CI runs the suite on Windows so problems show up, but a failure there doesn't fail the build.

The `contextrail-continue` y/n pane and the Lightning countdown were tested in Claude Code's interactive terminal UI on macOS, driven through a pseudo-terminal by an end-to-end runner, and in everyday use. The desktop app, the IDE extensions, Linux terminals and other terminal emulators have not been tested.

## Python

Python 3.9 or newer, as `python3` on the `PATH` that Claude Code's hooks see. Only the standard library is used. CI runs 3.9 and 3.13; the suite also passes on 3.11 and 3.14 on macOS.

## Which features need contextrail-continue

| Feature | ContextRail alone | With contextrail-continue |
|---|---|---|
| Handoff requests, saving, history, drift check, loading on startup, `/clear` and compaction | Yes | Yes |
| Status line, doctor, status, purge | Yes | Yes |
| The y/n offer to start a new session after the final handoff | No: type `/clear` yourself | Yes |
| Lightning Mode's automatic new session | No: Lightning sets the point, you `/clear` | Yes, after a 10 s countdown |
| A note that a "Stop hook error" is ContextRail's request (with `request_style` set to `block`) | No | Yes |
