---
description: Check that ContextRail is set up correctly, its hooks run, and the handoff reaches new sessions
allowed-tools: ["Bash(${CLAUDE_PLUGIN_ROOT}/contextrail/cr.py:*)"]
---

```!
"${CLAUDE_PLUGIN_ROOT}/contextrail/cr.py" doctor --exit-zero $ARGUMENTS
```

Show the ContextRail doctor report above to the user as it is. For each `warn` or `FAIL` line, add one short sentence on what to do, based only on the report. If it prints a `"statusLine"` entry, show it unchanged and offer to put it in the settings file it names; change that file only if the user says yes. Don't run other commands.
