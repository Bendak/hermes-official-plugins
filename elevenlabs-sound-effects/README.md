<p align="center"><img src="../assets/elevenlabs-sound-effects.jpg" alt="ElevenLabs Sound Effects: official Hermes Agent plugin" width="100%"></p>

# ElevenLabs Sound Effects for Hermes Agent

> Part of the [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent.

Official [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin for the
[ElevenLabs Sound Effects API](https://elevenlabs.io/docs/capabilities/sound-effects). It gives the
agent one tool:

| Tool | What it does |
|---|---|
| `elevenlabs_sound_effect` | Turns a text description into an audio file (0.5 to 30 seconds, optionally a seamless loop) |

Ask for "the sound of a heavy wooden door creaking open in a stone hall", "a short retro coin pickup
blip" or "90s hip-hop drum loop, 90 BPM, loopable" and Hermes generates it and saves it. The tool hands
the agent a `MEDIA:` line for the file; when the agent puts it in its reply, the file is sent as an
audio attachment on Telegram, Discord, Slack and the other messaging platforms. In Hermes Desktop the
sound appears as an inline card (below). In the terminal you get the file path.

The plugin also ships:

| Part | What it does |
|---|---|
| `elevenlabs-sound-design` skill | How to write ElevenLabs prompts (from ElevenLabs' own prompting guide), make loops, pick a format for where the sound goes (game engine, video, UI, chat), build a sound pack, and deliver per surface |
| `sfx_tool.py` (in the skill) | Local post-processing with ffmpeg: trim silence, loudness-normalise, fades, loop-seam click check, convert to OGG/WAV/MP3, pack a named set into a folder + `manifest.json` + zip. Costs no ElevenLabs credits |
| Desktop sound card | `::elevenlabs-sfx{id="…"}` in a reply renders a card: waveform, play/pause, gapless loop audition, download in each format, reveal, and **Next** actions |
| Desktop sound library | ⌘K → **ElevenLabs: open sound library**: every sound this profile made, with its prompt and settings |

## Install

```bash
hermes plugins install elevenlabs-sound-effects
```

Or straight from this repo: `hermes plugins install NousResearch/hermes-official-plugins#elevenlabs-sound-effects`.

The installer asks for your ElevenLabs API key if `ELEVENLABS_API_KEY` is not set yet (create one
under **Profile → API keys** at <https://elevenlabs.io/app/settings/api-keys>). It is the same key
Hermes's ElevenLabs text-to-speech voice uses, so if you already have that set up there is nothing
to enter. The tool only appears to the agent once the key is set. Plugins are installed per profile:
use `hermes -p <profile> plugins install elevenlabs-sound-effects` for another profile.

The only Python dependency is `httpx`, which Hermes already ships.

## Settings

In Hermes Desktop: **Settings → Plugins → ElevenLabs Sound Effects**. Everywhere else, with
`hermes config set`:

| Setting | Default | What it does |
|---|---|---|
| `api_key` | (unset) | Your ElevenLabs API key, stored in `.env` as `ELEVENLABS_API_KEY`: `hermes config set ELEVENLABS_API_KEY <key>` |
| `sound_model` | `eleven_text_to_sound_v2` | `eleven_text_to_sound_v2` or `eleven_text_to_sound_v3`: `hermes config set plugins.entries.elevenlabs-sound-effects.settings.sound_model eleven_text_to_sound_v3` |
| `output_format` | `mp3_44100_128` | `mp3_44100_128`, `mp3_44100_192`, `opus_48000_128`, `opus_48000_192`, `wav_44100`, `wav_48000`: `hermes config set plugins.entries.elevenlabs-sound-effects.settings.output_format wav_48000`. 192 kbps MP3 needs the ElevenLabs Creator plan or above; 44.1 kHz WAV needs Pro or above. |
| `api_base_url` | `https://api.elevenlabs.io` | Change only for an ElevenLabs data-residency endpoint, e.g. `https://api.eu.residency.elevenlabs.io` |

The agent chooses the per-sound parameters itself: the description, the length (`duration_seconds`,
0.5 to 30; omit it and ElevenLabs picks one from the prompt), `loop`, and `prompt_influence` (0 to
1; higher follows the prompt more literally, lower varies more).

The tool also takes an optional `model` per call (`eleven_text_to_sound_v2` or
`eleven_text_to_sound_v3`); without it the `sound_model` setting applies.

## Where files go

Each sound is saved to `<HERMES_HOME>/cache/generated/audio/sfx_<description>_<timestamp>_<id>.<ext>`
and never overwrites an earlier one. Ask the agent to copy it into your project if you want it
somewhere else.

Each generation also appends one line to `<HERMES_HOME>/plugin-data/<plugin namespace>/sounds.jsonl`
(the plugin's own data folder, per profile): the sound id, prompt, requested length, loop, prompt
influence, model, format, file name, size, the credits ElevenLabs billed, and the chat it came from.
The Desktop library reads it. Nothing in it leaves your machine.

## Hermes Desktop

Turn the plugin's Desktop half on in **Settings → Plugins → ElevenLabs Sound Effects** (Desktop halves
are opt-in).

**Sound card.** On Desktop the bundled skill ends each generation with `::elevenlabs-sfx{id="<sound_id>"}`.
The card shows the prompt, length, loop, model, format and credits billed, a waveform you can click
to seek, Play/Pause, a **Loop** toggle that repeats the sound gaplessly so you can hear the seam,
**Download** (the original, or MP3 / WAV / OGG converted by ffmpeg on the machine Hermes runs on;
saved through Desktop's Save dialog on your computer), and **Reveal** in your file manager. **Next**
offers 3 variations, shorter, longer, make it loop and try the other model. Each row shows its cost
worked out from what ElevenLabs billed for this sound (for an automatic-length sound the per-second
price isn't known, and the row says so), and sends a chat message to the agent naming the tool and its
arguments. Nothing is billed until the agent runs the tool. The message goes to the chat the card is in
(Desktop builds that tell plugins which chat shows a card), else to the chat that made the sound,
opening it if needed.

**Sound library.** ⌘K → **ElevenLabs: open sound library** opens a tab listing every sound the tool
made in the active profile, MP3, Ogg Opus and WAV alike, with per-format counts and sizes, a format
filter and a prompt search. Each row plays, downloads, reveals, **Reuse prompt** (into the composer)
and **Open chat** (the conversation that made it). It reloads when a sound is generated, when you
switch profile or connection, and when you save the plugin's settings; it never polls.

**Remote Hermes.** When Desktop is connected to Hermes on another machine, the files stay on that
machine. Playback and downloads stream through Hermes, so they work the same; Reveal is replaced by a
note saying which host holds the file. Format conversion needs ffmpeg on that host; without it only
the original format is offered and the menu says why. Sounds made before version 1.1.0 have no record
and don't appear in the library.

## Cost and privacy

Every call is a paid ElevenLabs generation billed to your account: ElevenLabs charges per second of
audio when a duration is set and by the prompt otherwise (see their
[pricing](https://elevenlabs.io/pricing/api)). The tool reports the `character-cost` header
ElevenLabs returns as `billed_characters`. What leaves your machine: the sound description and the
parameters above, sent to `api_base_url` with your API key. Nothing else from the conversation is
sent.

## Local processing script

The bundled skill runs `skills/elevenlabs-sound-design/scripts/sfx_tool.py` (Python standard library
plus `ffmpeg`/`ffprobe` on PATH) when a sound needs processing:

```bash
python3 sfx_tool.py process in.wav --out out.ogg --loudness -16 --fade-ms 10   # trim + normalise + fade + convert
python3 sfx_tool.py loopcheck loop.wav                                         # seamless | click
python3 sfx_tool.py pack a.mp3 b.mp3 --name "UI pack" --out-dir . --format ogg # folder + manifest.json + zip
```

Every run prints one JSON line (`{"ok": true, ...}` or `{"ok": false, "code": ..., "error": ...}`),
including when ffmpeg is missing or an input is not audio.

## Development

```bash
cd elevenlabs-sound-effects
HERMES_AGENT_REPO=../../hermes-agent PYTHONPATH=../../hermes-agent python -m pytest -q
hermes plugins validate .
npm ci && npm test && npm run typecheck && npm run build   # Desktop half: src/*.tsx → desktop/plugin.js
```

The tests load the plugin through Hermes's real plugin discovery and dispatch the tool through the
real registry against a local stand-in for `POST /v1/sound-generation` that checks requests against
ElevenLabs' published OpenAPI schema; they never call ElevenLabs. `tests/test_library.py` reads the
provenance records back through `dashboard/plugin_api.py`; `tests/test_sfx_tool_script.py` runs the
script (its ffmpeg cases skip where ffmpeg is absent); `tests/sound.test.mjs` covers the card's Next
actions.

The Desktop half is TypeScript in `src/`, bundled with esbuild into `desktop/plugin.js`, which is
committed (Hermes Desktop loads it as is). It imports only `@hermes/plugin-sdk` and `react`;
`sdk.d.ts` declares the SDK names it uses, and newer SDK calls are feature-detected. Rebuild and
commit `desktop/plugin.js` after any change under `src/`.

## License

MIT, see [LICENSE](LICENSE).
