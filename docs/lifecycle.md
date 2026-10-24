# Session lifecycle

This page describes the states a Claude Code session goes through with ContextRail, the events that move it on, and what happens when an event repeats, fails or never comes. The Python hooks keep their part of the state in `state.json` (one entry per session, see `store.DEFAULT_ENTRY`). The optional `contextrail-continue` plugin keeps its part in memory, for the session it runs in. `tests/test_lifecycle.py` covers the transitions below.

## States

```
Monitoring ──(context reaches the handoff point)──► Due
Due ──(background task running, or the agent asked you a question, below the wrap-up point)──► Waiting
Waiting ──(the task ends / you answer / the wrap-up point is reached)──► Due
Due ──(Stop hook)──► Requested
Requested ──(reply holds a handoff block)──► Validating
Validating ──(problems, first time)──► Requested (one fix request)
Validating ──(fine, or problems a second time)──► Saved
Saved (checkpoint) ──► Monitoring (asks again once context grows by refresh_tokens)
Saved (final) ──(contextrail-continue)──► Resume offered
Resume offered ──(yes, or the Lightning countdown ends)──► Continued (/clear, then the handoff loads)
Resume offered ──(no, Esc, typing)──► Monitoring (the handoff stays saved; /clear loads it later)
```

| State | What is true | Recorded as |
|---|---|---|
| Monitoring | Context is below the next request point (`config.next_request`). Hooks read the transcript and return nothing. | `asked_kind` empty |
| Due | The Stop hook found the context at the handoff point (checkpoint), the wrap-up point (final), the Lightning limit, or `refresh_tokens` past the last save. | the rule and the numbers in `trigger` once asked |
| Waiting | A background task is in flight, or the turn ended with a question to you, and the wrap-up point is not reached. One notice says so; no request is sent. Scheduled tasks (`/loop`, reminders) don't hold the request; they are named in it. | `deferred_notice`, `question_notice` |
| Requested | ContextRail asked for the handoff: as Stop feedback on Claude Code 2.1.163 and later, as a blocked stop before (`request_style`). | `attempts`, `asked_kind`, `trigger` |
| Validating | The reply holds a `<contextrail-handoff>` block. It is checked for its sections, its size, and against the recorded facts (`content_checks`). | `retries` |
| Saved | The handoff is the current one. The previous one was archived first. | `saved_tokens`, `saved_kind`, `saved_running` |
| Resume offered | `contextrail-continue` asks y/n, or counts down in Lightning Mode. It waits while the agent waits for your answer, while background tasks run, and while a one-shot scheduled task is due within the hour. | the mod's memory |
| Continued | `/clear` ran; the SessionStart hook loaded the handoff into the new session. | `loaded_sha`, `loaded_at` |

## Other paths

| Path | What happens |
|---|---|
| **Manual handoff** | `/contextrail:handoff` asks for one at any point. It is saved like a requested one: final from the wrap-up point on, a checkpoint before. |
| **No block in the reply** | The request counts as an attempt. After `max_attempts` (3) ContextRail stops asking in that session (`gave_up`), and the previous handoff stays current. |
| **API error** (StopFailure) | The turn ended on a rate limit, an overload or another API error. Nothing is saved, the current handoff stays as it was, and a request made in the last 15 minutes doesn't count as an attempt, so the next normal stop asks again. `/contextrail:status` shows the error. |
| **Final handoff saved while tasks run** | The next session starts from it, and the first stop after the tasks finish asks for a fresh one (`saved_running`). |
| **Compaction** | PreCompact saves the recorded facts with the last narrative carried over, unless this session saved one recently, and starts the counting again from the first reply after compaction. The handoff then loads once into the compacted session. |
| **Session ends without a handoff** | SessionEnd saves the facts with the last narrative carried over when the session edited files and is `checkpoint_tokens` past its start. Claude Code stops this hook after about 1.5 s, so it gives up rather than run late. |
| **`/resume`** | Nothing loads: the resumed session already has its own context. |
| **Restore** | `/contextrail:history --restore N` makes an archived handoff current again, archiving the current one. An archive saved for another project is refused. |
| **Purge** | `/contextrail:purge` deletes this project's handoffs and state under the project lock; the next session starts without a handoff. |
| **Hook killed or timed out** | Every write goes to a temporary file that replaces the target, so a reader sees the old file or the new one. A save writes the archive, then the handoff, then its meta. A process stopped in between leaves a usable handoff: the old one, or the new one with its meta marked as not matching (the drift check then says changes can't be checked). |
| **Another project's folder** | A folder whose `project.json` names another path is never read. The doctor reports it. |

## Repeated events

Every handler is safe to run twice for the same event, which happens when two copies of the plugin are enabled or Claude Code retries a hook.

| Repeated event | Result |
|---|---|
| Stop with the same handoff, same session and context size, no request since | Saved once; the second one returns nothing. |
| Stop with `stop_hook_active` | Never a second request in a row; the next turn asks. |
| SessionStart of the same kind in the same session within 60 s | Loaded once. A later `/clear` loads it again. |
| StopFailure | Refunds at most the one request it followed. |
| PreCompact, SessionEnd | Skip when this session saved recently. |
| `/contextrail:purge --yes`, `--restore N` | A second purge finds nothing to delete; a second restore of the same number restores what is now at that place in the list, which `/contextrail:history` shows first. |

## Locks

State changes hold one lock per project (`.lock` in its folder), shared by every session open in that project: `fcntl` on macOS and Linux, `msvcrt` on Windows, and a file created exclusively where neither exists (a lock file left by a dead process is taken over after 60 s). A hook that can't get the lock in 10 s gives up and logs it; it never blocks Claude Code for longer than its own timeout.
