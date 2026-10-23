import type { EngineInterface, Register, Timer } from 'claude-code'

// ContextRail's Stop hook asks for a handoff by blocking the stop with a reason
// that starts with REQUEST (prompts.request in ContextRail). A later stop whose
// message holds the handoff block, and that ContextRail lets end, means it saved
// the handoff; it blocks again only to ask for a fix.
const REQUEST = 'ContextRail: this session is at'
const OWN = 'ContextRail:'
const BLOCK = /<contextrail-handoff>\s*([\s\S]*?)\s*<\/contextrail-handoff>/g
const HEADING = /^##[ \t]+(.+?)[ \t#]*$/m
const FALLBACK = 'Continue where the last session stopped. Start with the handoff\'s "Verify first" commands.'

const PANE = 'contextrail-continue'
const QUESTION = 'ContextRail saved the handoff. Start a new session from it?'
const YES = 'Start a new session'
const NO = 'Keep this session'
const KEPT = 'ContextRail: kept this session. The handoff is saved; type /clear when you want to continue from it.'
// The question waits for the turn to finish ending, so the pane can take the keys.
const OFFER_DELAY_MS = 500

type State = {
  isRequested: boolean // ContextRail asked for a handoff and none was saved since
  resume: string | undefined // the resume prompt while the person is asked
  offer: Timer | undefined
}

/** The last handoff block in a message, without its tags. */
function handoffOf(text: string | undefined): string | undefined {
  const blocks = [...(text ?? '').matchAll(BLOCK)]
  return blocks.at(-1)?.[1]?.trim()
}

/** The handoff's `## Resume prompt`, read as ContextRail reads it. */
function resumePromptOf(body: string): string | undefined {
  const parts = body.split(HEADING)
  let resume: string | undefined
  for (let i = 1; i < parts.length - 1; i += 2) {
    const key = (parts[i] ?? '').toLowerCase().replace(/[^a-z]+/g, ' ').trim()
    if (key === 'resume prompt' || key === 'resume') {
      resume = (parts[i + 1] ?? '').trim() || undefined
    }
  }
  return resume
}

/** Drops the question without an answer: the person carried on in this session. */
async function withdraw($: EngineInterface, state: State): Promise<void> {
  state.offer?.cancel()
  state.offer = undefined
  if (state.resume === undefined) {
    return
  }
  state.resume = undefined
  await $.ui.close({ id: PANE })
}

/** Asks in a pane that y and n answer, or in a dialog where no pane can be shown now. */
async function ask($: EngineInterface, state: State): Promise<void> {
  state.offer = undefined
  const resume = state.resume
  if (resume === undefined) {
    return
  }
  const opened = await $.ui.open({
    id: PANE,
    title: 'ContextRail',
    focus: true,
    closeOnEscape: true,
    holdToasts: true,
    rows: 4,
  })
  if (opened.isPlaced) {
    return
  }
  // A pane opened unasked waits undrawn on a narrow terminal.
  await $.ui.close({ id: PANE })
  let answer: string | undefined
  try {
    answer = await $.ui.ask(QUESTION, { options: [YES, NO], header: 'ContextRail' })
  } catch {
    answer = undefined // dismissed, or nobody to ask (claude -p)
  }
  if (state.resume !== resume) {
    return // a new turn took the question back while the dialog was up
  }
  if (answer === YES) {
    startNew($, state)
  } else {
    await keep($, state)
  }
}

/** y: clears the session and sends the resume prompt. */
function startNew($: EngineInterface, state: State): void {
  const resume = state.resume
  if (resume === undefined) {
    return
  }
  state.resume = undefined
  // From a timer: `/clear` is refused inside a hook the turn waits on.
  state.offer = $.clock.after(0, async () => {
    state.offer = undefined
    await $.ui.close({ id: PANE })
    try {
      await $.command.run({ command: 'clear' })
      await $.prompt.submit({ text: resume })
    } catch (error) {
      $.ui.toast(`ContextRail: could not clear and continue (${String(error)}). Type /clear to continue.`)
    }
  })
}

/** n: keeps the session; the saved handoff still loads after a later /clear. */
async function keep($: EngineInterface, state: State): Promise<void> {
  if (state.resume === undefined) {
    return
  }
  state.resume = undefined
  await $.ui.close({ id: PANE })
  $.ui.toast(KEPT)
}

export const register: Register = (on, options) => {
  const afterManual = options['after_manual'] === true
  const state: State = { isRequested: false, resume: undefined, offer: undefined }

  on('prompt.submit', async ($, e, next) => {
    if (e.origin.kind === 'composer') {
      await withdraw($, state)
    }
    return next(e)
  })

  // New work on the main loop (a prompt, a background task's notification)
  // means the handoff no longer describes where the session stops.
  on('turn.start', async ($, e, next) => {
    if (e.agentId === undefined) {
      await withdraw($, state)
    }
    return next(e)
  })

  on('session.end', async ($, e, next) => {
    await withdraw($, state)
    state.isRequested = false
    return next(e)
  })

  // Escape, or the pane's close mark, answers n.
  on('ui.close', async ($, e, next) => {
    const result = await next(e)
    if (e.id === PANE && e.origin.kind === 'person' && state.resume !== undefined) {
      state.resume = undefined
      $.ui.toast(KEPT)
    }
    return result
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    if (state.resume === undefined) {
      return <Text dimColor>No handoff is waiting. Press Esc to close.</Text>
    }
    return (
      <Box flexDirection="column">
        <Text>ContextRail saved the handoff.</Text>
        <Text>Press y to start a new Claude Code session. Press n to continue using the current session.</Text>
        <Box flexDirection="row" columnGap={3}>
          <Button key="yes" hotkey="y" plain onPress={() => startNew($, state)}>
            {YES}
          </Button>
          <Button key="no" hotkey="n" plain onPress={() => keep($, state)}>
            {NO}
          </Button>
        </Box>
        {!e.props.isFocused && <Text dimColor>Press ctrl+x tab to answer here.</Text>}
      </Box>
    )
  })

  on('classic.Stop', async ($, e, next) => {
    const result = await next(e)
    if (result.block !== undefined) {
      if (result.block.startsWith(OWN)) {
        // Claude Code labels every blocked stop "Stop hook error"; this one is a request.
        $.ui.toast('ContextRail: the "Stop hook error" above is ContextRail\'s request to the agent. Nothing failed.')
      }
      state.isRequested ||= result.block.startsWith(REQUEST)
      return result
    }
    const body = handoffOf(e.last_assistant_message)
    if (body === undefined || !(state.isRequested || afterManual)) {
      return result
    }
    state.isRequested = false
    const pending = (e.background_tasks?.length ?? 0) + (e.session_crons?.length ?? 0)
    if (pending > 0) {
      $.ui.toast(
        'ContextRail: handoff saved. Not offering a new session while background or scheduled tasks are pending. ' +
          'Type /clear when they are done.',
      )
      return result
    }
    await withdraw($, state)
    state.resume = resumePromptOf(body) ?? FALLBACK
    state.offer = $.clock.after(OFFER_DELAY_MS, () => void ask($, state))
    return result
  })
}
