# Security and privacy

## Reporting a problem

Report a vulnerability privately through GitHub: the repository's **Security** tab, **Report a vulnerability**. If that isn't available, open an issue that asks for a private contact, without the details. Expect a first answer within a week.

## What ContextRail reads

- **The session's transcript**, at the path Claude Code passes to each hook: context size, the model, the files the agent edited, and the test, lint and build commands it ran with their results. The status line, if you turned it on, reads the payload Claude Code gives it.
- **Git metadata of the project**, through local git commands (`rev-parse`, `status`, `diff`, `log`, `ls-files`, `ls-tree`, `hash-object`, `cat-file`): branch, commit, uncommitted files, renames. It never fetches, pulls or pushes.
- **The contents of uncommitted files and of files the session edited**, to hash them (SHA-1; a file over 8 MB is compared by size and time instead), so the next session can tell which ones changed since the handoff. The contents are not stored.
- **Claude Code's other transcripts** under `~/.claude/projects/`, only to work out which project an old ContextRail 0.6 folder belongs to, and in the doctor and status commands.
- Its own settings in `~/.contextrail/config.json` and the project's `config.json`.

## What it writes

Everything goes under `~/.contextrail/` (or `CONTEXTRAIL_HOME`), never into a repository:

| Path | What it holds | Kept |
|---|---|---|
| `projects/<name>-<hash>/HANDOFF.md` | The current handoff: the agent's narrative and the recorded facts | Until replaced |
| `projects/<name>-<hash>/history/` | Earlier handoffs and their meta | The newest `history_keep` (20) |
| `projects/<name>-<hash>/meta.json`, `RESUME_PROMPT.txt` | Git state, file hashes, checks, the resume prompt | With the handoff |
| `projects/<name>-<hash>/state.json` | Per-session counters: when it asked, what it saved | 14 days, at most 50 sessions |
| `projects/<name>-<hash>/project.json` | Which project the folder belongs to | With the folder |
| `sessions/*.json` | Status-line reports: window size and context per session | 7 days |
| `error.log` | Tracebacks of hooks that failed | Restarted past 256 KB; deleted after 30 days without errors |
| `heartbeat.json`, `plugin-root`, `bin/statusline` | When each hook last ran; where the plugin is; the status-line launcher | Small, kept |
| `eval/` | Only if you run the evaluation tool: session content and verdicts | Until deleted |

A handoff can hold anything from the session: paths, code, your words, output of commands. The handoff prompt tells the agent to leave out secrets, tokens and credentials, but that is a request to a model, not a filter. Treat the folder like your shell history.

ContextRail also copies the resume prompt to the clipboard when it saves a handoff (`copy_resume_prompt`, on by default), where other programs can read it.

## Network

ContextRail opens no network connection and calls no model of its own. The handoff is written by the agent in your session, in the turn ContextRail asks for it. A test (`tests/test_lifecycle.py`, `NoNetworkTest`) runs every hook and command with Python's sockets made to fail. The only programs it starts are `git`, `python3` (the doctor's start-up check) and the clipboard tool.

## File permissions

On macOS and Linux, folders are created `0700` and files `0600`, so other users of the machine can't read them; the status-line launcher is `0700`. Each write goes to a new private temporary file that then replaces the target. At each session start, data written by ContextRail 0.6 or earlier, which could be readable by others, is made private, and `/contextrail:doctor` reports anything that still is. Symlinks inside the folder are left alone. On Windows, ContextRail sets no permissions: the folder is as private as your user profile's ACLs make it.

## Threat model

| Threat | What ContextRail does | What it doesn't |
|---|---|---|
| Other users on a shared machine read handoffs | Private folders and files, fixed for older installs, checked by the doctor | Protect against an administrator, or against backups and sync tools that copy the folder |
| Two projects share data | Each project's folder is named by a SHA-256 of its canonical path, carries `project.json`, and is never read for another path. Each git worktree is its own project. Archives saved for another project are not restored | — |
| Concurrent sessions corrupt state | A per-project lock (`fcntl`, `msvcrt`, or an exclusive file) around every state change, atomic writes | — |
| A crash or a killed hook loses the last good handoff | The previous handoff is archived before the new one replaces it; a handoff without matching meta is still loaded, with a warning | — |
| Text in a repository, a log or a tool result poses as instructions in the next session | The loaded handoff is wrapped and labelled as a record, not instructions; any copy of the wrapper's tags inside it is defused; text from files and tools is called data; earlier approvals of risky actions are flagged as covering their moment only | Guarantee the model follows those labels. A handoff is model-written text and can carry injected content. Review what the agent proposes, as in any session |
| An old approval is taken as permission for a new action | The prompt tells the agent to record approvals as what they covered and when; the loaded handoff says nothing in it is the user's approval, and lists recorded approvals for destructive or outside actions | — |
| A stale claim is trusted | Drift check: commits, branch changes, rewritten history, changed, renamed and deleted files since the handoff, and the items that name them; check results recorded with when, where, the exit status and the edits after them | Prove the code still behaves as the handoff says |

## Deleting your data

- `/contextrail:purge` lists what it would delete for this project; add `--yes` to delete the current handoff, its archive, resume prompt and session state. `--archives` deletes only the archive, `--stale DAYS` every project folder untouched for that long, `--everything` all data except `config.json`.
- Uninstalling the plugin (`claude plugin uninstall contextrail@contextrail`) leaves `~/.contextrail` in place. Run `/contextrail:purge --everything --yes` before, or delete the folder after. If you turned on the status line, remove the `statusLine` entry from `~/.claude/settings.json`; until then the launcher keeps running your previous status line, if it wrapped one.
- Claude Code's transcripts are Claude Code's, under `~/.claude/projects/`, and ContextRail never deletes them.
