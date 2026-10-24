"""What made each sound: one JSON line per generation in the plugin's own data directory.

The Desktop sound library and the in-chat sound card read these records through
``dashboard/plugin_api.py``. The directory is ``PluginState(<plugin>).data_dir`` (the same
``<HERMES_HOME>/plugin-data/<namespace>/`` that ``ctx.state`` uses), resolved on every call so a
multiplexed gateway and the Desktop backend each write and read the active profile's own file.

Records are appended, never rewritten: concurrent generations from the CLI, the gateway and
Desktop can't lose each other's lines, and a torn last line is skipped on read. Only the file NAME
is stored; the backend resolves it under its own ``cache/generated/audio`` so a record never points
outside that folder.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

PLUGIN_ID = "elevenlabs-sound-effects"
FILE_NAME = "sounds.jsonl"
SOUND_ID_RE = re.compile(r"[0-9a-f]{12}")
AUDIO_NAME_RE = re.compile(r"sfx_[a-z0-9-]{1,40}_\d{8}_\d{6}_[0-9a-f]{8,12}\.(?:mp3|opus|wav)")
# Read at most the newest ~2 MB (several thousand records); older lines stay on disk untouched.
_TAIL_BYTES = 2 * 1024 * 1024
_LOCK_OFFSET = 0x7FFFFFFF
# `duration_seconds` is the length requested (what ElevenLabs bills, null = automatic); `length_seconds`
# the length of the audio it returned, which can be longer (loops).
_FIELDS = ("id", "created_at", "prompt", "duration_seconds", "length_seconds", "loop", "prompt_influence",
           "model", "output_format", "file_name", "bytes", "billed_characters", "session_id")
# Desktop-card handshake (see ``mark_card_ready``): a hash of each sound prompt the Desktop half saw start.
SEEN_FILE_NAME = "desktop_cards.jsonl"
# How long before the tool's handler starts its tool.start sighting may land (the event fires first;
# an approval prompt can sit between them).
CARD_SLACK_SECONDS = 120.0
_SEEN_MAX_BYTES = 64 * 1024
_SEEN_TAIL_BYTES = 16 * 1024


def data_dir() -> Path:
    from hermes_cli.plugins_state import PluginState

    return PluginState(PLUGIN_ID).data_dir


def audio_dir() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "cache" / "generated" / "audio"


def record(entry: dict[str, Any]) -> None:
    """Append one generation. Never raises: the sound is already saved and billed, and a lost
    library row must not turn the tool call into an error."""
    row = {key: entry.get(key) for key in _FIELDS}
    row["created_at"] = row["created_at"] or datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds")
    line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
    try:
        _append(FILE_NAME, line)
    except (OSError, ImportError, RuntimeError, ValueError):
        pass


def _append(name: str, line: str) -> None:
    directory = data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    # O_APPEND: each write lands at the end even with several writers (one short line per write).
    fd = os.open(directory / name, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":  # pragma: no cover - Windows emulates O_APPEND with seek+write: lock it
            import msvcrt
            # Windows byte locks are mandatory: lock a sentinel byte far past any real end of file so
            # readers of the records never hit it, only other writers taking the same lock.
            os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            try:
                os.lseek(fd, 0, os.SEEK_END)
                os.write(fd, line.encode("utf-8"))
            finally:
                os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _prompt_key(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8", "surrogatepass")).hexdigest()[:32]


@contextlib.contextmanager
def _seen_lock():
    """Exclusive lock for the sightings file: appends, trims, clears and the tool's read-and-consume from
    the plugin host's threads and processes must not drop each other's lines. On Windows it also keeps
    the tool's read off the file while a trim or clear replaces it (a replace fails on an open file)."""
    directory = data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / f"{SEEN_FILE_NAME}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        if os.name == "nt":  # pragma: no cover - exercised by the Windows CI leg
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # byte 0; LK_LOCK retries for ~10 s before raising
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        locked = True
        yield directory / SEEN_FILE_NAME
    finally:
        if locked and os.name == "nt":  # pragma: no cover
            import msvcrt
            # Unlock explicitly: Windows releases a lock left at close only "when resources allow", and the
            # next writer would wait for it.
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)  # on POSIX closing releases the flock


def _rewrite(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except BaseException:
        # Disk full or a failed replace: never leave the half-written copy behind.
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def mark_card_ready(prompt: str, owner: str = "", session: str = "") -> None:
    """The Desktop half (instance ``owner``) saw a sound tool call with ``prompt`` start in chat
    ``session`` (the event's session id), so a card for its result will render there. Called by the
    backend route the Desktop half posts to; the tool reads it back with ``card_ready``. Only a hash of
    the prompt, the time, the owner token and the session id are stored."""
    row = {"key": _prompt_key(prompt), "at": round(time.time(), 3), "owner": owner[:64]}
    if session:
        row["session"] = session[:256]
    line = json.dumps(row) + "\n"
    with _seen_lock() as path:
        with open(path, "ab") as handle:
            handle.write(line.encode("utf-8"))
        if path.stat().st_size > _SEEN_MAX_BYTES:  # keep it small: only the last minutes matter
            _rewrite(path, path.read_bytes()[-_SEEN_TAIL_BYTES:].split(b"\n", 1)[-1])


def clear_card_marks(owner: str = "") -> None:
    """Desktop half ``owner`` was switched off (or unloaded): forget its sightings, so a call that
    repeats a recent prompt does not get a card line that nothing will draw. Other Desktops' sightings
    stay. An empty owner clears every sighting."""
    with _seen_lock() as path:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return
        keep = []
        for line in raw.splitlines(keepends=True):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if owner and isinstance(row, dict) and row.get("owner") != owner:
                keep.append(line)
        _rewrite(path, b"".join(keep))


def _pick_sighting(lines: list[bytes], key: str, since: float, sessions: set[str]) -> int | None:
    """Index of the oldest fresh sighting of this prompt that names one of ``sessions``, or None. A
    sighting that names no chat vouches for nothing: it can't say which call it saw."""
    now = time.time()
    for index, line in enumerate(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("key") != key:
            continue
        at = row.get("at")
        if isinstance(at, (int, float)) and since <= at <= now + 5 and row.get("session") in sessions:
            return index
    return None


def card_ready(prompt: str, since: float, sessions: set[str] | frozenset[str]) -> bool:
    """Did a Desktop with this plugin's Desktop half on see THIS sound call start: same ``prompt``, at or
    after ``since`` (the caller's start, less ``CARD_SLACK_SECONDS`` for an approval wait), in one of
    ``sessions`` (the calling turn's session ids)? A sighting that vouches is used up, so it can't vouch
    for a later call too. False on any doubt: the caller then delivers the plain ``MEDIA:`` line, which
    every surface plays."""
    sessions = {s for s in sessions if isinstance(s, str) and s}
    if not sessions:
        return False
    try:
        with _seen_lock() as path:
            try:
                raw = path.read_bytes()
            except FileNotFoundError:
                return False
            lines = raw.splitlines(keepends=True)
            index = _pick_sighting(lines, _prompt_key(prompt), since, sessions)
            if index is None:
                return False
            del lines[index]
            _rewrite(path, b"".join(lines))
            return True
    except (OSError, ImportError, RuntimeError, ValueError):
        return False


def read_all() -> list[dict[str, Any]]:
    """Every readable record, newest first, de-duplicated by id."""
    try:
        path = data_dir() / FILE_NAME
        with open(path, "rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - _TAIL_BYTES))
            raw = handle.read()
    except (OSError, ImportError, RuntimeError, ValueError):
        return []
    lines = raw.split(b"\n")
    if size > _TAIL_BYTES and lines:
        lines = lines[1:]  # the read started mid-file: the first line may be partial
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        sound_id = row.get("id")
        if not isinstance(sound_id, str) or not SOUND_ID_RE.fullmatch(sound_id) or sound_id in seen:
            continue
        seen.add(sound_id)
        rows.append({key: row.get(key) for key in _FIELDS})
    return rows


def count() -> int:
    """How many records the file holds (lines), including any older than the read window."""
    try:
        with open(data_dir() / FILE_NAME, "rb") as handle:
            return sum(chunk.count(b"\n") for chunk in iter(lambda: handle.read(1 << 20), b""))
    except (OSError, ImportError, RuntimeError, ValueError):
        return 0


def find(sound_id: str) -> dict[str, Any] | None:
    if not isinstance(sound_id, str) or not SOUND_ID_RE.fullmatch(sound_id):
        return None
    return next((row for row in read_all() if row["id"] == sound_id), None)


def audio_path(row: dict[str, Any]) -> Path | None:
    """The record's file inside this profile's ``cache/generated/audio``, or ``None`` when the name
    is not one this plugin writes, the file is gone, it resolves (symlink) outside the folder, or it
    is hard-linked (possibly to a file elsewhere)."""
    name = row.get("file_name")
    if not isinstance(name, str) or not AUDIO_NAME_RE.fullmatch(name):
        return None
    root = audio_dir()
    try:
        resolved = (root / name).resolve(strict=True)
        root_resolved = root.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if resolved.parent != root_resolved or not resolved.is_file():
        return None
    # A hard link has no symlink to resolve: refuse any extra link, the tool writes each file once.
    try:
        if resolved.stat().st_nlink > 1:
            return None
    except OSError:
        return None
    return resolved
