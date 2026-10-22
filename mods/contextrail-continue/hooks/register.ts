import type { EngineInterface, Register, Timer } from 'claude-code'

// ContextRail's Stop hook asks for a handoff by blocking the stop with a reason
// that starts with REQUEST (prompts.request in ContextRail). A later stop whose
// message holds the handoff block, and that ContextRail lets end, means it saved
// the handoff; it blocks again only to ask for a fix.
const REQUEST = 'ContextRail: this session is at'
const BLOCK = /<contextrail-handoff>\s*([\s\S]*?)\s*<\/contextrail-handoff>/g
const HEADING = /^##[ \t]+(.+?)[ \t#]*$/m
const FALLBACK = 'Continue where the last session stopped. Start with the handoff\'s "Verify first" commands.'

type State = {
  isRequested: boolean // ContextRail asked for a handoff and none was saved since
  inARow: number // automatic continues since the person last sent a prompt
  countdown: Timer | undefined
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

function cancel(state: State): void {
  state.countdown?.cancel()
  state.countdown = undefined
}

async function continueFrom($: EngineInterface, state: State, resume: string): Promise<void> {
  state.countdown = undefined
  if ((await $.prompt.read()).text.trim() !== '') {
    $.ui.toast('ContextRail: kept this session because you are typing. Type /clear when you are ready.')
    return
  }
  state.inARow += 1
  try {
    await $.command.run({ command: 'clear' })
    await $.prompt.submit({ text: resume })
  } catch (error) {
    $.ui.toast(`ContextRail: could not clear and continue (${String(error)}). Type /clear to continue.`)
  }
}

export const register: Register = (on, options) => {
  const delayMs = Math.max(0, Number(options['delay_seconds'] ?? 10)) * 1000
  const maxInARow = Math.max(1, Number(options['max_in_a_row'] ?? 3))
  const afterManual = options['after_manual'] === true
  const state: State = { isRequested: false, inARow: 0, countdown: undefined }

  on('prompt.submit', ($, e, next) => {
    if (e.origin.kind === 'composer') {
      state.inARow = 0
    }
    return next(e)
  })

  // New work on the main loop (a prompt, a background task's notification)
  // means the handoff no longer describes where the session stops.
  on('turn.start', ($, e, next) => {
    if (e.agentId === undefined) {
      cancel(state)
    }
    return next(e)
  })

  on('session.end', ($, e, next) => {
    cancel(state)
    state.isRequested = false
    return next(e)
  })

  on('classic.Stop', async ($, e, next) => {
    const result = await next(e)
    if (result.block !== undefined) {
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
        'ContextRail: handoff saved. Not clearing while background or scheduled tasks are pending. ' +
          'Type /clear when they are done.',
      )
      return result
    }
    if (state.inARow >= maxInARow) {
      $.ui.toast(
        `ContextRail: handoff saved. Continued ${state.inARow} times in a row without a prompt from you, ` +
          'so this one waits. Type /clear to continue.',
      )
      return result
    }
    const resume = resumePromptOf(body) ?? FALLBACK
    cancel(state)
    $.ui.toast(
      `ContextRail: handoff saved. Clearing and continuing in ${delayMs / 1000} s; start typing to stay in this session.`,
      { timeoutMs: Math.max(4000, delayMs) },
    )
    state.countdown = $.clock.after(delayMs, () => void continueFrom($, state, resume))
    return result
  })
}
