import { expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

const REQUEST = 'ContextRail: this session is at 102,000 tokens of context. The user set up ContextRail...'
const FIX = 'ContextRail: the handoff needs fixing before it can be saved'
const RESUME = 'Continue the API rate limiting in api/app.py.'
const HANDOFF = [
  'Here is the handoff.',
  '<contextrail-handoff>',
  '## Goal',
  'Ship the limiter.',
  '## Resume prompt',
  RESUME,
  '</contextrail-handoff>',
].join('\n')
const QUESTION = 'ContextRail saved the handoff. Start a new session from it?'
const PANE = {
  plugin: 'contextrail-continue',
  component: 'Pane',
  requestId: 'contextrail-continue',
  props: {
    title: 'ContextRail',
    isFocused: true,
    bodyColumns: 100,
    placement: 'inline',
    scroll: { offset: 0, bodyRows: 4 },
    view: {},
  },
} as const

/**
 * Stands in for the engine and for ContextRail's own Stop hook beneath the mod,
 * and records what the mod asks of the session.
 */
function engine(on: On) {
  const clock = mock.clock(on)
  const seen = {
    commands: [] as string[],
    prompts: [] as string[],
    toasts: [] as string[],
    opened: [] as string[],
    closed: [] as string[],
    asked: [] as unknown[],
  }
  const world = {
    stopAnswer: {} as { block?: string },
    isPlaced: true,
    answer: undefined as string | undefined, // undefined: the dialog is dismissed
    config: undefined as string | undefined, // ~/.contextrail/config.json; undefined: there is none
  }
  mock.env(on, { HOME: '/Users/test' })
  on('fs.read', (_, e) => {
    if (e.path !== '/Users/test/.contextrail/config.json' || world.config === undefined) {
      return { deny: `ENOENT: no such file, open '${e.path}'` }
    }
    return { value: world.config }
  })
  on('classic.Stop', () => world.stopAnswer)
  on('command.run', (_, e) => {
    seen.commands.push(e.command)
    return {}
  })
  on('prompt.submit', (_, e) => {
    seen.prompts.push(e.text)
    return { text: e.text }
  })
  on('ui.toast', (_, e) => {
    seen.toasts.push(e.text)
    return { value: undefined }
  })
  on('ui.open', (_, e) => {
    seen.opened.push(e.id)
    return { value: world.isPlaced ? { isPlaced: true } : { isPlaced: false, reason: 'the terminal is 100 columns wide' } }
  })
  on('ui.close', (_, e) => {
    seen.closed.push(e.id)
    return { value: undefined }
  })
  on('tool.call', { tool: 'AskUserQuestion' }, (_, e) => {
    seen.asked.push(e.questions)
    if (world.answer === undefined) {
      return { deny: 'The user dismissed the question.' }
    }
    return { result: { questions: e.questions, answers: { [QUESTION]: world.answer } } }
  })
  return { clock, seen, world }
}

async function stop($: Engine, message: string) {
  return $.classic.Stop({ stop_hook_active: false, last_assistant_message: message })
}

async function requestThenSave($: Engine, world: { stopAnswer: { block?: string } }) {
  world.stopAnswer = { block: REQUEST }
  await stop($, 'Done with the refactor.')
  world.stopAnswer = {}
  await stop($, HANDOFF)
}

test('asks in a pane once ContextRail asked and saved, and y clears and continues', async ($, on) => {
  const { clock, seen, world } = engine(on)
  await requestThenSave($, world)
  expect(seen.opened).toEqual([])
  await clock.advance(500)
  expect(seen.opened).toEqual(['contextrail-continue'])

  await clock.advance(600_000)
  expect(seen.commands).toEqual([])

  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await ui.press({ key: 'yes' })
  await clock.settle()
  expect(seen.closed).toContain('contextrail-continue')
  expect(seen.commands).toEqual(['clear'])
  expect(seen.prompts).toEqual([RESUME])
  await ui.unmount()
})

test('n keeps the session and says how to continue later', async ($, on) => {
  const { clock, seen, world } = engine(on)
  await requestThenSave($, world)
  await clock.advance(500)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await ui.press({ key: 'no' })
  await clock.advance(60_000)
  expect(seen.commands).toEqual([])
  expect(seen.closed).toContain('contextrail-continue')
  expect(seen.toasts.at(-1)).toContain('kept this session')
  await ui.unmount()
})

test('the pane shows the question and both keys, on every surface', async ($, on) => {
  const { clock, world } = engine(on)
  await requestThenSave($, world)
  await clock.advance(500)
  for (const surface of ['terminal', 'desktop', 'vscode', 'mobile'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await ui.find({ type: 'Text', text: /Press y to start a new Claude Code session/ }))?.text).toContain(
      'Press n to continue using the current session',
    )
    expect(await ui.findAll({ type: 'Button' })).toHaveLength(2)
    expect(await ui.find({ type: 'Text', text: /ctrl\+x tab/ })).toBeUndefined()
    await ui.unmount()
  }
  const unfocused = await $.ui.mount({ ...PANE, props: { ...PANE.props, isFocused: false }, surface: 'terminal' })
  expect(await unfocused.find({ type: 'Text', text: /ctrl\+x tab/ })).toBeDefined()
  await unfocused.unmount()
})

test('explains the Stop hook error and waits while ContextRail asks for a fix', async ($, on) => {
  const { clock, seen, world } = engine(on)
  world.stopAnswer = { block: REQUEST }
  await stop($, 'ok')
  expect(seen.toasts.at(-1)).toContain('"Stop hook error" above is ContextRail\'s request')
  world.stopAnswer = { block: FIX }
  await stop($, HANDOFF)
  await clock.advance(60_000)
  expect(seen.opened).toEqual([])

  world.stopAnswer = {}
  await stop($, HANDOFF)
  await clock.advance(500)
  expect(seen.opened).toEqual(['contextrail-continue'])
})

test('leaves a handoff the person asked for alone', async ($, on) => {
  const { clock, seen, world } = engine(on)
  world.stopAnswer = { block: 'Another plugin: run the linter before stopping.' }
  await stop($, 'ok')
  expect(seen.toasts).toEqual([])
  world.stopAnswer = {}
  await stop($, HANDOFF)
  await clock.advance(60_000)
  expect(seen.opened).toEqual([])
})

test('asks after a handoff the person asked for when after_manual is on', { options: { after_manual: true } }, async ($, on) => {
  const { clock, seen } = engine(on)
  await stop($, HANDOFF)
  await clock.advance(500)
  expect(seen.opened).toEqual(['contextrail-continue'])
})

test('a new turn or a prompt from the person takes the question back', async ($, on) => {
  const { clock, seen, world } = engine(on)
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  await requestThenSave($, world)
  await $.turn.start({ text: 'one more thing', turnId: 't2' })
  await clock.advance(60_000)
  expect(seen.opened).toEqual([])

  await requestThenSave($, world)
  await clock.advance(500)
  await $.prompt.submit({ text: 'wait, first this', origin: { kind: 'composer' }, wait: false })
  expect(seen.closed).toContain('contextrail-continue')
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.findAll({ type: 'Button' })).toHaveLength(0)
  await ui.unmount()
  await clock.advance(60_000)
  expect(seen.commands).toEqual([])
})

test('background work holds the offer until it finishes and its results are shown', async ($, on) => {
  const { clock, seen, world } = engine(on)
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  const running = [{ id: 'b1', type: 'shell', status: 'running', description: 'npm test' }]
  world.stopAnswer = { block: REQUEST }
  await stop($, 'ok')
  world.stopAnswer = {}
  await $.classic.Stop({ stop_hook_active: true, last_assistant_message: HANDOFF, background_tasks: running })
  await clock.advance(60_000)
  expect(seen.opened).toEqual([])
  expect(seen.toasts.at(-1)).toContain('once they finish and their results are shown, ContextRail offers a new session')

  // The task's notification starts a turn, in which the agent shows the results.
  await $.turn.start({ text: '', turnId: 't2' })
  await $.classic.Stop({ stop_hook_active: false, last_assistant_message: 'Still running.', background_tasks: running })
  await clock.advance(60_000)
  expect(seen.opened).toEqual([])
  await $.turn.start({ text: '', turnId: 't3' })
  await $.classic.Stop({ stop_hook_active: false, last_assistant_message: 'npm test passed: 214 tests.', background_tasks: [] })
  await clock.advance(500)
  expect(seen.opened).toEqual(['contextrail-continue'])
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await ui.press({ key: 'yes' })
  await clock.settle()
  expect(seen.prompts).toEqual([RESUME])
  await ui.unmount()
})

test('a prompt from the person, or the session ending, drops a held offer', async ($, on) => {
  const { clock, seen, world } = engine(on)
  on('session.end', (_, e) => ({ sessionId: e.sessionId }))
  const crons = [{ id: 'c1', schedule: '*/5 * * * *', recurring: true, prompt: '/babysit-prs' }]
  for (const carryOn of ['prompt', 'end'] as const) {
    world.stopAnswer = { block: REQUEST }
    await stop($, 'ok')
    world.stopAnswer = {}
    await $.classic.Stop({ stop_hook_active: true, last_assistant_message: HANDOFF, session_crons: crons })
    if (carryOn === 'prompt') {
      await $.prompt.submit({ text: 'how is it going?', origin: { kind: 'composer' }, wait: false })
    } else {
      await $.session.end({ reason: 'other', sessionId: 's1', resume: { id: 's1' } })
    }
    await stop($, 'All done.')
    await clock.advance(60_000)
    expect(seen.opened).toEqual([])
  }
})

test('asks in a dialog where the pane cannot be shown', async ($, on) => {
  const { clock, seen, world } = engine(on)
  world.isPlaced = false
  world.answer = 'Start a new session'
  await requestThenSave($, world)
  await clock.advance(500)
  await clock.settle()
  expect(seen.closed).toContain('contextrail-continue')
  expect(seen.asked).toHaveLength(1)
  expect(seen.commands).toEqual(['clear'])
  expect(seen.prompts).toEqual([RESUME])
})

test('a dialog answered no, or dismissed, keeps the session', async ($, on) => {
  const { clock, seen, world } = engine(on)
  world.isPlaced = false
  world.answer = 'Keep this session'
  await requestThenSave($, world)
  await clock.advance(500)
  await clock.settle()
  expect(seen.toasts.at(-1)).toContain('kept this session')

  world.answer = undefined
  await requestThenSave($, world)
  await clock.advance(500)
  await clock.settle()
  expect(seen.asked).toHaveLength(2)
  expect(seen.commands).toEqual([])
})

test('falls back to a generic prompt when the handoff has no resume prompt', async ($, on) => {
  const { clock, seen, world } = engine(on)
  world.stopAnswer = { block: REQUEST }
  await stop($, 'ok')
  world.stopAnswer = {}
  await stop($, '<contextrail-handoff>\n## Goal\nShip it.\n</contextrail-handoff>')
  await clock.advance(500)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await ui.press({ key: 'yes' })
  await clock.settle()
  expect(seen.prompts[0]).toContain('Verify first')
  await ui.unmount()
})
