---
name: elevenlabs-sound-design
description: Write ElevenLabs sound effect prompts, loops and packs.
version: 1.1.0
author: Teknium (teknium1), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [audio, sound-effects, elevenlabs, game-audio, foley]
    related_skills: []
---

# ElevenLabs Sound Design

Turns a request for a sound ("a coin pickup for my platformer", "rain ambience I can loop", "a UI
sound pack") into good `elevenlabs_sound_effect` calls, picks the file format for where the sound is
going, post-processes it locally when the destination needs it, and delivers it on the user's
surface. Every generation is billed to the user's ElevenLabs account; local processing is free.

## When to Use

- The user asks for a sound effect, foley, ambience, UI sound, stinger, whoosh, drum loop or a set of them.
- The user wants a generated sound trimmed, normalised, looped cleanly, converted (ogg/wav/mp3) or packed.
- Don't use for: speech or voices (Hermes's text-to-speech), full songs (ElevenLabs' Music API, not this plugin).

## Prerequisites

- The `elevenlabs_sound_effect` tool (this plugin, with `ELEVENLABS_API_KEY` set).
- Optional, for post-processing: `ffmpeg` and `ffprobe` on PATH of the machine Hermes runs on. Check
  with `terminal(command="ffmpeg -version")`; without them skip processing and deliver the original.

## Tool arguments (exact names from the tool schema)

| Argument | Values | Notes |
|---|---|---|
| `prompt` | text | Required. What the sound is. |
| `duration_seconds` | 0.5-30 | Omit to let ElevenLabs choose. Set it for anything that must fit a slot (UI blip, cut). |
| `loop` | true/false | Seamless loop (ambience, beds, engine hums). |
| `prompt_influence` | 0-1 (default 0.3) | Higher = more literal, lower = more varied. |
| `model` | `eleven_text_to_sound_v2`, `eleven_text_to_sound_v3` | Omit to use the user's setting. |

The format is a user setting (`output_format`: mp3, Ogg Opus or 48/44.1 kHz WAV), not an argument;
convert locally (below) when one sound needs another format. Never invent other arguments.

## Writing the prompt (ElevenLabs' prompting guide)

Source: ElevenLabs, "Sound effects → Prompting guide",
https://elevenlabs.io/docs/capabilities/sound-effects

1. Simple effects: a clear, concise description of source + action + surface/space:
   "Glass shattering on concrete", "Heavy wooden door creaking open", "Thunder rumbling in the distance".
2. Sequences: describe the order of events with "then"/"followed by":
   "Footsteps on gravel, then a metallic door opens".
3. Musical elements: genre, tempo, key: "90s hip-hop drum loop, 90 BPM", "Vintage brass stabs in F minor".
4. Audio terms the model understands: impact, whoosh, ambience, one-shot, loop, stem, braam, glitch, drone.
5. Add what you know about the destination: "short, dry, no reverb" for UI; "distant, wide stereo"
   for ambience; "single hit, no tail" for one-shots that a game engine will layer.

Keep one sound per call. For a set, make one call per sound with a consistent style phrase
("soft, rounded, wooden, short decay") repeated in every prompt so the pack sounds like one family.

## Loops

- Set `loop: true` and a `duration_seconds` long enough to avoid obvious repetition (6-20 s for
  ambience, a whole number of bars for music: at 90 BPM one 4/4 bar is 2.667 s).
- Verify the seam before calling it seamless: run the `loopcheck` step below and report its verdict.
  A loop flagged `click` gets a 5-10 ms crossfade via `process --fade-ms 8 --no-trim`, or a regeneration.
- Never trim silence or add fades to a loop unless the check fails: both break the seam.

## Format by destination

| Destination | Deliver | Why |
|---|---|---|
| Godot, Unity, Unreal, web games | OGG (Vorbis) for music/ambience loops, WAV for short one-shots | engines import both; OGG keeps loops small, WAV avoids decode cost and MP3 padding on one-shots |
| Video editing (Premiere, Resolve, Final Cut) | WAV 48 kHz | the video standard; no generation loss |
| App/UI sounds | WAV (or OGG on Android/web) | short, sample-accurate start |
| Chat (Telegram, Discord, Slack, WhatsApp) | MP3 or the original | small, plays inline |

MP3 adds a short silence at the start (encoder padding), so never use MP3 for gapless loops in an
engine; convert loops to OGG or WAV.

## Local post-processing script

`scripts/sfx_tool.py` (stdlib Python + ffmpeg/ffprobe). Every run prints exactly one JSON line,
`{"ok": true, ...}` or `{"ok": false, "error": "...", "code": "..."}`, and exits 0 or 1; read that
line, never guess. Codes: `ffmpeg_missing`, `bad_input`, `bad_args`, `ffmpeg_failed`.
Run it with `terminal`, using the script's path inside this skill's directory:

```
python3 <skill_dir>/scripts/sfx_tool.py probe IN
python3 <skill_dir>/scripts/sfx_tool.py process IN --out OUT.ogg [--trim] [--loudness -16] [--fade-ms 10] [--no-trim]
python3 <skill_dir>/scripts/sfx_tool.py loopcheck IN
python3 <skill_dir>/scripts/sfx_tool.py pack --name NAME --out-dir DIR FILE [FILE ...] [--format ogg|wav|mp3] [--prompts prompts.json]
```

- `process`: trims leading/trailing silence (`--trim`, default on for one-shots; `--no-trim` for
  loops), loudness-normalises to `--loudness` LUFS (EBU R128; -16 for chat/web, -23 for broadcast
  video, -14 to -18 for games), applies `--fade-ms` in/out fades, and writes the format implied by
  `OUT`'s extension (`.ogg` Vorbis, `.wav` 16-bit PCM, `.mp3`). It never overwrites its input.
- `loopcheck`: compares the last and first samples of the file; verdict `seamless` or `click`
  with the jump size. Report the verdict as measured.
- `pack`: copies (and converts with `--format`) the files into `DIR/NAME/` with tidy names, writes
  `manifest.json` (file, duration, sample rate, channels, format, and the prompt for each file when
  `--prompts` maps file names to prompts) and `DIR/NAME.zip`.

## Procedure

1. Settle the brief before spending: what the sound is, where it goes (engine, video, UI, chat), and
   the length. Ask only for what you can't infer. Completion: you can fill `prompt`, `duration_seconds`
   and `loop`.
2. State the cost before a set or a long sound: ElevenLabs bills per generated second when
   `duration_seconds` is set (the tool's `billed_characters` reports the real charge; read it from the
   first result and use it for the rest of a set). Completion: the user knows the size of the spend.
3. Call `elevenlabs_sound_effect`. For variations, call it again with the same prompt (and a lower
   `prompt_influence` for more variety). Completion: each call returned `success` and a `sound_id`.
4. Post-process only when the destination needs it (format, loudness, trimming, a loop check).
   Completion: the script printed `"ok": true` for every file, or you report its error line.
5. Deliver by surface:
   - Hermes Desktop: one line of text, then the card directive on its own paragraph:
     `::elevenlabs-sfx{id="<sound_id>"}`. The card plays, loops and downloads the sound. Do not add a
     MEDIA: line on Desktop (the card already shows the file).
   - Messaging (Telegram, Discord, Slack, WhatsApp, Signal…): put `MEDIA:<file_path>` on its own line
     for every file to send (the tool's `media_tag`). The file path appears ONLY on that line, never
     in prose, or the file is sent twice. Markdown links and images are not delivered.
   - CLI/terminal: give the file path.
   Completion: every sound produced is delivered once on the user's surface.
6. A sound pack: generate each sound (steps 3-4), then run `pack`; deliver the zip via `MEDIA:` on
   messaging, or the zip path on Desktop/CLI, plus the manifest summary. Completion: the zip exists and
   `manifest.json` lists every sound.
7. End with what was made: per sound the duration, format and the credits billed, plus the loop
   verdict when a loop was checked. Offer the next step (variations, shorter/longer, a loop, the other model).

## Pitfalls

- The format can't be chosen per call; convert locally instead of changing the user's setting.
- 44.1 kHz WAV and 192 kbps MP3 need higher ElevenLabs plans; a plan error is reported by the tool as
  "ElevenLabs returned HTTP 4xx: ...". Report it; suggest the default format.
- An auto-length sound (no `duration_seconds`) is billed by the prompt, not per second.
- The card directive is Desktop-only: other clients print it as text.
- Files live on the machine Hermes runs on. A Desktop connected to a remote Hermes still plays and
  downloads through the card; paths in replies are paths on that host.

## Verification

- Each delivered sound came from a successful tool result (`success: true`) or an `ok: true` script line.
- Loops you called seamless have a `loopcheck` verdict of `seamless`.
- On messaging, each file appears on exactly one `MEDIA:` line.
