// Audio decode, waveform peaks and gapless playback (Web Audio), plus the host doors the card and the
// library share: where the Hermes backend runs, sending a follow-up to the right chat, saving a download.

import { host, type PluginContext } from '@hermes/plugin-sdk'

export interface AudioPayload {
  id: string
  format: string
  mime: string
  bytes: number
  file_name: string
  data_url: string
}

let audioContext: AudioContext | null = null
const context = (): AudioContext => (audioContext ??= new AudioContext())

export async function decode(dataUrl: string): Promise<AudioBuffer> {
  const bytes = await (await fetch(dataUrl)).arrayBuffer()
  return context().decodeAudioData(bytes)
}

/** Max |sample| per bucket across channels, 0..1. */
export function peaks(buffer: AudioBuffer, buckets = 120): number[] {
  const out = new Array<number>(buckets).fill(0)
  const size = Math.max(1, Math.floor(buffer.length / buckets))
  for (let c = 0; c < buffer.numberOfChannels; c++) {
    const data = buffer.getChannelData(c)
    for (let b = 0; b < buckets; b++) {
      let max = out[b]
      const end = Math.min(data.length, (b + 1) * size)
      for (let i = b * size; i < end; i += 4) {
        const v = Math.abs(data[i])
        if (v > max) max = v
      }
      out[b] = max
    }
  }
  return out
}

type Listener = () => void

/**
 * One sound plays at a time across every card and the library. AudioBufferSourceNode with `loop` is
 * gapless, which is what a loop audition needs (an <audio loop> element leaves a gap at the seam).
 */
class Player {
  private source: AudioBufferSourceNode | null = null
  private startedAt = 0
  private offset = 0
  owner: null | string = null
  buffer: AudioBuffer | null = null
  loop = false
  private listeners = new Set<Listener>()

  subscribe(fn: Listener): () => void {
    this.listeners.add(fn)
    return () => this.listeners.delete(fn)
  }

  private emit() {
    this.listeners.forEach(fn => fn())
  }

  playing(owner: string): boolean {
    return this.owner === owner && this.source !== null
  }

  position(owner: string): number {
    if (this.owner !== owner || !this.buffer) return 0
    if (!this.source) return this.offset
    const t = this.offset + (context().currentTime - this.startedAt)
    return this.loop ? t % this.buffer.duration : Math.min(t, this.buffer.duration)
  }

  play(owner: string, buffer: AudioBuffer, loop: boolean, from = 0) {
    this.stopSource()
    if (this.owner !== owner) this.offset = 0
    this.owner = owner
    this.buffer = buffer
    this.loop = loop
    const ctx = context()
    void ctx.resume()
    const node = ctx.createBufferSource()
    node.buffer = buffer
    node.loop = loop
    node.connect(ctx.destination)
    const start = from >= buffer.duration ? 0 : from
    node.onended = () => {
      if (this.source === node) {
        this.source = null
        this.offset = 0
        this.emit()
      }
    }
    node.start(0, start)
    this.source = node
    this.startedAt = ctx.currentTime
    this.offset = start
    this.emit()
  }

  pause(owner: string) {
    if (this.owner !== owner || !this.source) return
    this.offset = this.position(owner)
    this.stopSource()
    this.emit()
  }

  setLoop(owner: string, loop: boolean) {
    if (this.owner !== owner) return
    if (this.source && this.buffer) {
      this.play(owner, this.buffer, loop, this.position(owner))
    } else {
      this.loop = loop
      this.emit()
    }
  }

  release(owner: string) {
    if (this.owner !== owner) return
    this.stopSource()
    this.owner = null
    this.buffer = null
    this.offset = 0
    this.emit()
  }

  private stopSource() {
    const node = this.source
    this.source = null
    if (node) {
      node.onended = null
      try {
        node.stop()
      } catch {
        // already stopped
      }
      node.disconnect()
    }
  }

  dispose() {
    this.stopSource()
    this.listeners.clear()
    void audioContext?.close()
    audioContext = null
  }
}

export const player = new Player()

/**
 * Is the backend `ctx.rest` talks to on another machine? `true` / `false` when the Desktop can tell,
 * `null` when it can't (older build without the connection registry). Callers treat `null` as "maybe":
 * they still offer Reveal and explain when the OS could not find the path.
 */
export async function backendIsRemote(): Promise<boolean | null> {
  try {
    const active =
      typeof host.activeConnectionId === 'function' ? host.activeConnectionId() : (host.state?.connectionId?.get() ?? null)
    if (typeof host.connections !== 'function') return null
    const rows = await host.connections()
    const row = active ? rows.find(r => r.id === active) : rows.find(r => r.primary)
    if (!row) return active ? null : false
    return row.kind !== undefined ? row.kind !== 'local' : null
  } catch {
    return null
  }
}

export interface ScopeKey {
  profile: string
  connection: string
}

export const currentScope = (): ScopeKey => ({
  profile: host.state?.profile?.get() ?? '',
  connection: host.state?.connectionId?.get() ?? ''
})

/** Re-run `fn` when the active profile or connection changes (ctx.rest then reaches another backend). */
export function onScopeChange(fn: () => void): () => void {
  const offs = [host.state?.profile?.subscribe(() => fn()), host.state?.connectionId?.subscribe(() => fn())]
  return () => offs.forEach(off => off?.())
}

/**
 * Send a card's follow-up into the chat that shows the card. Hosts with the directive-owner seam pass
 * `sessionId` (a string, or null for a chat with no session yet) and `host.composer.submit` fails closed
 * when that chat's composer is not visible; `undefined` means an older host, where the active composer
 * (null) is the only address there is. The id is captured when the card renders and the submit runs
 * synchronously on click, before any await, so moving to another chat cannot redirect it.
 */
export function sendToChat(directiveSession: null | string | undefined, text: string): boolean {
  const submit = host.composer?.submit
  if (typeof submit !== 'function') return false
  return submit(directiveSession !== undefined ? directiveSession : null, text)
}

/** Bytes for one format through the plugin backend (works against a remote Hermes too). */
export const fetchAudio = (ctx: PluginContext, id: string, format: string) =>
  ctx.rest<AudioPayload>(`audio?id=${encodeURIComponent(id)}&format=${encodeURIComponent(format)}`, { timeoutMs: 60_000 })

/** Hand the bytes to the Desktop's download handler (Save dialog in Downloads on THIS machine). */
export async function saveDownload(payload: AudioPayload): Promise<void> {
  const blob = await (await fetch(payload.data_url)).blob()
  const url = URL.createObjectURL(blob)
  const link = Object.assign(document.createElement('a'), { href: url, download: payload.file_name })
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 60_000)
}

/** `ctx.rest` rejects with "<status>: <body>"; show FastAPI's `detail` instead of raw JSON. */
export function errorText(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err)
  const m = /^(\d{3}):\s*(.*)$/s.exec(raw)
  if (m) {
    let detail: unknown
    try {
      detail = (JSON.parse(m[2]) as { detail?: unknown }).detail
    } catch {
      // not JSON
    }
    // The host's own 404s (the route is not mounted for this profile), not one of this plugin's details.
    if (m[1] === '404' && (detail === undefined || detail === 'Not Found' || detail === 'Plugin not found')) {
      return 'ElevenLabs Sound Effects is not enabled in this profile’s Hermes (or its backend has not loaded it yet). Install or enable it for this profile; a running backend may need a restart.'
    }
    if (typeof detail === 'string') return detail
    return `${m[1]}: ${m[2].slice(0, 200)}`
  }
  return raw.slice(0, 300)
}

export const statusOf = (err: unknown): number => {
  const m = /^(\d{3}):/.exec(err instanceof Error ? err.message : String(err))
  return m ? Number(m[1]) : 0
}
