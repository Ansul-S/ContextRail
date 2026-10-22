---
description: Write a handoff now. ContextRail saves it and loads it into your next session.
---

Write a ContextRail handoff for this session now, even if context is not full yet. ContextRail saves it outside the repository and loads it into the next session in this project.

Reply with the handoff block below, filled in. No tool calls are needed.
- Be specific and brief: file paths, function names, commands, numbers.
- List an item under Done only if it was verified in this session. Anything unverified goes under In progress, marked unverified.
- If a handoff from an earlier session was loaded at the start, carry forward its Decisions and Dead ends that still apply, and drop what is finished.
- Leave out secrets, tokens and credentials.
- Stay under 8,000 characters. Git state, edited files and test commands are recorded automatically, so don't repeat them.

<contextrail-handoff>
## Goal
What we are building or fixing, and why (1-2 sentences).

## Done
- Finished and verified in this session, with the evidence (test, command output, commit).

## In progress
- Half-finished work: file:line, its current state, what is left.

## Next steps
1. The very next concrete action.
2. ...

## Decisions
- Decision, and why (include constraints the user set).

## Dead ends
- What was tried and why it failed, so it is not retried.

## Open questions
- Questions waiting on the user, or "None".

## Verify first
- Commands that confirm the state before continuing.

## Resume prompt
Two or three lines the user can paste into the next session to continue.
</contextrail-handoff>

$ARGUMENTS
