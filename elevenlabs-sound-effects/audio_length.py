"""Playing length of the audio ElevenLabs returns, read from the bytes with the standard library.

ElevenLabs can return more audio than was asked for (a 1 s loop came back 1.5 s long, 2 s loops 2.25 s),
so the tool reports the length of the file it saved, not the request. Handles every ``output_format``
the plugin offers: MP3 (frame scan), WAV (RIFF header) and Ogg Opus (last granule position minus the
pre-skip). Returns ``None`` for anything it cannot read; it never raises and never runs ffprobe.
"""

from __future__ import annotations

import struct

# MPEG audio frame header tables: bitrates in kbps by [version is MPEG1][layer][index].
_BITRATES = {
    (True, 1): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (True, 2): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (True, 3): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (False, 1): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (False, 2): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    (False, 3): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_SAMPLE_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def _mp3_frame(data: bytes, pos: int) -> tuple[int, int, int] | None:
    """``(frame bytes, samples, sample rate)`` of the MPEG audio frame at ``pos``, or None."""
    if pos + 4 > len(data) or data[pos] != 0xFF or data[pos + 1] & 0xE0 != 0xE0:
        return None
    version, layer_bits = (data[pos + 1] >> 3) & 3, (data[pos + 1] >> 1) & 3
    bitrate_index, rate_index = data[pos + 2] >> 4, (data[pos + 2] >> 2) & 3
    if version == 1 or layer_bits == 0 or bitrate_index in (0, 15) or rate_index == 3:
        return None
    layer = 4 - layer_bits
    mpeg1 = version == 3
    bitrate = _BITRATES[(mpeg1, layer)][bitrate_index] * 1000
    rate = _SAMPLE_RATES[version][rate_index]
    padding = (data[pos + 2] >> 1) & 1
    if layer == 1:
        return (12 * bitrate // rate + padding) * 4, 384, rate
    samples = 1152 if layer == 2 or mpeg1 else 576
    return samples // 8 * bitrate // rate + padding, samples, rate


def _info_frame_trim(frame: bytes) -> int | None:
    """For a Xing/Info header frame (it carries no audio): the encoder delay + padding its LAME
    extension records, which decoders drop (0 without one). None when ``frame`` is an audio frame."""
    for tag in (b"Xing", b"Info"):
        at = frame.find(tag)
        if at >= 0:
            break
    else:
        return 0 if b"VBRI" in frame else None
    flags = struct.unpack_from(">I", frame, at + 4)[0]
    ext = at + 8 + 4 * bool(flags & 1) + 4 * bool(flags & 2) + 100 * bool(flags & 4) + 4 * bool(flags & 8)
    if ext + 24 > len(frame):
        return 0
    b = frame[ext + 21:ext + 24]
    return (b[0] << 4 | b[1] >> 4) + ((b[1] & 0x0F) << 8 | b[2])


def mp3_seconds(data: bytes) -> float | None:
    pos = 0
    if data[:3] == b"ID3" and len(data) >= 10:
        size = (data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 | (data[8] & 0x7F) << 7 | (data[9] & 0x7F)
        pos = 10 + size + (10 if data[5] & 0x10 else 0)
    samples, rate, first, trim = 0, 0, True, 0
    while pos < len(data):
        frame = _mp3_frame(data, pos)
        if frame is None:
            if first:  # resync once past junk before the first frame
                nxt = data.find(b"\xff", pos + 1)
                if nxt < 0:
                    break
                pos = nxt
                continue
            break
        size, count, rate = frame
        if size <= 4:
            break
        info_trim = _info_frame_trim(data[pos:pos + size]) if first else None
        if info_trim is None:
            samples += count
        else:
            trim = info_trim
        first = False
        pos += size
    if trim < samples:
        samples -= trim
    return samples / rate if samples and rate else None


def wav_seconds(data: bytes) -> float | None:
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    pos, byte_rate = 12, 0
    while pos + 8 <= len(data):
        tag, size = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        body = pos + 8
        if tag == b"fmt " and size >= 16 and body + 16 <= len(data):
            byte_rate = struct.unpack_from("<I", data, body + 8)[0]
        elif tag == b"data":
            # A streamed WAV can declare 0 or 0xFFFFFFFF; the bytes that follow are the audio.
            available = len(data) - body
            size = available if size in (0, 0xFFFFFFFF) or size > available else size
            return size / byte_rate if byte_rate else None
        pos = body + size + (size & 1)
    return None


def opus_seconds(data: bytes) -> float | None:
    head = data.find(b"OpusHead")
    last = data.rfind(b"OggS")
    if head < 0 or last < 0 or head + 12 > len(data) or last + 14 > len(data):
        return None
    pre_skip = struct.unpack_from("<H", data, head + 10)[0]
    granule = struct.unpack_from("<q", data, last + 6)[0]
    if granule <= pre_skip:
        return None
    return (granule - pre_skip) / 48000  # Opus granule positions always count 48 kHz samples


_READERS = {"mp3": mp3_seconds, "wav": wav_seconds, "opus": opus_seconds}


def audio_seconds(data: bytes, extension: str) -> float | None:
    """Length in seconds (3 decimals) of ``data`` saved as ``extension``; None when unreadable."""
    reader = _READERS.get(extension)
    try:
        seconds = reader(data) if reader else None
    except (struct.error, IndexError, KeyError, ZeroDivisionError):
        seconds = None
    return round(seconds, 3) if seconds and seconds > 0 else None
