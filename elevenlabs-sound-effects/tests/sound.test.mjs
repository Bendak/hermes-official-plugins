// node --experimental-strip-types --test tests/sound.test.mjs
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { isSoundToolCall, matches, nextActions, ratePerSecond } from '../src/sound.ts'

const rec = over => ({
  id: 'abcdef012345',
  created_at: '2026-10-23T12:00:00+00:00',
  prompt: 'Glass bottle shattering on a stone floor',
  duration_seconds: 2,
  loop: false,
  prompt_influence: 0.3,
  model: 'eleven_text_to_sound_v2',
  output_format: 'mp3_44100_128',
  file_name: 'sfx_glass_20261023_120000_abcdef012345.mp3',
  bytes: 33061,
  billed_characters: '20',
  session_id: '20261023_120000_aaaaaa',
  kind: 'mp3',
  exists: true,
  path: '/h/cache/generated/audio/x.mp3',
  size: 33061,
  ...over
})

// Exactly the tool schema's argument names (elevenlabs_sound_effect): anything else is a refused call.
const SCHEMA_ARGS = new Set(['prompt', 'duration_seconds', 'loop', 'prompt_influence', 'model'])

test('every action quotes the cost from this sound’s bill and sends only schema arguments', () => {
  const actions = nextActions(rec())
  assert.deepEqual(actions.map(a => a.id), ['variations', 'shorter', 'longer', 'loop', 'model'])
  const byId = Object.fromEntries(actions.map(a => [a.id, a]))
  assert.match(byId.variations.cost, /60 credits total \(3 × 20 credits\)/)
  assert.match(byId.shorter.label, /1 s/)
  assert.match(byId.shorter.cost, /≈ 10 credits \(10\/s/)
  assert.match(byId.longer.cost, /≈ 40 credits/)
  assert.match(byId.loop.message, /loop true/)
  assert.match(byId.model.label, /Try v3/)
  assert.match(byId.model.message, /model eleven_text_to_sound_v3/)
  for (const a of actions) {
    assert.match(a.message, /elevenlabs_sound_effect/)
    const names = [...a.message.matchAll(/\b([a-z]+(?:_[a-z]+)*) (?=["\d]|true|false|eleven_)/g)].map(m => m[1])
    for (const n of names.filter(n => n.includes('_') || n === 'prompt' || n === 'loop' || n === 'model')) {
      assert.ok(SCHEMA_ARGS.has(n), `${a.id} sends ${n}`)
    }
    assert.doesNotMatch(a.message, /\b(seed|output_format|format|model_id|text)\b(?= )/)
  }
})

test('an auto-length sound never claims a per-second price it cannot know', () => {
  const r = rec({ duration_seconds: null, billed_characters: '50' })
  assert.equal(ratePerSecond(r), null)
  const actions = nextActions(r, 2.0)
  const byId = Object.fromEntries(actions.map(a => [a.id, a]))
  assert.match(byId.variations.cost, /150 credits total \(3 × 50 credits\)/)
  assert.match(byId.shorter.cost, /rate is unknown/)
  assert.doesNotMatch(byId.variations.message, /duration_seconds/)
  assert.match(byId.shorter.message, /duration_seconds 1/)
})

test('bounds and loops: no shorter below 0.5 s, no longer past 30 s, no make-it-loop on a loop', () => {
  assert.ok(!nextActions(rec({ duration_seconds: 0.5, billed_characters: '5' })).some(a => a.id === 'shorter'))
  assert.ok(!nextActions(rec({ duration_seconds: 30, billed_characters: '300' })).some(a => a.id === 'longer'))
  const loop = nextActions(rec({ loop: true, model: 'eleven_text_to_sound_v3' }))
  assert.ok(!loop.some(a => a.id === 'loop'))
  assert.match(loop.find(a => a.id === 'model').label, /Try v2/)
  assert.deepEqual(nextActions(rec({ prompt: '' })), [])
})

test('a missing bill is said plainly', () => {
  const v = nextActions(rec({ billed_characters: null }))[0]
  assert.match(v.cost, /cost unknown/)
})

test('tool-call detection covers direct calls and tool search wrappers', () => {
  assert.ok(isSoundToolCall('elevenlabs_sound_effect', {}))
  assert.ok(isSoundToolCall('tool_call', { name: 'elevenlabs_sound_effect' }))
  assert.ok(isSoundToolCall('tool_call', { calls: [{ name: 'x' }, { name: 'elevenlabs_sound_effect' }] }))
  assert.ok(!isSoundToolCall('tool_call', { name: 'web_search' }))
  assert.ok(!isSoundToolCall('terminal', null))
})

test('library search matches every term', () => {
  assert.ok(matches(rec(), 'glass stone'))
  assert.ok(matches(rec(), 'v2 mp3'))
  assert.ok(!matches(rec(), 'glass rain'))
})
