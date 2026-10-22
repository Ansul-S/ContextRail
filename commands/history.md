---
description: List this project's saved handoffs, or make an archived one current with --restore N
argument-hint: "[--restore N]"
allowed-tools: ["Bash(${CLAUDE_PLUGIN_ROOT}/contextrail/cr.py:*)"]
---

```!
"${CLAUDE_PLUGIN_ROOT}/contextrail/cr.py" history --exit-zero $ARGUMENTS
```

Show the ContextRail output above to the user as it is. A restored handoff takes effect from the next session, so if this session started with a different handoff loaded, that is expected: don't compare the two. Don't run other commands.
