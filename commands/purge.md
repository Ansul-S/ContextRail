---
description: Delete this project's saved handoffs and session state (lists first; add --yes to delete)
argument-hint: "[--archives | --legacy | --stale DAYS | --everything] [--yes]"
allowed-tools: ["Bash(${CLAUDE_PLUGIN_ROOT}/contextrail/cr.py:*)"]
---

```!
"${CLAUDE_PLUGIN_ROOT}/contextrail/cr.py" purge --exit-zero $ARGUMENTS
```

Show the ContextRail output above to the user as it is. Don't run other commands, and don't run it again with --yes yourself: deleting is the user's decision, made by typing the command with --yes.
