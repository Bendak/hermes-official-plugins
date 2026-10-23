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
audio attachment on Telegram, Discord, Slack and the other messaging platforms and shows up in the chat
in Hermes Desktop. In the terminal you get the file path.

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

## Where files go

Each sound is saved to `<HERMES_HOME>/cache/generated/audio/sfx_<description>_<timestamp>_<id>.<ext>`
and never overwrites an earlier one. Ask the agent to copy it into your project if you want it
somewhere else.

## Cost and privacy

Every call is a paid ElevenLabs generation billed to your account: ElevenLabs charges per second of
audio when a duration is set and by the prompt otherwise (see their
[pricing](https://elevenlabs.io/pricing/api)). The tool reports the `character-cost` header
ElevenLabs returns as `billed_characters`. What leaves your machine: the sound description and the
parameters above, sent to `api_base_url` with your API key. Nothing else from the conversation is
sent.

## Development

```bash
cd elevenlabs-sound-effects
HERMES_AGENT_REPO=../../hermes-agent PYTHONPATH=../../hermes-agent python -m pytest -q
hermes plugins validate .
```

The tests load the plugin through Hermes's real plugin discovery and dispatch the tool through the
real registry against a local stand-in for `POST /v1/sound-generation` that checks requests against
ElevenLabs' published OpenAPI schema; they never call ElevenLabs.

## License

MIT, see [LICENSE](LICENSE).
