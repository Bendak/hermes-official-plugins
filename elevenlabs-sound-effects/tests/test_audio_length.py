"""The sound's real length, read from the returned bytes with the standard library. ElevenLabs can
return more audio than requested (live: a 1 s loop came back 1.5 s, 2 s loops 2.25 s), so the tool
reports what it saved; these fixtures are built byte by byte in the three containers the plugin
offers. The real-file corpus check (72 ElevenLabs files vs ffmpeg's decoded length) is in the PR."""

from __future__ import annotations

from elevenlabs_sfx_plugin.audio_length import audio_seconds


def test_wav_length_comes_from_the_header_and_streamed_sizes_use_the_bytes(audio):
    assert audio_seconds(audio.wav(1.5), "wav") == 1.5
    assert audio_seconds(audio.wav(2.25, rate=44100, channels=1), "wav") == 2.25
    assert audio_seconds(audio.wav(1.5, streamed=True), "wav") == 1.5


def test_mp3_length_counts_frames_minus_the_encoder_delay_and_padding(audio):
    # 98 frames x 1152 - (576 + 1575) = 110745 samples = 2.511 s; ffmpeg decodes exactly that.
    assert audio_seconds(audio.mp3(98), "mp3") == round((98 * 1152 - 2151) / 44100, 3)
    assert audio_seconds(audio.mp3(98, delay=0, padding=0), "mp3") == round(98 * 1152 / 44100, 3)


def test_opus_length_drops_the_pre_skip_and_the_start_offset(audio):
    assert audio_seconds(audio.opus(72000), "opus") == 1.5
    # ElevenLabs' Opus files start 312 samples late (first page granule > its packets): ffmpeg plays 1.000 s.
    assert audio_seconds(audio.opus(48000, offset=312), "opus") == 1.0


def test_random_bytes_are_not_mistaken_for_mp3():
    import random

    rng = random.Random(7)
    hits = [n for n in range(1500) if audio_seconds(bytes(rng.getrandbits(8) for _ in range(600)), "mp3")]
    # One plausible header is not a stream (a single-header check accepted ~28 %); a run of 3 is.
    assert len(hits) <= 5, len(hits)


def test_unreadable_audio_has_no_length_and_never_raises(audio):
    for data, ext in ((b"", "wav"), (b"RIFF\x24\x00\x00\x00WAVEfmt fake", "wav"), (b"ID3fake-mp3", "mp3"),
                      (b"\xff\xfb" * 3, "mp3"), (b"OggS", "opus"), (audio.wav(1.0), "flac")):
        assert audio_seconds(data, ext) is None, (data[:12], ext)


def test_a_length_the_bytes_cannot_hold_is_no_length(audio):
    """One corrupt byte in the last Ogg page's granule made a 1.5 s file read as months; a WAV header
    with a byte rate of 1 does the same. No real file in these formats is under ~6 kbit/s."""
    import struct

    opus = bytearray(audio.opus(72000))
    last = opus.rfind(b"OggS")
    struct.pack_into("<q", opus, last + 6, 2**40)
    assert audio_seconds(bytes(opus), "opus") is None
    wav = bytearray(audio.wav(1.0))
    struct.pack_into("<I", wav, wav.find(b"fmt ") + 16, 1)  # byte rate
    assert audio_seconds(bytes(wav), "wav") is None
    assert audio_seconds(audio.opus(72000), "opus") == 1.5 and audio_seconds(audio.wav(1.0), "wav") == 1.0


def test_the_mp3_start_is_looked_for_only_in_the_first_64_kib(audio):
    """A junk 200 response (up to the 50 MB cap) must not cost seconds of scanning: a frame run that
    starts past 64 KiB of junk is not taken for this file's audio."""
    mp3 = audio.mp3(40)[20:]  # no ID3 tag: the junk comes first
    expected = round((40 * 1152 - 2151) / 44100, 3)
    assert audio_seconds(b"\x00" * 1000 + mp3, "mp3") == expected
    assert audio_seconds(b"\x00" * 70_000 + mp3, "mp3") is None
    assert audio_seconds(b"\xff" * 70_000 + mp3, "mp3") is None


def test_mutated_and_cross_format_inputs_never_raise_or_claim_too_much(audio):
    """Bounded fuzz (fixed seed, fixed run count): truncations, bit flips, splices, cuts and garbage of
    every container, also fed to the wrong reader, plus empty and one-byte inputs. Nothing raises and no
    length exceeds what the bytes can hold."""
    import random

    rng = random.Random(181)
    seeds = [(audio.mp3(30), "mp3"), (audio.mp3(30, delay=0, padding=0), "mp3"), (audio.wav(0.5), "wav"),
             (audio.wav(0.5, streamed=True), "wav"), (audio.opus(24000), "opus"),
             (audio.opus(48000, offset=312), "opus"), (b"", "mp3"), (b"\xff", "opus"), (b"R", "wav")]

    def mutate(data: bytes) -> bytes:
        b = bytearray(data)
        kind = rng.randrange(6)
        if not b or kind == 5:
            return bytes(rng.getrandbits(8) for _ in range(rng.randrange(0, 3000)))
        if kind == 0:
            return bytes(b[:rng.randrange(len(b) + 1)])
        if kind == 1:
            for _ in range(rng.randrange(1, 32)):
                b[rng.randrange(len(b))] = rng.getrandbits(8)
            return bytes(b)
        at = rng.randrange(len(b))
        if kind == 2:
            return bytes(b[:at] + bytes(rng.getrandbits(8) for _ in range(rng.randrange(1, 512))) + b[at:])
        if kind == 3:
            return bytes(b[:at] + b[at + rng.randrange(1, 2048):])
        return bytes(b[at:])

    for _ in range(4000):
        data, ext = rng.choice(seeds)
        for _ in range(rng.randrange(1, 4)):  # chained mutations reach empty and tiny inputs too
            data = mutate(data)
        for reader in (ext, rng.choice(("mp3", "wav", "opus"))):
            seconds = audio_seconds(data, reader)
            assert seconds is None or 0 < seconds <= len(data) * 8 / 4000, (reader, len(data), seconds)
    for ext in ("mp3", "wav", "opus"):
        assert audio_seconds(b"", ext) is None and audio_seconds(b"\xff", ext) is None
