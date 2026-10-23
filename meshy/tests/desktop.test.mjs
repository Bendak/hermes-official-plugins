// Desktop behaviours that do not need a renderer: profile/connection switches, local vs remote backend,
// "Show in folder" on a remote backend, sending a model to a remote backend, and saved-format handling.
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { bestForTask, canRender, formatOf, mtlLibOf, mtlTextures, siblingPath } from '../src/formats.ts'
import { localHeading, reconcileSelection } from '../src/gallery.ts'
import { createLocalityTracker, localityOf, locatingOs, watchScope } from '../src/scope.ts'
import { createStatusStore, DEFAULT_SETTINGS } from '../src/status.ts'
import { JSON_UPLOAD_MAX_BYTES, UPLOAD_MAX_BYTES, uploadModel } from '../src/upload.ts'

/** nanostores-shaped atom: subscribe() calls back immediately with the current value, then on each set. */
function atom(value) {
  const listeners = new Set()
  return {
    get: () => value,
    subscribe(fn) {
      listeners.add(fn)
      fn(value)
      return () => listeners.delete(fn)
    },
    set(next) {
      value = next
      listeners.forEach(fn => fn(value))
    },
    listeners
  }
}

const tick = () => new Promise(resolve => setImmediate(resolve))

/** A backend per profile, like ctx.rest after the host re-routes it. */
function profileBackends(state, byProfile) {
  const calls = []
  const pending = []
  const rest = (path, _opts) => {
    const profile = state.profile.get()
    calls.push(`${profile}:${path}`)
    return new Promise((resolve, reject) => pending.push({ profile, resolve, reject, answer: byProfile[profile] }))
  }
  const answerAll = () => pending.splice(0).forEach(p => p.resolve(p.answer))
  return { rest, calls, pending, answerAll }
}

test('a profile switch re-reads the credit balance and settings from the new profile', async () => {
  const state = { profile: atom('default'), connectionId: atom('local') }
  const b = profileBackends(state, {
    default: { configured: true, balance: 900, settings: { show_credits: false, gallery_limit: 10 } },
    work: { configured: true, balance: 25, settings: { auto_rotate: false } }
  })
  const store = createStatusStore(b.rest, state)
  const seen = []
  store.onBalance(v => seen.push(v?.balance ?? null))
  void store.refresh()
  b.answerAll()
  await tick()
  assert.equal(store.balance().balance, 900)
  assert.equal(store.settings().show_credits, false)

  state.profile.set('work')
  // The old profile's numbers are gone at once (no "900 cr" under the work profile while it loads).
  assert.equal(store.balance(), null)
  assert.deepEqual(store.settings(), DEFAULT_SETTINGS)
  b.answerAll()
  await tick()
  assert.deepEqual(b.calls, ['default:status', 'work:status'])
  assert.equal(store.balance().balance, 25)
  assert.equal(store.settings().auto_rotate, false)
  assert.equal(store.settings().show_credits, true) // not carried over from the default profile
  assert.deepEqual(seen, [900, null, 25])
  store.dispose()
})

test('a reply from the old profile that lands after the switch is dropped', async () => {
  const state = { profile: atom('default'), connectionId: atom('local') }
  const b = profileBackends(state, { default: { configured: true, balance: 900 }, work: { configured: true, balance: 25 } })
  const store = createStatusStore(b.rest, state)
  void store.refresh() // in flight for "default"
  state.profile.set('work')
  const [old, fresh] = b.pending.splice(0)
  old.resolve(old.answer) // the profile we left answers first...
  await tick()
  assert.equal(store.balance(), null) // ...and is not shown under "work"
  fresh.resolve(fresh.answer)
  await tick()
  assert.equal(store.balance().balance, 25)
  // ...or last: still never painted over the new profile's numbers.
  void store.refresh()
  state.profile.set('default')
  state.profile.set('work')
  const [stale, current] = [b.pending.shift(), b.pending.pop()]
  current.resolve(current.answer)
  await tick()
  stale.resolve({ configured: true, balance: 900 })
  await tick()
  assert.equal(store.balance().balance, 25)
  store.dispose()
})

test('a connection switch with the same profile name also re-reads status', async () => {
  const state = { profile: atom('default'), connectionId: atom('local') }
  let n = 0
  const store = createStatusStore(async () => ({ configured: true, balance: ++n }), state)
  await store.refresh()
  state.connectionId.set('homelab')
  await tick()
  assert.equal(store.balance().balance, 2)
  store.dispose()
})

test('older Desktops (no host.state) keep one backend and never rescope', async () => {
  let n = 0
  const store = createStatusStore(async () => ({ configured: true, balance: ++n }), undefined)
  await store.refresh()
  assert.equal(store.balance().balance, 1)
  assert.doesNotThrow(() => watchScope(undefined, () => assert.fail('no scope to watch'))())
})

test('watchScope fires once per real switch, not on subscribe or repeats, and unsubscribes', () => {
  const state = { profile: atom('a'), connectionId: atom(null) }
  const keys = []
  const stop = watchScope(state, key => keys.push(key))
  state.profile.set('a')
  state.profile.set('b')
  state.connectionId.set('remote-1')
  assert.deepEqual(keys, ['\nb', 'remote-1\nb'])
  stop()
  assert.equal(state.profile.listeners.size, 0)
  assert.equal(state.connectionId.listeners.size, 0)
})

test('locality: primary and the local registry entry are local, any other source is remote', () => {
  const rows = [{ id: 'local', kind: 'local', label: 'This Mac' }, { id: 'homelab', kind: 'remote', label: 'Homelab' }, { id: 'box', kind: 'ssh' }]
  assert.deepEqual(localityOf({ connectionId: atom(null) }, rows), { kind: 'local' })
  assert.deepEqual(localityOf({ connectionId: atom('local') }, rows), { kind: 'local' })
  assert.deepEqual(localityOf({ connectionId: atom('homelab') }, rows), { kind: 'remote', label: 'Homelab' })
  assert.deepEqual(localityOf({ connectionId: atom('box') }, rows), { kind: 'remote', label: undefined })
  assert.deepEqual(localityOf({ connectionId: atom('homelab') }, null), { kind: 'remote', label: undefined })
  assert.deepEqual(localityOf({ connectionId: atom('mac-2') }, [{ id: 'mac-2', kind: 'local', label: 'Studio' }]), { kind: 'local', label: 'Studio' })
  assert.deepEqual(localityOf(undefined, rows), { kind: 'unknown' })
  assert.deepEqual(localityOf({ profile: atom('x') }, rows), { kind: 'unknown' })
})

test('the locality tracker reads labels from host.connections and follows a switch', async () => {
  const state = { profile: atom('default'), connectionId: atom('local') }
  const host = { state, connections: async () => [{ id: 'local', kind: 'local' }, { id: 'homelab', kind: 'remote', label: 'Homelab' }] }
  const tracker = createLocalityTracker(host)
  const seen = []
  tracker.subscribe(l => seen.push(l.kind))
  await tracker.refresh()
  assert.equal(tracker.get().kind, 'local')
  state.connectionId.set('homelab')
  await tracker.refresh()
  assert.deepEqual(tracker.get(), { kind: 'remote', label: 'Homelab' })
  assert.deepEqual(seen, ['remote'])
  // A Desktop without host.connections still answers from the id.
  const bare = createLocalityTracker({ state: { connectionId: atom('homelab') } })
  await bare.refresh()
  assert.equal(bare.get().kind, 'remote')
})

function fakeOs(revealed) {
  const calls = { reveal: [], clipboard: [] }
  return {
    calls,
    os: {
      revealPath: async path => (calls.reveal.push(path), revealed),
      writeClipboard: async text => (calls.clipboard.push(text), true),
      pickOpenPath: async () => null
    }
  }
}

test('remote backend: Show in folder never opens this computer\'s file manager; it says where the file is', async () => {
  const { os, calls } = fakeOs(true)
  const notes = []
  const wrapped = locatingOs(os, () => ({ kind: 'remote', label: 'Homelab' }), n => notes.push(n))
  assert.equal(await wrapped.revealPath('/srv/hermes/model.glb'), false)
  assert.deepEqual(calls.reveal, []) // the same path could name an unrelated local file
  assert.deepEqual(calls.clipboard, ['/srv/hermes/model.glb'])
  assert.equal(notes.length, 1)
  assert.match(notes[0].message, /on Homelab, not on this computer: \/srv\/hermes\/model\.glb \(path copied\)/)
})

test('local backend: a refused reveal (revealPath → false) is reported, not ignored', async () => {
  const notes = []
  const ok = fakeOs(true)
  assert.equal(await locatingOs(ok.os, () => ({ kind: 'local' }), n => notes.push(n)).revealPath('/h/m.glb'), true)
  assert.deepEqual(notes, [])
  const refused = fakeOs(false)
  assert.equal(await locatingOs(refused.os, () => ({ kind: 'local' }), n => notes.push(n)).revealPath('/h/m.glb'), false)
  assert.deepEqual(refused.calls.reveal, ['/h/m.glb'])
  assert.equal(notes.length, 1)
  assert.match(notes[0].message, /\/h\/m\.glb/)
  // An older Desktop (unknown locality) still tries the file manager first, then says where the file is.
  const old = fakeOs(false)
  delete old.os.writeClipboard
  const oldNotes = []
  await locatingOs(old.os, () => ({ kind: 'unknown' }), n => oldNotes.push(n)).revealPath('/h/m.glb')
  assert.deepEqual(old.calls.reveal, ['/h/m.glb'])
  assert.match(oldNotes[0].message, /\/h\/m\.glb on the machine Hermes runs on/)
})

test('the gallery heading names the backend machine, never "this machine" for a remote one', () => {
  assert.equal(localHeading({ kind: 'local' }), 'On this machine')
  assert.equal(localHeading({ kind: 'remote', label: 'Homelab' }), 'On Homelab')
  assert.equal(localHeading({ kind: 'remote' }), 'On the Hermes host')
  assert.equal(localHeading(undefined), 'Saved files')
  assert.equal(localHeading({ kind: 'unknown' }), 'Saved files')
})

const bytesOf = text => new TextEncoder().encode(text).buffer

test('upload sends the picked file as multipart bytes, never a local path', async () => {
  const calls = []
  const rest = async (path, opts) => (calls.push({ path, opts }), { path: '/srv/uploads/ab-robot.glb', name: 'robot.glb', bytes: 4 })
  const saved = await uploadModel(rest, { name: 'robot.glb', bytes: bytesOf('glTF'), type: '' })
  assert.equal(saved.path, '/srv/uploads/ab-robot.glb')
  assert.equal(calls.length, 1)
  assert.equal(calls[0].path, 'upload')
  assert.equal(calls[0].opts.method, 'POST')
  assert.equal(calls[0].opts.upload.filename, 'robot.glb')
  assert.equal(new TextDecoder().decode(calls[0].opts.upload.bytes), 'glTF')
  assert.equal(calls[0].opts.body, undefined)
})

test('an older Desktop (multipart dropped) or an OAuth remote falls back to base64 JSON', async () => {
  for (const refusal of ['400: {"detail":"no file in the request"}', 'File uploads are not supported against OAuth-gated remote backends yet.']) {
    const calls = []
    const rest = async (path, opts) => {
      calls.push(opts)
      if (opts.upload) throw new Error(refusal)
      return { path: '/srv/uploads/x.stl', name: 'x.stl', bytes: 5 }
    }
    await uploadModel(rest, { name: 'x.stl', bytes: bytesOf('solid'), type: 'model/stl' })
    assert.equal(calls.length, 2)
    assert.deepEqual(calls[1].body, { filename: 'x.stl', data: Buffer.from('solid').toString('base64') })
    assert.equal(calls[1].upload, undefined)
  }
})

test('upload never re-sends after an unknown outcome, and refuses oversize files before sending', async () => {
  let sent = 0
  const timeout = async () => {
    sent += 1
    throw new Error('Request timed out')
  }
  await assert.rejects(uploadModel(timeout, { name: 'a.glb', bytes: bytesOf('glTF') }), /timed out/)
  assert.equal(sent, 1)
  const huge = { byteLength: UPLOAD_MAX_BYTES + 1 }
  await assert.rejects(uploadModel(timeout, { name: 'a.glb', bytes: huge }), /up to 128 MB/)
  assert.equal(sent, 1)
  const midsize = new ArrayBuffer(JSON_UPLOAD_MAX_BYTES + 1)
  const old = async opts => {
    sent += 1
    throw new Error('400: {"detail":"no file in the request"}')
  }
  await assert.rejects(uploadModel((_p, o) => old(o), { name: 'a.glb', bytes: midsize }), /Update Hermes Desktop/)
})

test('saved formats: which render, and companions of an OBJ', () => {
  assert.equal(formatOf('/p/Robot.FBX'), 'fbx')
  assert.equal(formatOf('C:\\p\\crate.3mf'), '3mf')
  assert.equal(formatOf('/p/noext'), '')
  for (const f of ['glb', 'gltf', 'fbx', 'obj', 'stl', 'usdz', '3mf']) assert.ok(canRender(`/p/m.${f}`), f)
  assert.equal(canRender('/p/m.blend'), false)
  assert.equal(mtlLibOf('# Meshy\nmtllib robot.mtl\nv 0 0 0\n'), 'robot.mtl')
  assert.equal(mtlLibOf('v 0 0 0\n'), null)
  assert.deepEqual(mtlTextures('newmtl m\nmap_Kd robot_texture.png\nmap_Bump -bm 0.5 normal.png\nKd 1 1 1\n'), ['robot_texture.png', 'normal.png'])
  assert.equal(siblingPath('/home/u/game/robot.obj', 'robot.mtl'), '/home/u/game/robot.mtl')
  assert.equal(siblingPath('C:\\u\\game\\robot.obj', 'tex/robot.png'), 'C:\\u\\game\\tex\\robot.png')
  assert.equal(siblingPath('/home/u/game/robot.obj', '../../.ssh/id_rsa'), null)
  assert.equal(siblingPath('/home/u/game/robot.obj', '/etc/passwd'), null)
  assert.equal(siblingPath('/home/u/game/robot.obj', 'C:/x.png'), null)
})

test('a task with several saved formats opens its GLB, not the STL saved after it', () => {
  const files = [
    { path: '/p/crate.stl', task: 't' }, // newest first, like /local
    { path: '/p/crate.fbx', task: 't' },
    { path: '/p/crate.glb', task: 't' },
    { path: '/p/other.glb', task: 'u' }
  ]
  assert.equal(bestForTask(files, 't').path, '/p/crate.glb')
  assert.equal(bestForTask(files.slice(0, 2), 't').path, '/p/crate.fbx')
  assert.equal(bestForTask(files, 'none'), undefined)
  const saved = files.map(f => ({ kind: 'file', path: f.path, task: f.task }))
  assert.equal(reconcileSelection(null, 't', false, [], saved).path, '/p/crate.glb')
})
