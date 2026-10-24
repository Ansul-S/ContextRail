import type { EngineInterface, Register, Timer } from 'claude-code'

// ContextRail's Stop hook asks for a handoff by continuing the turn with text that
// starts with REQUEST (prompts.request in ContextRail): as Stop feedback
// (additionalContext) on Claude Code 2.1.163 and later, as a blocked stop before.
// A later stop whose message holds the handoff block, and that ContextRail lets
// end, means it saved the handoff; it continues the turn again only to ask for a fix.
const REQUEST = 'ContextRail: this session is at'
// A request with these words (prompts.CHECKPOINT in ContextRail) asks for a checkpoint before the
// hard point: work goes on in the session after it, so no new session is offered.
const CHECKPOINT = 'This handoff is a checkpoint:'
const OWN = 'ContextRail:'
const BLOCK = /<contextrail-handoff>\s*([\s\S]*?)\s*<\/contextrail-handoff>/g
// The same with each tag on a line of its own, so a tag quoted inside the handoff doesn't end it.
const LINE_BLOCK = /^[ \t]*<contextrail-handoff>[ \t\r]*\n([\s\S]*?)\n[ \t]*<\/contextrail-handoff>[ \t\r]*$/gm
const HEADING = /^##[ \t]+(.+?)[ \t#]*$/m
// A reply that waits for the person's answer: its last line ends with a question mark, maybe inside
// closing markdown, quotes or brackets (handlers.QUESTION_END_RE in ContextRail; a test keeps them in step).
const QUESTION_END = /\?[*_`'"’”)\]\s]*$/
// Background task types that are watchers or Claude Code's own chores, not work: they can run for good,
// so they hold nothing (handlers.PASSIVE in ContextRail; a test keeps them in step).
const PASSIVE = ['monitor', 'dream', 'auto-mode scan', 'memory import']
// A one-shot scheduled task due within this long holds the offer, as running work does: its turn
// is likely to carry the work on. A later one, and a recurring one, doesn't hold it, since that
// could last for days (recurring tasks expire after 7). A self-paced /loop shows as a one-shot
// wakeup that schedules the next when it fires, so a one-shot whose prompt was already seen in
// one that fired counts as recurring.
const ONE_SHOT_HOLD_MS = 60 * 60 * 1000
const FALLBACK = 'Continue where the last session stopped. Start with the handoff\'s "Verify first" commands.'

const PANE = 'contextrail-continue'
const QUESTION = 'ContextRail saved the handoff. Start a new session from it?'
const YES = 'Start a new session'
const NO = 'Keep this session'
const KEPT = 'ContextRail: kept this session. The handoff is saved; type /clear when you want to continue from it.'
const TYPING = 'ContextRail: kept this session because you are typing. Type /clear when you want to continue from the handoff.'
const HELD =
  'ContextRail: handoff saved. Background tasks are still running, or a task is due to run soon; once they finish ' +
  'and their results are shown, ContextRail offers a new session. Type /clear to continue from the handoff sooner.'
const BLOCKED = 'ContextRail: the "Stop hook error" above is ContextRail\'s request to the agent. Nothing failed.'
// The question waits for the turn to finish ending, so the pane can take the keys.
const OFFER_DELAY_MS = 500

// Lightning Mode (`/contextrail:lightning`): a new session starts on its own after a countdown.
const LIGHTNING_SECONDS = 10
const BLUE = '#3b9eff'
const PALE = '#9ccfff'

type State = {
  isRequested: boolean // ContextRail asked for a handoff and none was saved since
  isCheckpoint: boolean // that request asked for a checkpoint
  held: string | undefined // a saved handoff's resume prompt, waiting for background work to finish
  resume: string | undefined // the resume prompt while the person is asked, or counted down to
  left: number | undefined // seconds left in a Lightning countdown
  offer: Timer | undefined
  scheduled: string[] // the session's scheduled tasks that don't hold the offer, shown with it
  oneShots: Map<string, string> // id -> prompt of one-shot tasks seen while an offer was held
  loops: Set<string> // prompts of one-shot tasks that scheduled themselves again: self-paced loops
}

type Cron = { id?: string; schedule?: string; recurring?: boolean; prompt?: string }

/**
 * When a one-shot task's cron expression fires next, for the plain "M H DoM Mon *" form a
 * one-shot uses (DoM and Mon may be *), in local time; undefined for any other form.
 */
function nextFire(schedule: string | undefined, now: number): number | undefined {
  const fields = (schedule ?? '').trim().split(/\s+/)
  if (fields.length !== 5 || fields[4] !== '*') {
    return undefined
  }
  const [minute, hour, day, month] = fields.map((field) => (field === '*' ? undefined : Number(field)))
  if (minute === undefined || hour === undefined || !Number.isInteger(minute) || !Number.isInteger(hour)) {
    return undefined
  }
  if ((day !== undefined && !Number.isInteger(day)) || (month !== undefined && !Number.isInteger(month))) {
    return undefined
  }
  const start = new Date(now)
  for (let days = 0; days <= 366; days++) {
    const at = new Date(start.getFullYear(), start.getMonth(), start.getDate() + days, hour, minute)
    const matches = (day === undefined || at.getDate() === day) && (month === undefined || at.getMonth() + 1 === month)
    if (matches && at.getTime() >= now - 60_000) {
      return at.getTime()
    }
  }
  return undefined
}

/** A scheduled task as the person would recognise it. */
function cronLabel(cron: Cron): string {
  const prompt = (cron.prompt ?? 'a prompt').replace(/\s+/g, ' ').trim()
  return `${prompt.length > 50 ? `${prompt.slice(0, 49)}…` : prompt} (${cron.schedule ?? '?'})`
}

/**
 * The scheduled tasks that hold the offer, and those that don't. A one-shot task due soon holds
 * it, unless it is a self-paced loop's next wakeup; recurring tasks and later one-shots don't.
 */
function sortCrons(state: State, crons: Cron[], now: number): { holding: Cron[]; others: Cron[] } {
  const ids = new Set(crons.map((cron) => cron.id ?? ''))
  for (const [id, prompt] of [...state.oneShots]) {
    if (!ids.has(id)) {
      state.oneShots.delete(id)
      if (crons.some((cron) => cron.recurring === false && cron.prompt === prompt)) {
        state.loops.add(prompt) // it fired, and scheduled the same prompt again
      }
    }
  }
  const holding: Cron[] = []
  const others: Cron[] = []
  for (const cron of crons) {
    const due = nextFire(cron.schedule, now)
    const isOneShot = cron.recurring === false
    const isLoop = cron.prompt !== undefined && state.loops.has(cron.prompt)
    if (isOneShot && !isLoop && (due === undefined || due - now <= ONE_SHOT_HOLD_MS)) {
      holding.push(cron)
      state.oneShots.set(cron.id ?? '', cron.prompt ?? '')
    } else {
      others.push(cron)
    }
  }
  return { holding, others }
}

/** ContextRail's request or fix in a Stop result, as a block or as feedback, and which it is. */
function ownText(result: { block?: string; additionalContext?: readonly string[] }): { text: string; isBlock: boolean } | undefined {
  if (result.block !== undefined && result.block.startsWith(OWN)) {
    return { text: result.block, isBlock: true }
  }
  const text = (result.additionalContext ?? []).find((context) => context.startsWith(OWN))
  return text === undefined ? undefined : { text, isBlock: false }
}

/** The last handoff block in a message, without its tags. */
function handoffOf(text: string | undefined): string | undefined {
  const onLines = [...(text ?? '').matchAll(LINE_BLOCK)]
  const blocks = onLines.length > 0 ? onLines : [...(text ?? '').matchAll(BLOCK)]
  return blocks.at(-1)?.[1]?.trim()
}

/** Whether the agent ended its turn with a question to the person. */
function waitsForAnswer(text: string | undefined): boolean {
  const lines = (text ?? '').split('\n').filter((line) => line.trim() !== '')
  return lines.length > 0 && QUESTION_END.test(lines[lines.length - 1] ?? '')
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

/**
 * A `lightning_tokens` value as ContextRail's config reads it (Python's `int()`):
 * a number cut to a whole one, or a string of digits; undefined where it can't
 * use the value, which then keeps the one before.
 */
function tokensOf(value: unknown): number | undefined {
  let tokens: number
  if (typeof value === 'number' && Number.isFinite(value)) {
    tokens = Math.trunc(value)
  } else if (typeof value === 'string' && /^\s*[+-]?\d+(?:_\d+)*\s*$/.test(value)) {
    tokens = Number(value.trim().replace(/_/g, ''))
  } else {
    return undefined // a boolean, null, a list, or text like "2e5" or "200k"
  }
  return tokens < 0 ? undefined : tokens
}

/**
 * Whether Lightning Mode is on: ContextRail's `lightning_tokens` from its
 * environment variable, else from the global config.json, where
 * `/contextrail:lightning` writes it. ContextRail reads it only there too.
 */
async function isLightning($: EngineInterface): Promise<boolean> {
  const fromEnv = tokensOf(await $.env.get('CONTEXTRAIL_LIGHTNING_TOKENS'))
  if (fromEnv !== undefined) {
    return fromEnv > 0
  }
  const home = (await $.env.get('HOME')) ?? ''
  const root = ((await $.env.get('CONTEXTRAIL_HOME')) || `${home}/.contextrail`).replace(/^~(?=\/|$)/, home)
  let value: unknown
  try {
    value = JSON.parse(await $.fs.read(`${root}/config.json`))?.lightning_tokens
  } catch {
    value = undefined // no config.json, or not JSON: ContextRail's defaults, Lightning off
  }
  return (tokensOf(value) ?? 0) > 0
}

/** Drops the question or countdown without an answer: the person carried on in this session. */
async function withdraw($: EngineInterface, state: State): Promise<void> {
  state.offer?.cancel()
  state.offer = undefined
  state.left = undefined
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

/** Lightning: counts down in a pane, or in a toast where no pane can be shown, then starts a new session. */
async function countDown($: EngineInterface, state: State): Promise<void> {
  state.offer = undefined
  if (state.resume === undefined) {
    return
  }
  state.left = LIGHTNING_SECONDS
  const opened = await $.ui.open({
    id: PANE,
    title: '⚡ ContextRail',
    focus: true,
    closeOnEscape: true,
    holdToasts: true,
    rows: 3,
  })
  if (state.left === undefined) {
    return // taken back while the pane opened
  }
  if (!opened.isPlaced) {
    await $.ui.close({ id: PANE })
    $.ui.toast(
      `⚡ ContextRail Lightning Mode: handoff saved. Starting a new session in ${LIGHTNING_SECONDS} s; ` +
        'start typing to stay in this session.',
      { timeoutMs: LIGHTNING_SECONDS * 1000 },
    )
  }
  state.offer = $.clock.every(1000, async () => {
    if (state.left === undefined) {
      return
    }
    state.left -= 1
    if (state.left > 0) {
      $.ui.invalidate('ui.render')
      return
    }
    state.offer?.cancel()
    state.offer = undefined
    state.left = undefined
    if ((await $.prompt.read()).text.trim() !== '') {
      await keep($, state, TYPING)
    } else {
      startNew($, state)
    }
  })
}

/** y, or the end of a countdown: clears the session and sends the resume prompt. */
function startNew($: EngineInterface, state: State): void {
  const resume = state.resume
  if (resume === undefined) {
    return
  }
  state.offer?.cancel()
  state.resume = undefined
  state.left = undefined
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
async function keep($: EngineInterface, state: State, message = KEPT): Promise<void> {
  if (state.resume === undefined) {
    return
  }
  state.offer?.cancel()
  state.offer = undefined
  state.resume = undefined
  state.left = undefined
  await $.ui.close({ id: PANE })
  $.ui.toast(message)
}

export const register: Register = (on, options) => {
  const afterManual = options['after_manual'] === true
  const state: State = {
    isRequested: false, isCheckpoint: false, held: undefined, resume: undefined, left: undefined, offer: undefined,
    scheduled: [], oneShots: new Map(), loops: new Set(),
  }

  // A prompt the person typed means they carry on here: nothing is offered for this handoff.
  on('prompt.submit', async ($, e, next) => {
    if (e.origin.kind === 'composer') {
      state.held = undefined
      await withdraw($, state)
    }
    return next(e)
  })

  // Other new work on the main loop, such as a background task's notification, takes an open
  // question back until the turn ends; the next stop with nothing pending offers it again.
  on('turn.start', async ($, e, next) => {
    if (e.agentId === undefined && state.resume !== undefined) {
      state.held = state.resume
      await withdraw($, state)
    }
    return next(e)
  })

  on('session.end', async ($, e, next) => {
    state.held = undefined
    await withdraw($, state)
    state.isRequested = false
    state.oneShots.clear()
    state.loops.clear()
    return next(e)
  })

  // Escape, or the pane's close mark, answers n.
  on('ui.close', async ($, e, next) => {
    const result = await next(e)
    if (e.id === PANE && e.origin.kind === 'person' && state.resume !== undefined) {
      state.offer?.cancel()
      state.offer = undefined
      state.resume = undefined
      state.left = undefined
      $.ui.toast(KEPT)
    }
    return result
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    if (state.resume === undefined) {
      return <Text dimColor>No handoff is waiting. Press Esc to close.</Text>
    }
    const hint = !e.props.isFocused && <Text dimColor>Press ctrl+x tab to answer here.</Text>
    if (state.left !== undefined) {
      const done = LIGHTNING_SECONDS - state.left
      return (
        <Box flexDirection="column">
          <Text color={BLUE} bold>
            ⚡ Lightning Mode: starting a new session in {state.left} s
          </Text>
          <Text>
            <Text color={BLUE}>{'━'.repeat(done * 2)}</Text>
            <Text color={PALE}>●</Text>
            <Text dimColor>{'┄'.repeat(state.left * 2)}</Text>
            <Text color={BLUE}> ⚡</Text>
          </Text>
          <Box flexDirection="row" columnGap={3}>
            <Button key="now" hotkey="y" plain onPress={() => startNew($, state)}>
              Start now
            </Button>
            <Button key="stay" hotkey="n" plain onPress={() => keep($, state)}>
              Stay in this session
            </Button>
          </Box>
          {hint}
        </Box>
      )
    }
    const scheduled = state.scheduled.length > 0 && (
      <Text dimColor>
        Scheduled in this session: {state.scheduled.slice(0, 2).join('; ')}
        {state.scheduled.length > 2 ? ` and ${state.scheduled.length - 2} more` : ''}. A new session may not run it.
      </Text>
    )
    return (
      <Box flexDirection="column">
        <Text>ContextRail saved the handoff.</Text>
        <Text>Press y to start a new Claude Code session. Press n to continue using the current session.</Text>
        {scheduled}
        <Box flexDirection="row" columnGap={3}>
          <Button key="yes" hotkey="y" plain onPress={() => startNew($, state)}>
            {YES}
          </Button>
          <Button key="no" hotkey="n" plain onPress={() => keep($, state)}>
            {NO}
          </Button>
        </Box>
        {hint}
      </Box>
    )
  })

  on('classic.Stop', async ($, e, next) => {
    const result = await next(e)
    const own = ownText(result)
    if (own !== undefined) {
      if (own.isBlock) {
        // Claude Code labels every blocked stop "Stop hook error"; this one is a request.
        $.ui.toast(BLOCKED)
      }
      if (own.text.startsWith(REQUEST)) {
        state.isRequested = true
        state.isCheckpoint = own.text.includes(CHECKPOINT)
      }
      return result
    }
    if (result.block !== undefined || (result.additionalContext?.length ?? 0) > 0) {
      return result // another hook keeps the turn going: this is not where it ends
    }
    const body = handoffOf(e.last_assistant_message)
    const isSaved = body !== undefined && (state.isRequested || afterManual || state.held !== undefined)
    if (isSaved) {
      const isOffered = !(state.isRequested && state.isCheckpoint)
      state.isRequested = false
      if (isOffered) {
        state.held = resumePromptOf(body) ?? FALLBACK
      }
    }
    if (state.held === undefined) {
      return result
    }
    // Background work still runs, or a task is due soon, and its results are still to come in this session.
    const working = (e.background_tasks ?? []).filter((task) => !PASSIVE.includes(task.type))
    const { holding, others } = sortCrons(state, [...(e.session_crons ?? [])], await $.clock.now())
    if (working.length + holding.length > 0) {
      if (isSaved) {
        $.ui.toast(HELD)
      }
      return result
    }
    state.scheduled = others.map(cronLabel)
    // The agent waits for the person's answer: no question or countdown over it. Answering carries on here.
    if (waitsForAnswer(e.last_assistant_message)) {
      return result
    }
    await withdraw($, state)
    state.resume = state.held
    state.held = undefined
    // Lightning starts a new session by itself only when nothing is scheduled here; with a schedule it asks.
    if (state.scheduled.length === 0 && (await isLightning($))) {
      state.offer = $.clock.after(OFFER_DELAY_MS, () => void countDown($, state))
    } else {
      state.offer = $.clock.after(OFFER_DELAY_MS, () => void ask($, state))
    }
    return result
  })
}
