// Pure helpers shared by the sound card and the library: record shape, labels, and the Next actions.
// No SDK imports here, so `node --test` can exercise this file directly.

export interface SoundRecord {
  id: string
  created_at: null | string
  prompt: null | string
  duration_seconds: null | number
  loop: boolean | null
  prompt_influence: null | number
  model: null | string
  output_format: null | string
  file_name: null | string
  bytes: null | number
  billed_characters: null | string
  session_id: null | string
  /** Added by the backend: container the tool wrote (mp3 | opus | wav). */
  kind: string
  exists: boolean
  /** Absolute path on the Hermes host (which may be another machine). */
  path: null | string
  size: null | number
}

export interface HostInfo {
  host: string
  ffmpeg: boolean
  conversions: string[]
}

export const MODELS = ['eleven_text_to_sound_v2', 'eleven_text_to_sound_v3'] as const
export const TOOL = 'elevenlabs_sound_effect'
/** Every container the tool can write (plugin.yaml output_format choices). */
export const KINDS = ['mp3', 'opus', 'wav'] as const

export const modelLabel = (model: null | string): string =>
  model === 'eleven_text_to_sound_v3' ? 'v3' : model === 'eleven_text_to_sound_v2' ? 'v2' : model || 'model ?'

export const kindLabel = (kind: string): string => (kind === 'opus' ? 'Ogg Opus' : kind.toUpperCase() || '?')

export function formatLabel(format: null | string): string {
  const m = /^(mp3|opus|wav)_(\d+)(?:_(\d+))?$/.exec(format || '')
  if (!m) return format || '?'
  const rate = `${Number(m[2]) / 1000} kHz`
  return m[3] ? `${kindLabel(m[1])} ${rate} ${m[3]} kbps` : `${kindLabel(m[1])} ${rate}`
}

export function bytesLabel(n: null | number): string {
  if (!n || n < 0) return '0 B'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

export const secondsLabel = (s: null | number | undefined): string =>
  typeof s === 'number' && Number.isFinite(s) ? `${s < 10 ? s.toFixed(1).replace(/\.0$/, '') : Math.round(s)} s` : 'auto length'

/** Credits the record was billed, from ElevenLabs' `character-cost` header (null when it sent none). */
export function billed(rec: Pick<SoundRecord, 'billed_characters'>): null | number {
  const n = Number(rec.billed_characters)
  return rec.billed_characters != null && Number.isFinite(n) && n >= 0 ? n : null
}

/** Credits per second, derived only from a sound that had an explicit duration (an auto-length sound is
 *  billed a flat amount per generation, ~50 credits measured, so it says nothing about the per-second
 *  price). */
export function ratePerSecond(rec: Pick<SoundRecord, 'billed_characters' | 'duration_seconds'>): null | number {
  const b = billed(rec)
  const d = rec.duration_seconds
  return b != null && typeof d === 'number' && d > 0 ? b / d : null
}

export interface NextAction {
  id: 'longer' | 'loop' | 'model' | 'shorter' | 'variations'
  label: string
  /** Cost line shown on the row, from this sound's real bill. */
  cost: string
  /** The chat message the row sends (the agent runs the tool; its approval and billing apply). */
  message: string
}

interface ToolArgs {
  prompt: string
  duration_seconds?: number
  loop: boolean
  prompt_influence?: number
  model: string
}

const round1 = (n: number) => Math.round(n * 10) / 10
const clampDuration = (n: number) => Math.min(30, Math.max(0.5, round1(n)))
const credits = (n: number) => `${Math.round(n)} credit${Math.round(n) === 1 ? '' : 's'}`

/** Only argument names the tool schema accepts, in the order the model reads them. */
export function argsText(args: ToolArgs): string {
  const parts = [`prompt ${JSON.stringify(args.prompt)}`]
  if (args.duration_seconds != null) parts.push(`duration_seconds ${args.duration_seconds}`)
  parts.push(`loop ${args.loop}`)
  if (args.prompt_influence != null) parts.push(`prompt_influence ${args.prompt_influence}`)
  parts.push(`model ${args.model}`)
  return parts.join(', ')
}

function costFor(rec: SoundRecord, seconds: null | number): { line: string; total: null | number } {
  const rate = ratePerSecond(rec)
  const b = billed(rec)
  if (rate != null && seconds != null) {
    return { line: `≈ ${credits(rate * seconds)} (${round1(rate)}/s, from this sound's bill)`, total: rate * seconds }
  }
  if (seconds == null && b != null) {
    return { line: `≈ ${credits(b)} (what this auto-length sound billed)`, total: b }
  }
  if (b != null) {
    return { line: `billed per second; this auto-length sound billed ${credits(b)}, so the rate is unknown`, total: null }
  }
  return { line: 'cost unknown (ElevenLabs reported no bill for this sound)', total: null }
}

/**
 * The card's Next menu. Each row's message names the tool and only arguments its schema accepts
 * (prompt, duration_seconds, loop, prompt_influence, model) and states the cost derived from this
 * sound's billed result. `measured` is the decoded length, used as the base when the sound had an
 * automatic length.
 */
export function nextActions(rec: SoundRecord, measured: null | number = null): NextAction[] {
  const prompt = (rec.prompt || '').replace(/\s+/g, ' ').trim().slice(0, 450)
  if (!prompt) return []
  const model = rec.model && (MODELS as readonly string[]).includes(rec.model) ? rec.model : MODELS[0]
  const base: ToolArgs = {
    prompt,
    loop: Boolean(rec.loop),
    model,
    ...(typeof rec.duration_seconds === 'number' ? { duration_seconds: rec.duration_seconds } : {}),
    ...(typeof rec.prompt_influence === 'number' ? { prompt_influence: rec.prompt_influence } : {})
  }
  const seconds = typeof rec.duration_seconds === 'number' ? rec.duration_seconds : null
  const length = seconds ?? (measured != null && measured > 0 ? measured : null)
  const ref = `(from sound ${rec.id})`
  const out: NextAction[] = []

  const same = costFor(rec, seconds)
  out.push({
    id: 'variations',
    label: '3 variations',
    cost: same.total != null ? `≈ ${credits(same.total * 3)} total (3 × ${credits(same.total)})` : same.line,
    message:
      `Use ${TOOL} to make 3 variations of this sound ${ref}, one call each with ${argsText(base)}. ` +
      `Cost: ${same.total != null ? `about ${credits(same.total)} each, ${credits(same.total * 3)} in total` : same.line}. ` +
      'Show each result with its sound card.'
  })

  const resize = (id: 'longer' | 'shorter', target: number) => {
    const args = { ...base, duration_seconds: target }
    const cost = costFor(rec, target)
    out.push({
      id,
      label: `${id === 'shorter' ? 'Shorter' : 'Longer'} (${secondsLabel(target)})`,
      cost: cost.line,
      message: `Use ${TOOL} to make a ${id} version of this sound ${ref} with ${argsText(args)}. Cost: ${cost.line}. Show it with its sound card.`
    })
  }
  if (length != null && length > 0.5) resize('shorter', clampDuration(length / 2))
  if (length != null && length < 30) resize('longer', clampDuration(length * 2))

  if (!rec.loop) {
    const args = { ...base, loop: true }
    out.push({
      id: 'loop',
      label: 'Make it loop',
      cost: same.line,
      message:
        `Use ${TOOL} to make a seamless loop of this sound ${ref} with ${argsText(args)}. Cost: ${same.line}. ` +
        'Show it with its sound card.'
    })
  }

  const other = model === MODELS[0] ? MODELS[1] : MODELS[0]
  out.push({
    id: 'model',
    label: `Try ${modelLabel(other)}`,
    cost: same.line,
    message:
      `Use ${TOOL} to make this sound ${ref} again with the ${modelLabel(other)} model: ${argsText({ ...base, model: other })}. ` +
      `Cost: ${same.line}. Show it with its sound card.`
  })
  return out
}

/** Does a tool.complete payload belong to this plugin's tool (direct, or wrapped by tool search's `tool_call`)? */
export function isSoundToolCall(name: unknown, args: unknown): boolean {
  if (name === TOOL) return true
  if (name !== 'tool_call' || !args || typeof args !== 'object') return false
  const a = args as { name?: unknown; calls?: unknown }
  if (a.name === TOOL) return true
  return Array.isArray(a.calls) && a.calls.some(c => !!c && typeof c === 'object' && (c as { name?: unknown }).name === TOOL)
}

export const SOUND_ID = /^[0-9a-f]{12}$/

/** Text filter for the library: every whitespace-separated term must appear in the prompt or model/format. */
export function matches(rec: SoundRecord, query: string): boolean {
  const hay = `${rec.prompt || ''} ${rec.model || ''} ${rec.output_format || ''}`.toLowerCase()
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every(term => hay.includes(term))
}
