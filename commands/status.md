---
description: Show context usage, trigger points and the saved handoff for this project
allowed-tools: ["Bash(${CLAUDE_PLUGIN_ROOT}/contextrail/cr.py:*)"]
---

```!
"${CLAUDE_PLUGIN_ROOT}/contextrail/cr.py" status
```

Show the ContextRail status above to the user as it is. Add a short note only if something looks wrong.
