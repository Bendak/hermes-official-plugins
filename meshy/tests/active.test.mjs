// Progress chip polling (src/active.ts): which tasks it asks the backend about, and when it stops.
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { ACTIVE_POLL_MS, awaitedTasks, createProgressTracker, MAX_FAILURES, startedTasks } from '../src/active.ts'
import { EARLY_404_MS } from '../src/task-poll.ts'

const ID = '01a157b8-7317-7551-abc5-000000000001'
const ID2 = '01a157b8-7317-7551-abc5-000000000002'

function rig({ foreign = () => false } = {}) {
  let now = 1_000_000
  let timers = []
  let seq = 0
  const clock = {
    now: () => now,
    setTimeout: (fn, ms) => {
      const t = { id: ++seq, at: now + ms, fn }
      timers.push(t)
      return t.id
    },
    clearTimeout: id => {
      timers = timers.filter(t => t.id !== id)
    }
  }
  const handlers = {}
  const calls = []
  let reply = () => ({ tasks: [], done: [], missing: [] })
  const ctx = {
    onEvent: (type, fn) => {
      handlers[type] = fn
      return () => delete handlers[type]
    },
    rest: path => {
      calls.push(path)
      try {
        return Promise.resolve(reply(path))
      } catch (err) {
        return Promise.reject(err)
      }
    }
  }
  const states = []
  let idles = 0
  const tracker = createProgressTracker(ctx, s => states.push(s), () => idles++, { clock, foreign })
  const flush = () => new Promise(resolve => setImmediate(resolve))
  return {
    tracker,
    calls,
    states,
    idles: () => idles,
    setReply: fn => {
      reply = fn
    },
    emit: (type, payload, extra = {}) => handlers[type]?.({ payload, ...extra }),
    pending: () => timers.length,
    /** Advance the clock by `ms`, running due timers and letting their promises settle. */
    async advance(ms) {
      const end = now + ms
      for (;;) {
        timers.sort((a, b) => a.at - b.at)
        const t = timers[0]
        if (!t || t.at > end) break
        timers.shift()
        now = t.at
        t.fn()
        await flush()
        await flush()
      }
      now = end
    }
  }
}

const createResult = id => JSON.stringify({ results: [{ result: `Created.\n**Task ID**: ${id}` }] })
const bridged = (name, args = {}) => ({ calls: [{ name: `mcp_meshy_meshy_${name}`, arguments: args }] })
const running = (id, progress) => ({ task_id: id, task_type: 'text-to-3d', stage: 'preview', status: 'IN_PROGRESS', progress, prompt: 'duck' })

test('an idle window never polls', async () => {
  const r = rig()
  await r.advance(10 * 60_000)
  assert.equal(r.calls.length, 0)
  assert.equal(r.pending(), 0)
})

test('a create call is watched by id, one task per poll, until Meshy reports it done', async () => {
  const r = rig()
  r.emit('tool.start', { tool_id: 'c1', name: 'tool_call', args: bridged('text_to_3d', { prompt: 'duck' }) })
  await r.advance(5000)
  assert.equal(r.calls.length, 0, 'nothing to read while the create call runs')
  assert.equal(r.states.at(-1).running, true)

  let progress = 10
  r.setReply(() => ({ tasks: [running(ID, progress)], done: [], missing: [] }))
  r.emit('tool.complete', { tool_id: 'c1', name: 'tool_call', args: bridged('text_to_3d'), result: createResult(ID) })
  await r.advance(1000)
  assert.deepEqual(r.calls, [`active?tasks=${encodeURIComponent(`text-to-3d:${ID}`)}`])
  assert.equal(r.states.at(-1).tasks[0].progress, 10)

  progress = 60
  await r.advance(ACTIVE_POLL_MS)
  assert.equal(r.calls.length, 2)
  assert.equal(r.states.at(-1).tasks[0].progress, 60)

  const idles = r.idles()
  r.setReply(() => ({ tasks: [], done: [ID], missing: [] }))
  await r.advance(ACTIVE_POLL_MS)
  assert.equal(r.states.at(-1).tasks.length, 0)
  assert.equal(r.idles(), idles + 1, 'the balance refreshes once the task is over')
  await r.advance(10 * 60_000)
  assert.equal(r.calls.length, 3, 'no polls after the last task finished')
})

test('a status call names the task it waits on, with its type', () => {
  assert.deepEqual(awaitedTasks('tool_call', bridged('get_task_status', { task_id: ID, task_type: 'rigging' })), [['rigging', ID]])
  assert.deepEqual(awaitedTasks('mcp_meshy_meshy_get_task_status', { task_id: ID }), [['any', ID]])
  // Meshy's server tolerates a wrong task_type; the backend then looks the task up instead of refusing the batch.
  assert.deepEqual(awaitedTasks('mcp_meshy_meshy_get_task_status', { task_id: ID, task_type: 'preview' }), [['any', ID]])
  assert.deepEqual(awaitedTasks('mcp_meshy_meshy_get_task_status', { task_id: 'not-an-id' }), [])
  assert.deepEqual(startedTasks('mcp_meshy_meshy_rig', {}, `**Task ID**: ${ID}`), [['rigging', ID]])
  assert.deepEqual(startedTasks('mcp_meshy_meshy_check_balance', {}, `**Task ID**: ${ID}`), [])
})

test('a refused key stops polling until the next Meshy call', async () => {
  const r = rig()
  r.setReply(() => {
    throw new Error("Error invoking remote method 'hermes:api': Error: 403: {\"detail\":\"Meshy refused the request: ip_not_authorized\"}")
  })
  r.emit('tool.start', { tool_id: 's1', name: 'mcp_meshy_meshy_get_task_status', args: { task_id: ID, task_type: 'text-to-3d' } })
  await r.advance(60_000)
  assert.equal(r.calls.length, 1)
  assert.equal(r.pending(), 0)

  r.setReply(() => ({ tasks: [running(ID, 5)], done: [], missing: [] }))
  r.emit('tool.complete', { tool_id: 's1', name: 'mcp_meshy_meshy_get_task_status', args: { task_id: ID } })
  await r.advance(1000)
  assert.equal(r.calls.length, 2, 're-armed by the next Meshy tool event')
})

test('an outage backs off and gives up after MAX_FAILURES in a row', async () => {
  const r = rig()
  r.setReply(() => {
    throw new Error('502: {"detail":"Meshy API unreachable"}')
  })
  r.emit('tool.complete', { name: 'mcp_meshy_meshy_text_to_3d', args: {}, result: `**Task ID**: ${ID}` })
  await r.advance(60 * 60_000 - 1)
  assert.equal(r.calls.length, MAX_FAILURES)
  assert.equal(r.pending(), 0)
})

test('an id the route does not know yet is kept for the early window, then dropped', async () => {
  const r = rig()
  r.setReply(() => ({ tasks: [], done: [], missing: [ID] }))
  r.emit('tool.complete', { name: 'mcp_meshy_meshy_text_to_3d', args: {}, result: `**Task ID**: ${ID}` })
  await r.advance(EARLY_404_MS - 5000)
  const early = r.calls.length
  assert.ok(early > 10)
  await r.advance(60_000)
  assert.equal(r.tracker.watching().length, 0)
  const after = r.calls.length
  await r.advance(10 * 60_000)
  assert.equal(r.calls.length, after)
})

test("another profile's task is not polled through the front profile's key", async () => {
  const r = rig({ foreign: o => o?.profile === 'other' })
  r.setReply(() => ({ tasks: [running(ID2, 20)], done: [], missing: [] }))
  r.emit('tool.complete', { name: 'mcp_meshy_meshy_text_to_3d', args: {}, result: `**Task ID**: ${ID}` }, { profile: 'other' })
  r.emit('tool.complete', { name: 'mcp_meshy_meshy_text_to_3d', args: {}, result: `**Task ID**: ${ID2}` }, { profile: 'default' })
  await r.advance(1000)
  assert.deepEqual(r.calls, [`active?tasks=${encodeURIComponent(`text-to-3d:${ID2}`)}`])
})

test('a reply read through the previous profile is dropped after a switch', async () => {
  const r = rig()
  let release
  r.setReply(() => new Promise(resolve => (release = resolve)))
  r.emit('tool.complete', { name: 'mcp_meshy_meshy_text_to_3d', args: {}, result: `**Task ID**: ${ID}` })
  await r.advance(1000)
  assert.equal(r.calls.length, 1)
  r.tracker.rescope()
  release({ tasks: [running(ID, 50)], done: [], missing: [] })
  for (let i = 0; i < 3; i++) await new Promise(resolve => setImmediate(resolve))
  assert.equal(r.states.at(-1).tasks.length, 0)
})
