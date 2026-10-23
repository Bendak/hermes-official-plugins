/**
 * ElevenLabs Sound Effects for Hermes Desktop.
 *
 * - `::elevenlabs-sfx{id="<sound_id>"}` renders an inline sound card in the transcript: waveform,
 *   play/pause, gapless loop audition, download per format, reveal, and Next actions. The bundled
 *   elevenlabs-sound-design skill tells the agent to emit it after each generation on Desktop.
 * - "ElevenLabs: open sound library" (⌘K) opens a workspace tab listing every sound this profile made,
 *   from the provenance record the tool writes per generation.
 *
 * Bytes come from this plugin's backend (`dashboard/plugin_api.py`) through `ctx.rest`, so the card and
 * the library work the same when Desktop is connected to a remote Hermes. No account-wide UI (balance,
 * usage) by design: other ElevenLabs integrations share the key.
 */

import type { HermesPlugin, PaletteContribution, TranscriptDirectiveProps } from '@hermes/plugin-sdk'
import { host, PALETTE_AREA, TRANSCRIPT_DIRECTIVE_AREA } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'

import { player } from './audio.ts'
import { SoundCard } from './card.tsx'
import { SoundLibrary } from './library.tsx'
import { isSoundToolCall } from './sound.ts'

const PLUGIN_ID = 'elevenlabs-sound-effects'
const WORKSPACE_ID = 'elevenlabs-sound-library'

const plugin: HermesPlugin = {
  id: PLUGIN_ID,
  name: 'ElevenLabs Sound Effects',
  description: 'Play, loop and download ElevenLabs sound effects inline in chat, and browse every sound in a library tab.',
  register(ctx) {
    // Bumped when a sound tool call completes or the plugin's settings change: the open library reloads
    // once per bump. Event-driven only; nothing polls.
    let refresh = 0
    const refreshListeners = new Set<(n: number) => void>()
    const bump = () => {
      refresh += 1
      refreshListeners.forEach(fn => fn(refresh))
    }
    let closeWorkspace: (() => void) | null = null

    function LibraryHost() {
      const [signal, setSignal] = useState(refresh)
      useEffect(() => {
        refreshListeners.add(setSignal)
        return () => {
          refreshListeners.delete(setSignal)
        }
      }, [])
      return <SoundLibrary ctx={ctx} refreshSignal={signal} />
    }

    const openLibrary = () => {
      if (typeof host.openWorkspace !== 'function') {
        host.notify({ kind: 'warning', message: 'Update Hermes Desktop to open the sound library.' })
        return
      }
      closeWorkspace = host.openWorkspace(WORKSPACE_ID, {
        title: 'Sound library',
        minWidth: '26rem',
        onClose: () => {
          closeWorkspace = null
        },
        render: () => <LibraryHost />
      })
    }

    ctx.onEvent('tool.complete', event => {
      const payload = (event.payload ?? {}) as { name?: unknown; args?: unknown }
      if (isSoundToolCall(payload.name, payload.args)) bump()
    })
    ctx.onEvent(`plugin.${PLUGIN_ID}.settings.changed`, bump)
    ctx.onDispose(() => {
      closeWorkspace?.()
      refreshListeners.clear()
      player.dispose()
    })

    ctx.registerMany([
      {
        id: 'sound-directive',
        area: TRANSCRIPT_DIRECTIVE_AREA,
        data: {
          name: 'elevenlabs-sfx',
          render: (props: TranscriptDirectiveProps) => (
            <SoundCard
              ctx={ctx}
              id={String(props.attrs.id ?? '').trim()}
              onOpenLibrary={openLibrary}
              // Hosts with the directive-owner seam name the chat that shows this card; undefined on older ones.
              sessionId={props.sessionId}
              source={props.source}
              streaming={props.streaming}
            />
          )
        }
      },
      {
        id: 'open-library',
        area: PALETTE_AREA,
        data: {
          id: 'elevenlabs-sfx.library',
          label: 'ElevenLabs: open sound library',
          keywords: ['elevenlabs', 'sound', 'sfx', 'audio', 'library', 'gallery', 'effects'],
          run: openLibrary
        } satisfies PaletteContribution
      },
      {
        id: 'open-settings',
        area: PALETTE_AREA,
        data: {
          id: 'elevenlabs-sfx.settings',
          label: 'ElevenLabs Sound Effects: settings',
          keywords: ['elevenlabs', 'sound', 'settings', 'api key', 'format', 'model'],
          run: () => host.navigate(`/settings?tab=plugins&agent=${PLUGIN_ID}`)
        } satisfies PaletteContribution
      }
    ])
  }
}

export default plugin
