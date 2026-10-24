/**
 * Which Meshy tasks the status-bar progress chip watches, and when it asks the backend about them. Pure (no SDK
 * import) so node tests can drive it with fake events, clocks and a counting `rest`.
 *
 * Two sources, merged:
 *  1. Gateway progress events: a host that forwards MCP `notifications/progress` sends a `tool.progress`-style
 *     event ({name | tool_id, progress, total?, message?}). Used when present.
 *  2. The plugin backend's GET /active?tasks=kind:id,… for the tasks this window saw a Meshy tool start: the id a
 *     create call returned (tool.complete), or the id a status call is waiting on (tool.start). One Meshy read per
 *     task per poll, nothing while no task is known to run. Listing every Meshy route instead cost 17 calls per
 *     poll, which crowds Meshy's 20 requests/s per key.
 *
 * A task leaves the watch list when Meshy reports any status other than PENDING / IN_PROGRESS, when its route
 * still does not know it after EARLY_404_MS (a just-created id can lag), or after an hour. Polling backs off on
 * transient failures (3 s → 60 s) and stops on a permanent one (a refused key) or after MAX_FAILURES in a row;
 * the next Meshy tool call re-arms it. Tasks tagged with another profile or connection than the one in front
 * are not polled (ctx.rest reaches the front one, whose key does not own them).
 *
 * Meshy tools sit behind tool search, so a call usually arrives as `tool_call` with `args.calls[].name =
 * mcp_meshy_*`, not as a top-level `mcp_meshy_*` name; both are recognised.
 */

import { backoffMs, classifyFailure, EARLY_404_MS, httpStatus, type PollClock } from './task-poll.ts'

export interface ActiveTask {
  task_id: string
  task_type: string
  stage: string
  status: string
  progress: number
  prompt: string
}

export interface ProgressState {
  /** A Meshy tool call is running right now. */
  running: boolean
  /** Watched tasks Meshy still reports as PENDING / IN_PROGRESS (newest first). */
  tasks: ActiveTask[]
  /** Latest pushed progress from the gateway, if the host forwards MCP progress. */
  live: { progress: number; message: string } | null
}

/** Where Desktop says an event came from (renderer tags on GatewayEvent; absent on the primary path). */
export interface EventOrigin {
  profile?: string
  connectionId?: string
}

interface EventLike {
  payload?: unknown
  profile?: string
  connectionId?: string
}

export interface ActiveCtx {
  onEvent: (type: string, fn: (e: EventLike) => void) => () => void
  rest: <T>(path: string, init?: { timeoutMs?: number }) => Promise<T>
}

export const ACTIVE_POLL_MS = 4000
export const MAX_FAILURES = 5
export const MAX_WATCH_MS = 60 * 60 * 1000
/** The backend reads at most this many tasks per poll; the newest ones win. */
export const MAX_WATCHED = 8
const PROGRESS_EVENTS = ['tool.progress', 'mcp.progress', 'tool.progress.update']

/** The Meshy route (backend `_TASK_ROUTES` name) each creating tool's task lives on. */
export const TOOL_KIND: Record<string, string> = {
  meshy_text_to_3d: 'text-to-3d',
  meshy_text_to_3d_refine: 'text-to-3d',
  meshy_image_to_3d: 'image-to-3d',
  meshy_multi_image_to_3d: 'multi-image-to-3d',
  meshy_remesh: 'remesh',
  meshy_retexture: 'retexture',
  meshy_rig: 'rigging',
  meshy_animate: 'animation',
  meshy_text_to_motion: 'text-to-motion',
  meshy_convert: 'convert',
  meshy_resize: 'resize',
  meshy_uv_unwrap: 'uv-unwrap',
  meshy_text_to_image: 'text-to-image',
  meshy_image_to_image: 'image-to-image',
  meshy_repair_printability: 'print-repair',
  meshy_auto_split: 'print-split',
  meshy_process_multicolor: 'print-multi-color'
}
/** A status call without `task_type`: the backend finds the task's route itself (cached after the first read). */
export const ANY_KIND = 'any'
const KIND = /^[a-z0-9-]{1,48}$/
const TASK_ID = /^[0-9a-f][0-9a-f-]{15,63}$/

const bare = (name: unknown) => String(name ?? '').replace(/^(mcp__meshy__|mcp_meshy_)/, '')

const isMeshyName = (name: unknown) => {
  const n = String(name ?? '')
  return n.startsWith('mcp__meshy__') || n.startsWith('mcp_meshy_') || n.startsWith('meshy_')
}

/** True for a direct Meshy tool name, or a `tool_call` bridge whose calls include one. */
export function isMeshyCall(name: unknown, args: unknown): boolean {
  if (isMeshyName(name)) {
    return true
  }
  if (String(name ?? '') !== 'tool_call' || !args || typeof args !== 'object') {
    return false
  }
  const a = args as { calls?: unknown; name?: unknown }
  const calls = Array.isArray(a.calls) ? a.calls : [a]
  return calls.some(c => !!c && typeof c === 'object' && isMeshyName((c as { name?: unknown }).name))
}

/** The task id a create call returned: structuredContent.task_id, else the "**Task ID**: …" line. */
export function taskIdFrom(result: unknown): null | string {
  let value: unknown = result
  for (let i = 0; i < 3 && typeof value === 'string'; i++) {
    try {
      value = JSON.parse(value)
    } catch {
      break
    }
  }
  if (value && typeof value === 'object') {
    const v = value as { structuredContent?: { task_id?: unknown }; result?: unknown }
    if (typeof v.structuredContent?.task_id === 'string') {
      return v.structuredContent.task_id
    }
    value = v.result ?? JSON.stringify(value)
  }
  const m = /\*\*Task ID\*\*:\s*([0-9a-f][0-9a-f-]{15,63})/.exec(String(value ?? ''))
  return m ? m[1] : null
}

export interface MeshyCall {
  tool: string
  args: Record<string, unknown>
  /** This call's own result (a `tool_call` bridge carries one entry per call, in order). */
  result: unknown
}

/** Each Meshy call in an event: a direct call, or each call a `tool_call` bridge made. */
export function meshyCalls(name: unknown, args: unknown, result?: unknown): MeshyCall[] {
  if (isMeshyName(name)) {
    return [{ tool: bare(name), args: (args ?? {}) as Record<string, unknown>, result }]
  }
  if (String(name ?? '') !== 'tool_call' || !args || typeof args !== 'object') {
    return []
  }
  const calls = (Array.isArray((args as { calls?: unknown }).calls) ? (args as { calls: unknown[] }).calls : [args]) as Array<{
    name?: unknown
    arguments?: unknown
  }>
  let results: unknown[] = []
  try {
    const parsed = typeof result === 'string' ? JSON.parse(result) : result
    const r = (parsed as { results?: unknown })?.results
    results = Array.isArray(r) ? r : [parsed]
  } catch {
    results = [result]
  }
  return calls.flatMap((call, i) => {
    if (!call || typeof call !== 'object' || !isMeshyName(call.name)) {
      return []
    }
    const one = results[i] as { result?: unknown; content?: unknown } | undefined
    return [{ tool: bare(call.name), args: (call.arguments ?? {}) as Record<string, unknown>, result: one?.result ?? one?.content ?? one }]
  })
}

/** Tasks a finished call started (tool.complete): `[kind, id]` per creating call that returned a task id. */
export function startedTasks(name: unknown, args: unknown, result: unknown): Array<[string, string]> {
  return meshyCalls(name, args, result).flatMap(c => {
    const kind = TOOL_KIND[c.tool]
    const id = kind ? taskIdFrom(c.result) : null
    return kind && id ? [[kind, id] as [string, string]] : []
  })
}

/** Tasks a starting status call waits on (tool.start): `[kind, id]`, kind = its `task_type` or ANY_KIND. */
export function awaitedTasks(name: unknown, args: unknown): Array<[string, string]> {
  return meshyCalls(name, args).flatMap(c => {
    const id = c.args.task_id
    if (c.tool !== 'meshy_get_task_status' || typeof id !== 'string' || !TASK_ID.test(id)) {
      return []
    }
    const kind = typeof c.args.task_type === 'string' && KIND.test(c.args.task_type) ? c.args.task_type : ANY_KIND
    return [[kind, id] as [string, string]]
  })
}

interface Watched {
  kind: string
  since: number
  origin?: EventOrigin
}

interface ActiveReply {
  tasks?: ActiveTask[]
  done?: string[]
  missing?: string[]
}

const realClock: PollClock = {
  now: () => Date.now(),
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: handle => clearTimeout(handle as ReturnType<typeof setTimeout>)
}

export interface TrackerOptions {
  clock?: PollClock
  /** True when an event's origin is another profile/connection than the one ctx.rest reaches now. */
  foreign?: (origin?: EventOrigin) => boolean
}

export function createProgressTracker(
  ctx: ActiveCtx,
  onChange: (s: ProgressState) => void,
  onIdle: () => void,
  { clock = realClock, foreign = () => false }: TrackerOptions = {}
) {
  let state: ProgressState = { running: false, tasks: [], live: null }
  const runningIds = new Set<string>()
  const watched = new Map<string, Watched>()
  let timer: unknown
  let inFlight = false
  let failures = 0
  let stopped = false
  let disposed = false

  const set = (patch: Partial<ProgressState>) => {
    const wasBusy = state.running || state.tasks.length > 0
    state = { ...state, ...patch }
    onChange(state)
    if (wasBusy && !state.running && state.tasks.length === 0) {
      onIdle()
    }
  }

  const pollable = () => {
    const now = clock.now()
    for (const [id, w] of watched) {
      if (now - w.since > MAX_WATCH_MS) {
        watched.delete(id)
      }
    }
    return [...watched].filter(([, w]) => !foreign(w.origin)).slice(-MAX_WATCHED)
  }

  const schedule = (delay: number) => {
    if (timer !== undefined || inFlight || disposed || stopped || pollable().length === 0) {
      return
    }
    timer = clock.setTimeout(poll, delay)
  }

  const rearm = (delay: number) => {
    stopped = false
    failures = 0
    if (timer !== undefined) {
      clock.clearTimeout(timer)
      timer = undefined
    }
    schedule(delay)
  }

  function poll() {
    timer = undefined
    const list = pollable()
    if (!list.length) {
      if (state.tasks.length) {
        set({ tasks: [] })
      }
      return
    }
    inFlight = true
    let next = ACTIVE_POLL_MS
    const query = list.map(([id, w]) => `${w.kind}:${id}`).join(',')
    ctx
      .rest<ActiveReply>(`active?tasks=${encodeURIComponent(query)}`, { timeoutMs: 15_000 })
      .then(
        res => {
          if (disposed) return
          failures = 0
          const now = clock.now()
          for (const id of Array.isArray(res.done) ? res.done : []) watched.delete(id)
          for (const id of Array.isArray(res.missing) ? res.missing : []) {
            const w = watched.get(id)
            if (w && now - w.since >= EARLY_404_MS) watched.delete(id)
          }
          const rows = (Array.isArray(res.tasks) ? res.tasks : []).filter(t => watched.has(t.task_id))
          set({ tasks: rows })
        },
        (err: unknown) => {
          if (disposed) return
          failures += 1
          // A whole-request 404 is an old or missing backend route: permanent like any other 4xx.
          if (classifyFailure(httpStatus(err), Infinity) === 'permanent' || failures >= MAX_FAILURES) {
            stopped = true
            set({ tasks: [] })
          }
          next = backoffMs(failures)
        }
      )
      .finally(() => {
        inFlight = false
        schedule(next)
      })
  }

  const payloadOf = (e: EventLike) => (e.payload ?? {}) as Record<string, unknown>
  const originOf = (e: EventLike): EventOrigin => ({ profile: e.profile, connectionId: e.connectionId })
  const watch = (pairs: Array<[string, string]>, origin: EventOrigin) => {
    for (const [kind, id] of pairs) {
      const known = watched.get(id)
      // A status call's real task_type (or a create's route) beats an earlier "any".
      watched.set(id, { kind: known && kind === ANY_KIND ? known.kind : kind, since: known?.since ?? clock.now(), origin })
    }
  }

  const offs = [
    ctx.onEvent('tool.start', e => {
      const p = payloadOf(e)
      if (!isMeshyCall(p.name, p.args)) {
        return
      }
      runningIds.add(String(p.tool_id ?? p.name))
      watch(awaitedTasks(p.name, p.args), originOf(e))
      set({ running: true })
      rearm(1500)
    }),
    ctx.onEvent('tool.complete', e => {
      const p = payloadOf(e)
      const id = String(p.tool_id ?? p.name)
      if (!runningIds.delete(id) && !isMeshyCall(p.name, p.args)) {
        return
      }
      watch(startedTasks(p.name, p.args, p.result), originOf(e))
      set({ running: runningIds.size > 0, live: runningIds.size > 0 ? state.live : null })
      rearm(1000)
    }),
    ...PROGRESS_EVENTS.map(type =>
      ctx.onEvent(type, e => {
        const p = payloadOf(e)
        const ours = runningIds.has(String(p.tool_id ?? '')) || isMeshyName(p.name) || isMeshyCall(p.name, p.args)
        const raw = Number(p.progress)
        if (!ours || !Number.isFinite(raw)) {
          return
        }
        const total = Number(p.total)
        const pct = Number.isFinite(total) && total > 0 ? (raw / total) * 100 : raw
        set({ live: { progress: Math.max(0, Math.min(100, Math.round(pct))), message: String(p.message ?? '') } })
      })
    )
  ]

  return {
    get: () => state,
    /** Watched task ids (tests, debugging). */
    watching: () => [...watched.keys()],
    /** The profile/connection in front changed: drop rows read through the old one and poll what the new one owns. */
    rescope() {
      if (state.tasks.length) {
        set({ tasks: [] })
      }
      rearm(0)
    },
    dispose() {
      disposed = true
      if (timer !== undefined) {
        clock.clearTimeout(timer)
      }
      offs.forEach(off => off())
    }
  }
}
