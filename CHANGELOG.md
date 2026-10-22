# Changelog

## 0.1.0 (2026-10-22)

First release.

- **Handoff request:** the Stop hook asks for a handoff at 100k tokens or 50% of the window, whichever comes first, and again every 40k tokens if the session continues.
- **Mid-turn warning:** the PostToolUse hook tells the agent to wrap up at 130k tokens or 65% of the window.
- **Before compaction:** the PreCompact hook saves the session facts and carries over the last narrative.
- **Session start:** the SessionStart hook loads the handoff on startup, `/clear` and compaction, together with what changed in the repository since.
- **Validation:** an incomplete handoff gets one request for a fix.
- **Storage:** handoffs live in `~/.contextrail/`, with a history of previous ones.
- **Commands:** `/contextrail:handoff` and `/contextrail:status`.
