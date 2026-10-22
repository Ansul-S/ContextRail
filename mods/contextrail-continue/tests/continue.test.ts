import { expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

const REQUEST = 'ContextRail: this session is at 102,000 tokens of context. The user set up ContextRail...'
const FIX = 'ContextRail: the handoff needs fixing before it can be saved'
const HANDOFF = [
  'Here is the handoff.',
  '<contextrail-handoff>',
  '## Goal',
  'Ship the limiter.',
  '## Resume prompt',
  'Continue the API rate limiting in api/app.py.',
  '</contextrail-handoff>',
].join('\n')

/**
 * Stands in for the engine and for ContextRail's own Stop hook beneath the mod,
 * and records what the mod asks of the session.
 */
function engine(on: On) {
  const clock = mock.clock(on)
  const seen = { commands: [] as string[], prompts: [] as string[], toasts: [] as string[] }
  const world = { stopAnswer: {} as { block?: string }, draft: '' }
  on('classic.Stop', () => world.stopAnswer)
  on('command.run', (_, e) => {
    seen.commands.push(e.command)
    return {}
  })
  on('prompt.submit', (_, e) => {
    seen.prompts.push(e.text)
    return { text: e.text }
  })
  on('prompt.read', () => ({ value: { text: world.draft, cursor: world.draft.length } }))
  on('ui.toast', (_, e) => {
    seen.toasts.push(e.text)
    return { value: undefined }
  })
  return { clock, seen, world }
}

async function stop($: Engine, message: string, extra: { background_tasks?: never[] } = {}) {
  return $.classic.Stop({ stop_hook_active: false, last_assistant_message: message, ...extra })
}

async function requestThenSave($: Engine, world: { stopAnswer: { block?: string } }) {
  world.stopAnswer = { block: REQUEST }
  await stop($, 'Done with the refactor.')
  world.stopAnswer = {}
  await stop($, HANDOFF)
}

test('clears and continues with the resume prompt after ContextRail asked', async ($, on) => {
  const { clock, seen, world } = engine(on)
  await requestThenSave($, world)
  expect(seen.toasts.at(-1)).toContain('Clearing and continuing in 10 s')

  await clock.advance(9_999)
  expect(seen.commands).toEqual([])
  await clock.advance(1)
  expect(seen.commands).toEqual(['clear'])
  expect(seen.prompts).toEqual(['Continue the API rate limiting in api/app.py.'])
})

test('waits while ContextRail asks for a fix', async ($, on) => {
  const { clock, seen, world } = engine(on)
  world.stopAnswer = { block: REQUEST }
  await stop($, 'ok')
  world.stopAnswer = { block: FIX }
  await stop($, HANDOFF)
  await clock.advance(60_000)
  expect(seen.commands).toEqual([])

  world.stopAnswer = {}
  await stop($, HANDOFF)
  await clock.advance(10_000)
  expect(seen.commands).toEqual(['clear'])
})

test('leaves a handoff the person asked for alone', async ($, on) => {
  const { clock, seen, world } = engine(on)
  world.stopAnswer = { block: 'Another plugin: run the linter before stopping.' }
  await stop($, 'ok')
  world.stopAnswer = {}
  await stop($, HANDOFF)
  await clock.advance(60_000)
  expect(seen.commands).toEqual([])
})

test('continues after a handoff the person asked for when after_manual is on', { options: { after_manual: true, delay_seconds: 0 } }, async ($, on) => {
  const { clock, seen } = engine(on)
  await stop($, HANDOFF)
  await clock.settle()
  expect(seen.commands).toEqual(['clear'])
})

test('a new turn or a draft in the prompt box keeps the session', async ($, on) => {
  const { clock, seen, world } = engine(on)
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  await requestThenSave($, world)
  await $.turn.start({ text: 'one more thing', turnId: 't2' })
  await clock.advance(60_000)
  expect(seen.commands).toEqual([])

  await requestThenSave($, world)
  world.draft = 'wait, first'
  await clock.advance(10_000)
  expect(seen.commands).toEqual([])
  expect(seen.toasts.at(-1)).toContain('kept this session because you are typing')
})

test('background tasks hold it', async ($, on) => {
  const { clock, seen, world } = engine(on)
  world.stopAnswer = { block: REQUEST }
  await stop($, 'ok')
  world.stopAnswer = {}
  await $.classic.Stop({
    stop_hook_active: true,
    last_assistant_message: HANDOFF,
    background_tasks: [{ id: 'b1', type: 'shell', status: 'running', description: 'npm test' }],
  })
  await clock.advance(60_000)
  expect(seen.commands).toEqual([])
  expect(seen.toasts.at(-1)).toContain('Not clearing while background or scheduled tasks are pending')
})

test('stops continuing on its own after max_in_a_row until the person writes', { options: { max_in_a_row: 1 } }, async ($, on) => {
  const { clock, seen, world } = engine(on)
  await requestThenSave($, world)
  await clock.advance(10_000)
  expect(seen.commands).toEqual(['clear'])

  await requestThenSave($, world)
  await clock.advance(10_000)
  expect(seen.commands).toEqual(['clear'])
  expect(seen.toasts.at(-1)).toContain('Continued 1 times in a row')

  await $.prompt.submit({ text: 'keep going', origin: { kind: 'composer' }, wait: false })
  await requestThenSave($, world)
  await clock.advance(10_000)
  expect(seen.commands).toEqual(['clear', 'clear'])
})

test('falls back to a generic prompt when the handoff has no resume prompt', async ($, on) => {
  const { clock, seen, world } = engine(on)
  world.stopAnswer = { block: REQUEST }
  await stop($, 'ok')
  world.stopAnswer = {}
  await stop($, '<contextrail-handoff>\n## Goal\nShip it.\n</contextrail-handoff>')
  await clock.advance(10_000)
  expect(seen.prompts[0]).toContain('Verify first')
})
