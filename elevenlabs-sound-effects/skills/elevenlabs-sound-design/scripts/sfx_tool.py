#!/usr/bin/env python3
"""Local post-processing for generated sound effects (stdlib + ffmpeg/ffprobe).

Subcommands: probe, process (trim silence, loudness-normalise, fades, convert), loopfix (crossfade
a loop's end into its start), loopcheck (seam click/gap check), pack (named folder + manifest.json +
zip). Every run prints exactly ONE JSON line on
stdout, ``{"ok": true, ...}`` or ``{"ok": false, "code": ..., "error": ...}``, and exits 0 or 1. It
never prints a traceback: ffmpeg missing, a bad input file or bad arguments all come back as that
line, so an agent can read the outcome instead of guessing.
"""

from __future__ import annotations

import argparse
import array
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

CODECS = {".ogg": ["-c:a", "libvorbis", "-q:a", "5"], ".wav": ["-c:a", "pcm_s16le"],
          ".mp3": ["-c:a", "libmp3lame", "-b:a", "192k"], ".opus": ["-c:a", "libopus", "-b:a", "128k"]}
FORMAT_EXT = {"ogg": ".ogg", "wav": ".wav", "mp3": ".mp3"}
_TIMEOUT = 300
_MAX_PCM_BYTES = 64 * 1024 * 1024
_LOOP_RATE = 48000


class Failure(Exception):
    def __init__(self, code: str, error: str):
        super().__init__(error)
        self.code, self.error = code, error


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse would print usage to stderr and exit 2
        raise Failure("bad_args", message)


def emit(payload: dict) -> int:
    # ASCII-only: a Windows pipe uses the ANSI code page, and a non-ASCII path must not turn the
    # one JSON line into a UnicodeEncodeError traceback.
    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
    sys.stdout.flush()
    return 0 if payload.get("ok") else 1


def _tool(name: str, override: str | None) -> str:
    found = shutil.which(override or name)
    if not found:
        raise Failure("ffmpeg_missing", f"{override or name} not found on PATH; install ffmpeg "
                                        "(it includes ffprobe) to process sounds locally")
    return found


def _input(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_file():
        raise Failure("bad_input", f"input file not found: {path}")
    return p.resolve()


def _run(cmd: list[str], *, binary: bool = False) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        raise Failure("ffmpeg_failed", f"{Path(cmd[0]).name} did not finish within {_TIMEOUT} s") from None
    except OSError as exc:
        raise Failure("ffmpeg_missing", f"could not run {cmd[0]}: {exc.strerror or exc}") from None
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        code = "bad_input" if any(s in " ".join(tail) for s in ("Invalid data", "could not find codec",
                                                                "does not contain any stream")) else "ffmpeg_failed"
        raise Failure(code, f"{Path(cmd[0]).name} failed: {' | '.join(tail) or 'exit ' + str(proc.returncode)}")
    return proc


def probe(path: Path, ffprobe: str) -> dict:
    proc = _run([ffprobe, "-v", "error", "-show_entries",
                 "format=duration,format_name:stream=codec_type,codec_name,sample_rate,channels",
                 "-of", "json", str(path)])
    try:
        data = json.loads(proc.stdout or b"{}")
    except ValueError:
        raise Failure("bad_input", f"ffprobe returned unreadable output for {path.name}") from None
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not audio:
        raise Failure("bad_input", f"{path.name} has no audio stream")
    fmt = data.get("format", {})
    duration = fmt.get("duration")
    return {"file": str(path), "duration_seconds": round(float(duration), 3) if duration else None,
            "codec": audio.get("codec_name"), "sample_rate": int(audio.get("sample_rate") or 0) or None,
            "channels": audio.get("channels"), "container": fmt.get("format_name"),
            "bytes": path.stat().st_size}


def _filters(trim: bool, loudness: float | None, fade_ms: float) -> list[str]:
    chain = []
    if trim:
        edge = "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.01"
        chain += [edge, "areverse", edge, "areverse"]
    if loudness is not None:
        chain.append(f"loudnorm=I={loudness:g}:TP=-1.5:LRA=11")
    if fade_ms > 0:
        d = fade_ms / 1000
        chain += [f"afade=t=in:d={d:g}", "areverse", f"afade=t=in:d={d:g}", "areverse"]
    return chain


def cmd_probe(args) -> dict:
    return {"ok": True, **probe(_input(args.input), _tool("ffprobe", args.ffprobe))}


def _encode(ffmpeg: str, src: Path, out: Path, chain: list[str], sample_rate: int | None) -> None:
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-vn"]
    if chain:
        cmd += ["-af", ",".join(chain)]
    if sample_rate:  # loudnorm resamples to 192 kHz internally; keep the source rate
        # libopus takes only 48/24/16/12/8 kHz.
        cmd += ["-ar", "48000" if out.suffix.lower() == ".opus" else str(sample_rate)]
    cmd += CODECS[out.suffix.lower()]
    # Write beside the target and rename, so a failed run never leaves a half-written file at OUT.
    fd, tmp = tempfile.mkstemp(prefix=".sfx_tool_", suffix=out.suffix, dir=out.parent)
    os.close(fd)
    try:
        _run(cmd + [tmp])
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _output(path: str, src: Path, force: bool) -> Path:
    out = Path(path).expanduser()
    if out.suffix.lower() not in CODECS:
        raise Failure("bad_args", f"output extension must be one of {', '.join(sorted(CODECS))}")
    if not out.parent.is_dir():
        raise Failure("bad_args", f"output folder does not exist: {out.parent}")
    out = out.resolve()
    if out == src:
        raise Failure("bad_args", "output must differ from the input (the input is never overwritten)")
    if out.exists() and not force:
        raise Failure("bad_args", f"{out} exists; pass --force to replace it")
    return out


def cmd_process(args) -> dict:
    src = _input(args.input)
    ffmpeg, ffprobe = _tool("ffmpeg", args.ffmpeg), _tool("ffprobe", args.ffprobe)
    out = _output(args.out, src, args.force)
    if args.loudness is not None and not -70 <= args.loudness <= -5:
        raise Failure("bad_args", "--loudness must be between -70 and -5 LUFS")
    if not 0 <= args.fade_ms <= 5000:
        raise Failure("bad_args", "--fade-ms must be between 0 and 5000")
    before = probe(src, ffprobe)
    chain = _filters(args.trim, args.loudness, args.fade_ms)
    _encode(ffmpeg, src, out, chain, before["sample_rate"] if args.loudness is not None else None)
    after = probe(out, ffprobe)
    return {"ok": True, "input": before, "output": after,
            "applied": {"trim_silence": args.trim, "loudness_lufs": args.loudness, "fade_ms": args.fade_ms}}


def _pcm_frames(ffmpeg: str, src: Path, channels: int, rate: int) -> array.array:
    proc = _run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(src), "-vn",
                 "-ac", str(channels), "-ar", str(rate), "-f", "s16le", "-acodec", "pcm_s16le", "-"])
    raw = proc.stdout[:_MAX_PCM_BYTES]
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) - len(raw) % (2 * channels)])
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def cmd_loopfix(args) -> dict:
    """Crossfade the loop's last N ms into its first N ms (equal-power) and drop that tail: playback
    then runs from the blended start straight out of the original audio, with no dip or gap."""
    src = _input(args.input)
    ffmpeg, ffprobe = _tool("ffmpeg", args.ffmpeg), _tool("ffprobe", args.ffprobe)
    out = _output(args.out, src, args.force)
    if not 1 <= args.crossfade_ms <= 2000:
        raise Failure("bad_args", "--crossfade-ms must be between 1 and 2000")
    before = probe(src, ffprobe)
    channels, rate = int(before["channels"] or 2), int(before["sample_rate"] or 48000)
    if out.suffix.lower() == ".opus":
        rate = 48000
    samples = _pcm_frames(ffmpeg, src, channels, rate)
    frames = len(samples) // channels
    n = int(rate * args.crossfade_ms / 1000)
    if frames < 3 * n:
        raise Failure("bad_input", f"{src.name} is too short for a {args.crossfade_ms:g} ms crossfade")
    keep = frames - n
    blended = array.array("h", samples[: keep * channels])
    for i in range(n):
        t = (i + 0.5) / n
        fade_in, fade_out = math.sin(t * math.pi / 2), math.cos(t * math.pi / 2)
        for c in range(channels):
            v = samples[i * channels + c] * fade_in + samples[(keep + i) * channels + c] * fade_out
            blended[i * channels + c] = max(-32768, min(32767, int(round(v))))
    if sys.byteorder == "big":
        blended.byteswap()
    fd, raw_path = tempfile.mkstemp(prefix=".sfx_tool_", suffix=".pcm", dir=out.parent)
    with os.fdopen(fd, "wb") as handle:
        handle.write(blended.tobytes())
    try:
        raw_src = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "s16le",
                   "-ar", str(rate), "-ac", str(channels), "-i", raw_path]
        tmp_fd, tmp = tempfile.mkstemp(prefix=".sfx_tool_", suffix=out.suffix, dir=out.parent)
        os.close(tmp_fd)
        try:
            _run(raw_src + CODECS[out.suffix.lower()] + [tmp])
            os.replace(tmp, out)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    finally:
        os.unlink(raw_path)
    return {"ok": True, "input": before, "output": probe(out, ffprobe), "crossfade_ms": args.crossfade_ms}


def cmd_loopcheck(args) -> dict:
    src = _input(args.input)
    samples = _pcm_frames(_tool("ffmpeg", args.ffmpeg), src, 1, _LOOP_RATE)
    if len(samples) < _LOOP_RATE // 20:
        raise Failure("bad_input", f"{src.name} is shorter than 50 ms; nothing to loop")
    scale = 32768.0
    diffs = sorted(abs(samples[i + 1] - samples[i]) for i in range(0, len(samples) - 1, 7))
    typical = diffs[int(len(diffs) * 0.99)] / scale
    seam = abs(samples[0] - samples[-1]) / scale
    edge = _LOOP_RATE // 100  # 10 ms either side of the seam

    def rms(chunk) -> float:
        return math.sqrt(sum(s * s for s in chunk) / max(1, len(chunk))) / scale

    head, tail = rms(samples[:edge]), rms(samples[-edge:])
    level_db = 20 * math.log10(max(head, 1e-6) / max(tail, 1e-6))
    overall = rms(samples)
    # A fade to silence at the seam is a dropout on every repeat even though the edge samples match:
    # look at the 1 ms either side of the seam together.
    ms = _LOOP_RATE // 1000
    seam_level = rms(samples[-ms:].tolist() + samples[:ms].tolist())
    gap = overall > 0.01 and seam_level < overall * 0.2
    click = seam > max(2 * typical, 0.02) or abs(level_db) > 6
    verdict = "gap" if gap else "click" if click else "seamless"
    result = {"ok": True, "file": str(src), "verdict": verdict,
              "seam_jump": round(seam, 4), "typical_step_p99": round(typical, 4),
              "edge_level_difference_db": round(level_db, 1), "duration_seconds": round(len(samples) / _LOOP_RATE, 3)}
    if src.suffix.lower() == ".mp3":
        result["note"] = ("MP3 decoders add encoder padding at the start; convert the loop to .ogg or .wav "
                          "for engine playback and check that file")
    return result


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48]


def cmd_pack(args) -> dict:
    name = _slug(args.name)
    if not name:
        raise Failure("bad_args", "--name must contain letters or digits")
    out_dir = Path(args.out_dir).expanduser().resolve()
    if not out_dir.is_dir():
        raise Failure("bad_args", f"--out-dir does not exist: {out_dir}")
    sources = [_input(f) for f in args.files]
    prompts: dict = {}
    if args.prompts:
        try:
            prompts = json.loads(Path(args.prompts).expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise Failure("bad_args", f"--prompts must be a JSON object file: {exc}") from None
        if not isinstance(prompts, dict):
            raise Failure("bad_args", "--prompts must map file names to prompts (a JSON object)")
    ffprobe = _tool("ffprobe", args.ffprobe)
    ffmpeg = _tool("ffmpeg", args.ffmpeg) if args.format else None
    folder = out_dir / name
    zip_path = out_dir / f"{name}.zip"
    if (folder.exists() or folder.is_symlink() or zip_path.exists() or zip_path.is_symlink()) and not args.force:
        raise Failure("bad_args", f"{folder} or {zip_path} exists; pass --force to replace them")
    # Build in a fresh staging folder and swap it in by rename: a re-pack never keeps files from an
    # earlier pack, and never writes through a symlink left in the old folder or at the zip path.
    staging = Path(tempfile.mkdtemp(prefix=f".{name}.staging-", dir=out_dir))
    try:
        entries = _fill_pack(staging, sources, prompts, args, ffmpeg, ffprobe)
        manifest = {"name": args.name, "sounds": entries}
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                                               encoding="utf-8")
        fd, tmp_zip = tempfile.mkstemp(prefix=f".{name}.", suffix=".zip", dir=out_dir)
        os.close(fd)
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(staging.iterdir()):
                archive.write(path, f"{name}/{path.name}")
        os.replace(tmp_zip, zip_path)  # replaces a symlink itself, never its target
        if folder.is_symlink() or folder.is_file():
            folder.unlink()
        elif folder.exists():
            old = Path(tempfile.mkdtemp(prefix=f".{name}.old-", dir=out_dir))
            os.replace(folder, old / name)
            shutil.rmtree(old)  # rmtree removes links inside, never what they point to
        os.replace(staging, folder)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {"ok": True, "folder": str(folder), "zip": str(zip_path), "count": len(entries),
            "manifest": str(folder / "manifest.json"), "sounds": entries}


def _fill_pack(folder: Path, sources: list[Path], prompts: dict, args, ffmpeg: str | None, ffprobe: str) -> list[dict]:
    entries, used = [], set()
    for index, src in enumerate(sources, 1):
        ext = FORMAT_EXT[args.format] if args.format else src.suffix.lower()
        stem = _slug(re.sub(r"^sfx_|_\d{8}_\d{6}_[0-9a-f]+$", "", src.stem)) or "sound"
        target = folder / f"{index:02d}-{stem}{ext}"
        while target.name in used:
            target = target.with_name(f"{target.stem}-x{ext}")
        used.add(target.name)
        if ffmpeg and src.suffix.lower() != ext:
            _encode(ffmpeg, src, target, [], None)
        else:
            shutil.copyfile(src, target)
        info = probe(target, ffprobe)
        entry = {"file": target.name, "source": src.name, "duration_seconds": info["duration_seconds"],
                 "sample_rate": info["sample_rate"], "channels": info["channels"], "codec": info["codec"],
                 "bytes": info["bytes"]}
        prompt = prompts.get(src.name) or prompts.get(str(src))
        if isinstance(prompt, str):
            entry["prompt"] = prompt
        entries.append(entry)
    return entries


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="sfx_tool.py", description="Local post-processing for generated sound effects.")
    parser.add_argument("--ffmpeg", help="ffmpeg executable (default: ffmpeg on PATH)")
    parser.add_argument("--ffprobe", help="ffprobe executable (default: ffprobe on PATH)")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    p = sub.add_parser("probe", help="duration, codec, sample rate, channels")
    p.add_argument("input")
    p.set_defaults(fn=cmd_probe)
    p = sub.add_parser("process", help="trim silence, normalise loudness, fade, convert")
    p.add_argument("input")
    p.add_argument("--out", required=True, help="output file; its extension picks the format (.ogg .wav .mp3 .opus)")
    p.add_argument("--trim", dest="trim", action="store_true", default=False,
                   help="trim leading/trailing silence (one-shots; never on loops)")
    p.add_argument("--no-trim", dest="trim", action="store_false", help="keep the edges (the default)")
    p.add_argument("--loudness", type=float, default=None, help="target integrated loudness in LUFS, e.g. -16")
    p.add_argument("--fade-ms", type=float, default=0.0, help="fade in and out, milliseconds")
    p.add_argument("--force", action="store_true", help="replace an existing output file")
    p.set_defaults(fn=cmd_process)
    p = sub.add_parser("loopfix", help="crossfade a loop's end into its start (fixes a click without a gap)")
    p.add_argument("input")
    p.add_argument("--out", required=True, help="output file; its extension picks the format")
    p.add_argument("--crossfade-ms", type=float, default=20.0, help="crossfade length, milliseconds (default 20)")
    p.add_argument("--force", action="store_true", help="replace an existing output file")
    p.set_defaults(fn=cmd_loopfix)
    p = sub.add_parser("loopcheck", help="check a loop's seam for a click or a gap")
    p.add_argument("input")
    p.set_defaults(fn=cmd_loopcheck)
    p = sub.add_parser("pack", help="named folder + manifest.json + zip")
    p.add_argument("files", nargs="+")
    p.add_argument("--name", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--format", choices=sorted(FORMAT_EXT), help="convert every file to this format")
    p.add_argument("--prompts", help="JSON file mapping source file names to the prompts that made them")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_pack)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return emit(args.fn(args))
    except Failure as exc:
        return emit({"ok": False, "code": exc.code, "error": exc.error})
    except SystemExit as exc:  # --help
        return int(exc.code or 0)
    except KeyboardInterrupt:
        return emit({"ok": False, "code": "interrupted", "error": "interrupted"})
    except Exception as exc:  # noqa: BLE001 -- the contract is one JSON line, never a traceback
        return emit({"ok": False, "code": "internal", "error": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    sys.exit(main())
