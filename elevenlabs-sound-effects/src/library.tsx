// The sound library workspace: every sound this profile's tool made, from the provenance records.

import type { PluginContext } from '@hermes/plugin-sdk'
import { Button, Codicon, host } from '@hermes/plugin-sdk'
import { useEffect, useMemo, useState } from 'react'

import { backendIsRemote, currentScope, errorText, onScopeChange } from './audio.ts'
import {
  billed,
  bytesLabel,
  formatLabel,
  type HostInfo,
  KINDS,
  kindLabel,
  matches,
  modelLabel,
  secondsLabel,
  type SoundRecord
} from './sound.ts'
import { Chip, DownloadMenu, S, Transport, useSound, Waveform, Where } from './ui.tsx'

interface Library extends HostInfo {
  sounds: SoundRecord[]
  total: number
  totals: Record<string, { count: number; bytes: number; missing: number }>
}

const L = {
  pane: { display: 'flex', flexDirection: 'column', gap: 10, padding: 14, height: '100%', overflow: 'auto', color: 'var(--color-foreground)' },
  input: {
    flex: 1,
    minWidth: 160,
    background: 'var(--color-background)',
    color: 'var(--color-foreground)',
    border: '1px solid var(--color-border)',
    borderRadius: 6,
    padding: '4px 8px',
    fontSize: 13
  },
  row: { border: '1px solid var(--color-border)', borderRadius: 8, padding: '8px 10px', display: 'flex', flexDirection: 'column', gap: 6 }
} as const

/**
 * `refreshSignal` changes when a sound tool call completes (or settings change) so the list reloads once;
 * a profile or connection switch reloads it too. No timers: each trigger is one request, and an error
 * stays on screen with a Retry button instead of retrying on its own.
 */
export function SoundLibrary({ ctx, refreshSignal }: { ctx: PluginContext; refreshSignal: number }) {
  const [data, setData] = useState<Library | null>(null)
  const [error, setError] = useState<null | string>(null)
  const [remote, setRemote] = useState<boolean | null>(null)
  const [scope, setScope] = useState(currentScope())
  const [reload, setReload] = useState(0)
  const [kind, setKind] = useState<string>('all')
  const [query, setQuery] = useState('')

  useEffect(
    () =>
      onScopeChange(() => {
        setData(null)
        setScope(currentScope())
      }),
    []
  )

  useEffect(() => {
    let live = true
    setError(null)
    void backendIsRemote().then(r => live && setRemote(r))
    ctx
      .rest<Library>('sounds?limit=5000', { timeoutMs: 20_000 })
      .then(d => live && setData(d))
      .catch(err => live && setError(errorText(err)))
    return () => {
      live = false
    }
  }, [ctx, scope.profile, scope.connection, reload, refreshSignal])

  const shown = useMemo(
    () => (data?.sounds ?? []).filter(s => (kind === 'all' || s.kind === kind) && matches(s, query)),
    [data, kind, query]
  )

  const where =
    remote === true
      ? `on ${data?.host ?? 'the remote Hermes host'} (remote). Playback and downloads stream through Hermes.`
      : remote === false
        ? 'on this computer.'
        : `on ${data?.host ?? 'the Hermes host'}, the machine running this profile’s Hermes.`

  return (
    <div data-sfx-library="" style={L.pane as React.CSSProperties}>
      <div style={{ ...S.row, justifyContent: 'space-between' }}>
        <strong style={{ fontSize: 15 }}>
          <Codicon name="library" /> Sound library
        </strong>
        <Button aria-label="Reload" onClick={() => setReload(r => r + 1)} size="sm" variant="ghost">
          <Codicon name="refresh" />
        </Button>
      </div>
      <span data-sfx-scope="" style={S.muted}>
        Profile <b>{scope.profile || 'default'}</b>: sounds generated with ElevenLabs, stored {where}
      </span>
      {error && (
        <div style={S.row}>
          <span style={S.error}>{error}</span>
          <Button onClick={() => setReload(r => r + 1)} size="sm" variant="outline">
            Retry
          </Button>
        </div>
      )}
      {data && (
        <>
          <div data-sfx-totals="" style={S.row}>
            <Chip>
              {data.total} sound{data.total === 1 ? '' : 's'}
              {data.sounds.length < data.total ? ` (newest ${data.sounds.length} listed)` : ''}
            </Chip>
            {KINDS.map(k => {
              const t = data.totals[k]
              return (
                <Chip key={k} title={t?.missing ? `${t.missing} file(s) no longer on the host` : undefined}>
                  {kindLabel(k)} {t?.count ?? 0} · {bytesLabel(t?.bytes ?? 0)}
                </Chip>
              )
            })}
            {!data.ffmpeg && <span style={S.muted}>Conversions need ffmpeg on {data.host}.</span>}
          </div>
          <div style={S.row}>
            {['all', ...KINDS].map(k => (
              <Button
                aria-pressed={kind === k}
                data-sfx-filter={k}
                key={k}
                onClick={() => setKind(k)}
                size="sm"
                variant={kind === k ? 'default' : 'outline'}
              >
                {k === 'all' ? 'All' : kindLabel(k)}
              </Button>
            ))}
            <input
              aria-label="Search prompts"
              onChange={e => setQuery(e.currentTarget.value)}
              placeholder="Search prompts…"
              style={L.input}
              value={query}
            />
          </div>
          {data.total === 0 ? (
            <span style={S.muted}>
              No sounds yet in this profile. Ask Hermes for one (“a short retro coin pickup sound”); every sound the
              ElevenLabs tool makes from now on is listed here with the prompt and settings that made it.
            </span>
          ) : shown.length === 0 ? (
            <span style={S.muted}>No sounds match.</span>
          ) : (
            shown.map(rec => <LibraryRow ctx={ctx} info={data} key={rec.id} rec={rec} remote={remote} />)
          )}
        </>
      )}
      {!data && !error && <span style={S.muted}>Loading…</span>}
    </div>
  )
}

function LibraryRow({ ctx, rec, info, remote }: { ctx: PluginContext; rec: SoundRecord; info: HostInfo; remote: boolean | null }) {
  const sound = useSound(ctx, rec, `lib:${rec.id}`)
  const cost = billed(rec)
  const date = rec.created_at ? new Date(rec.created_at) : null
  const reuse = async () => {
    const text = rec.prompt || ''
    const inserted = typeof host.composer?.insertText === 'function' ? await host.composer.insertText(null, text) : false
    if (inserted) {
      host.notify({ kind: 'success', message: 'Prompt added to the composer.' })
    } else if (ctx.os.writeClipboard && (await ctx.os.writeClipboard(text))) {
      host.notify({ kind: 'info', message: 'No composer open: prompt copied to the clipboard.' })
    } else {
      host.notify({ kind: 'warning', message: 'Open a chat to reuse the prompt.' })
    }
  }
  const openChat = () => {
    if (rec.session_id && typeof host.openSession === 'function') {
      host.openSession(rec.session_id).catch(err => host.notify({ kind: 'error', message: `Could not open the chat: ${errorText(err)}` }))
    }
  }
  return (
    <div data-sfx-row={rec.kind} style={L.row as React.CSSProperties}>
      <div style={{ ...S.row, alignItems: 'flex-start' }}>
        <Transport compact sound={sound} />
        <span style={{ ...S.prompt, flex: 1 }}>{rec.prompt}</span>
      </div>
      {sound.loaded.state === 'ready' && <Waveform data={sound.loaded.wave} height={30} onSeek={f => void sound.seek(f)} progress={sound.progress} />}
      {sound.loaded.state === 'error' && <span style={S.error}>{sound.loaded.message}</span>}
      <div style={S.row}>
        <Chip>{sound.duration != null ? secondsLabel(sound.duration) : secondsLabel(rec.duration_seconds)}</Chip>
        {rec.loop && <Chip>loopable</Chip>}
        <Chip title={rec.model ?? ''}>{modelLabel(rec.model)}</Chip>
        <Chip>{formatLabel(rec.output_format)}</Chip>
        {typeof rec.prompt_influence === 'number' && <Chip title="prompt_influence">influence {rec.prompt_influence}</Chip>}
        {cost != null && <Chip>{cost} credits</Chip>}
        {rec.size != null && <Chip>{bytesLabel(rec.size)}</Chip>}
        {date && !Number.isNaN(date.getTime()) && <span style={S.muted}>{date.toLocaleString()}</span>}
      </div>
      <div style={S.row}>
        <Button data-sfx-reuse="" onClick={() => void reuse()} size="sm" variant="outline">
          <Codicon name="edit" /> Reuse prompt
        </Button>
        <Button
          data-sfx-open-chat=""
          disabled={!rec.session_id || typeof host.openSession !== 'function'}
          onClick={openChat}
          size="sm"
          title={rec.session_id ? 'Open the chat that made this sound' : 'No chat recorded for this sound'}
          variant="outline"
        >
          <Codicon name="comment-discussion" /> Open chat
        </Button>
        <DownloadMenu ctx={ctx} info={info} rec={rec} />
        <Where ctx={ctx} info={info} rec={rec} remote={remote} />
      </div>
    </div>
  )
}
