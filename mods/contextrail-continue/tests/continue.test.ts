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
function engine(on: On, env: Record<string, string> = {}) {
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
    config: undefined as string | undefined, // ContextRail's config.json; undefined: there is none
    configPath: '/Users/test/.contextrail/config.json',
    draft: '', // what the person has typed in the composer
  }
  mock.env(on, { HOME: '/Users/test', ...env })
  on('fs.read', (_, e) => {
    if (e.path !== world.configPath || world.config === undefined) {
      return { deny: `ENOENT: no such file, open '${e.path}'` }
    }
    return { value: world.config }
  })
  on('prompt.read', () => ({ value: { text: world.draft, cursor: world.draft.length } }))
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

test('a checkpoint handoff offers no new session, and the final one does', async ($, on) => {
  const { clock, seen, world } = engine(on)
  world.stopAnswer = { block: `${REQUEST} This handoff is a checkpoint: work goes on in this session after it.` }
  await stop($, 'Done with the refactor.')
  world.stopAnswer = {}
  await stop($, HANDOFF)
  await clock.advance(600_000)
  expect(seen.opened).toEqual([])
  expect(seen.asked).toEqual([])
  expect(seen.commands).toEqual([])
  expect(seen.prompts).toEqual([])

  await requestThenSave($, world)
  await clock.advance(500)
  expect(seen.opened).toEqual(['contextrail-continue'])
})

test('a handoff that quotes its closing tag keeps its resume prompt', async ($, on) => {
  const { clock, seen, world } = engine(on)
  world.stopAnswer = { block: REQUEST }
  await stop($, 'ok')
  world.stopAnswer = {}
  await stop($, HANDOFF.replace('Ship the limiter.', 'Ship the limiter. A stray `</contextrail-handoff>` cut handoffs short.'))
  await clock.advance(500)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await ui.press({ key: 'yes' })
  await clock.settle()
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

const LIGHTNING = '{"lightning_tokens":200000}'
const LIGHTNING_PANE = {
  ...PANE,
  props: { ...PANE.props, title: '⚡ ContextRail', scroll: { offset: 0, bodyRows: 3 } },
} as const

test('Lightning Mode counts down in the pane and starts a new session at 0', async ($, on) => {
  const { clock, seen, world } = engine(on)
  world.config = LIGHTNING
  await requestThenSave($, world)
  await clock.advance(500)
  expect(seen.opened).toEqual(['contextrail-continue'])
  const ui = await $.ui.mount({ ...LIGHTNING_PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /starting a new session in 10 s/ })).toBeDefined()
  expect(await ui.findAll({ type: 'Button' })).toHaveLength(2)

  await clock.advance(3_000)
  expect(await ui.find({ type: 'Text', text: /starting a new session in 7 s/ })).toBeDefined()
  await clock.advance(6_000)
  expect(seen.commands).toEqual([])

  await clock.advance(1_000)
  await clock.settle()
  expect(seen.closed).toContain('contextrail-continue')
  expect(seen.commands).toEqual(['clear'])
  expect(seen.prompts).toEqual([RESUME])
  expect(seen.asked).toEqual([])
  await ui.unmount()
})

test('y in the Lightning pane starts the new session at once', async ($, on) => {
  const { clock, seen, world } = engine(on)
  world.config = LIGHTNING
  await requestThenSave($, world)
  await clock.advance(2_500)
  const ui = await $.ui.mount({ ...LIGHTNING_PANE, surface: 'terminal' })
  await ui.press({ key: 'now' })
  await clock.settle()
  expect(seen.commands).toEqual(['clear'])
  expect(seen.prompts).toEqual([RESUME])
  await clock.advance(60_000)
  expect(seen.commands).toEqual(['clear'])
  await ui.unmount()
})

test('n stops the countdown and keeps the session', async ($, on) => {
  const { clock, seen, world } = engine(on)
  world.config = LIGHTNING
  await requestThenSave($, world)
  await clock.advance(3_500)
  const ui = await $.ui.mount({ ...LIGHTNING_PANE, surface: 'terminal' })
  await ui.press({ key: 'stay' })
  expect(seen.closed).toContain('contextrail-continue')
  expect(seen.toasts.at(-1)).toContain('kept this session')
  await clock.advance(60_000)
  expect(seen.commands).toEqual([])
  await ui.unmount()
})

test('text in the composer when the countdown ends keeps the session', async ($, on) => {
  const { clock, seen, world } = engine(on)
  world.config = LIGHTNING
  await requestThenSave($, world)
  await clock.advance(500)
  world.draft = 'wait, one more'
  await clock.advance(10_000)
  await clock.settle()
  expect(seen.commands).toEqual([])
  expect(seen.closed).toContain('contextrail-continue')
  expect(seen.toasts.at(-1)).toContain('kept this session because you are typing')
  await clock.advance(60_000)
  expect(seen.commands).toEqual([])
})

test('a prompt from the person stops the countdown', async ($, on) => {
  const { clock, seen, world } = engine(on)
  world.config = LIGHTNING
  await requestThenSave($, world)
  await clock.advance(4_500)
  await $.prompt.submit({ text: 'one more thing', origin: { kind: 'composer' }, wait: false })
  expect(seen.closed).toContain('contextrail-continue')
  await clock.advance(60_000)
  expect(seen.commands).toEqual([])
})

test('Lightning counts down in a toast where no pane can be shown', async ($, on) => {
  const { clock, seen, world } = engine(on)
  world.config = LIGHTNING
  world.isPlaced = false
  await requestThenSave($, world)
  await clock.advance(500)
  await clock.settle()
  expect(seen.closed).toContain('contextrail-continue')
  expect(seen.toasts.at(-1)).toContain('Lightning Mode: handoff saved. Starting a new session in 10 s')
  expect(seen.asked).toEqual([])
  await clock.advance(9_000)
  expect(seen.commands).toEqual([])
  await clock.advance(1_000)
  await clock.settle()
  expect(seen.commands).toEqual(['clear'])
  expect(seen.prompts).toEqual([RESUME])
})

// Whether Lightning is on, read as ContextRail's config reads lightning_tokens:
// the environment variable when it holds a usable value, else the global config.json.
const READINGS: [string, Record<string, string>, string | undefined, boolean][] = [
  ['no config.json', {}, undefined, false],
  ['a whole number', {}, LIGHTNING, true],
  ['0', {}, '{"lightning_tokens":0}', false],
  ['a fraction, cut to a whole number', {}, '{"lightning_tokens":200000.5}', true],
  ['a fraction below 1', {}, '{"lightning_tokens":0.5}', false],
  ['a negative number', {}, '{"lightning_tokens":-200000}', false],
  ['digits in a string', {}, '{"lightning_tokens":" 200_000 "}', true],
  ['a string in exponent form', {}, '{"lightning_tokens":"2e5"}', false],
  ['a string with a unit', {}, '{"lightning_tokens":"200k"}', false],
  ['true', {}, '{"lightning_tokens":true}', false],
  ['null', {}, '{"lightning_tokens":null}', false],
  ['a config.json that is not JSON', {}, '{"lightning_tokens":200000', false],
  ['the variable over config.json', { CONTEXTRAIL_LIGHTNING_TOKENS: '0' }, LIGHTNING, false],
  ['the variable alone', { CONTEXTRAIL_LIGHTNING_TOKENS: ' 150000 ' }, undefined, true],
  ['config.json under an unusable variable', { CONTEXTRAIL_LIGHTNING_TOKENS: 'lots' }, LIGHTNING, true],
  ['an exponent in the variable', { CONTEXTRAIL_LIGHTNING_TOKENS: '2e5' }, '{"lightning_tokens":0}', false],
  ['config.json under an empty CONTEXTRAIL_HOME', { CONTEXTRAIL_HOME: '' }, LIGHTNING, true],
]

for (const [label, env, config, isOn] of READINGS) {
  test(`Lightning is ${isOn ? 'on' : 'off'} with ${label}`, async ($, on) => {
    const { clock, seen, world } = engine(on, env)
    world.config = config
    world.isPlaced = false
    world.answer = 'Keep this session'
    await requestThenSave($, world)
    await clock.advance(500)
    await clock.settle()
    expect(seen.asked).toHaveLength(isOn ? 0 : 1)
    expect(seen.toasts.some(toast => toast.includes('Lightning Mode'))).toBe(isOn)
  })
}

test('Lightning reads config.json from CONTEXTRAIL_HOME', async ($, on) => {
  const { clock, seen, world } = engine(on, { CONTEXTRAIL_HOME: '~/state/contextrail' })
  world.config = LIGHTNING
  world.configPath = '/Users/test/state/contextrail/config.json'
  world.isPlaced = false
  await requestThenSave($, world)
  await clock.advance(500)
  await clock.settle()
  expect(seen.asked).toEqual([])
  expect(seen.toasts.at(-1)).toContain('Lightning Mode')
})
