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


def test_opus_length_is_the_last_granule_minus_the_pre_skip(audio):
    assert audio_seconds(audio.opus(72000), "opus") == 1.5


def test_unreadable_audio_has_no_length_and_never_raises(audio):
    for data, ext in ((b"", "wav"), (b"RIFF\x24\x00\x00\x00WAVEfmt fake", "wav"), (b"ID3fake-mp3", "mp3"),
                      (b"\xff\xfb" * 3, "mp3"), (b"OggS", "opus"), (audio.wav(1.0), "flac")):
        assert audio_seconds(data, ext) is None, (data[:12], ext)
