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

import datetime
import json
import os
import re
from pathlib import Path
from typing import Any

PLUGIN_ID = "elevenlabs-sound-effects"
FILE_NAME = "sounds.jsonl"
SOUND_ID_RE = re.compile(r"[0-9a-f]{12}")
AUDIO_NAME_RE = re.compile(r"sfx_[a-z0-9-]{1,40}_\d{8}_\d{6}_[0-9a-f]{8,12}\.(?:mp3|opus|wav)")
# Read at most the newest ~2 MB (several thousand records); older lines stay on disk untouched.
_TAIL_BYTES = 2 * 1024 * 1024
_FIELDS = ("id", "created_at", "prompt", "duration_seconds", "loop", "prompt_influence", "model",
           "output_format", "file_name", "bytes", "billed_characters", "session_id")


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
        directory = data_dir()
        directory.mkdir(parents=True, exist_ok=True)
        # O_APPEND: each write lands at the end even with several writers (one short line per write).
        fd = os.open(directory / FILE_NAME, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":  # pragma: no cover - Windows emulates O_APPEND with seek+write: lock it
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                try:
                    os.lseek(fd, 0, os.SEEK_END)
                    os.write(fd, line.encode("utf-8"))
                finally:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except (OSError, ImportError, RuntimeError, ValueError):
        pass


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


def find(sound_id: str) -> dict[str, Any] | None:
    if not isinstance(sound_id, str) or not SOUND_ID_RE.fullmatch(sound_id):
        return None
    return next((row for row in read_all() if row["id"] == sound_id), None)


def audio_path(row: dict[str, Any]) -> Path | None:
    """The record's file inside this profile's ``cache/generated/audio``, or ``None`` when the name
    is not one this plugin writes, the file is gone, or it resolves (symlink) outside the folder."""
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
    return resolved
