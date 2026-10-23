---
description: Lightning Mode - start a new session from the handoff at a context size you set, or turn it off
argument-hint: "[200k | off]"
allowed-tools: ["Bash(${CLAUDE_PLUGIN_ROOT}/contextrail/cr.py:*)"]
---

```!
"${CLAUDE_PLUGIN_ROOT}/contextrail/cr.py" lightning --exit-zero $ARGUMENTS
```

Show the ContextRail output above to the user as it is. Don't run other commands.
