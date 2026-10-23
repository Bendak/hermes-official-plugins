"""The sound library end to end: the real plugin, loaded by core's PluginManager from a temp
HERMES_HOME, generates against a loopback stand-in; each generation's provenance record is then read
back through the plugin's own ``dashboard/plugin_api.py`` router (FastAPI TestClient), the way the
Desktop card and library read it. Path guards, profile isolation and the conversion paths are here too.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import hermes_yaml as yaml
import pytest

PLUGIN = "elevenlabs-sound-effects"
TOOL = "elevenlabs_sound_effect"
AUDIO = b"ID3fake-mp3-bytes-for-the-library"


class _StandIn:
    """``POST /v1/sound-generation`` answering audio bytes and a ``character-cost`` header."""

    def __init__(self):
        self.bodies: list[dict] = []
        stand_in = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                stand_in.bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", str(len(AUDIO)))
                self.send_header("character-cost", "20")
                self.end_headers()
                self.wfile.write(AUDIO)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stand_in():
    server = _StandIn()
    yield server
    server.close()


@pytest.fixture
def loaded(_hermes_home, plugin_root, stand_in, monkeypatch):
    from hermes_cli import plugins as plugins_mod
    from hermes_cli.plugins import PluginManager
    from tools.registry import registry

    shutil.copytree(plugin_root, _hermes_home / "plugins" / PLUGIN,
                    ignore=shutil.ignore_patterns("tests", "__pycache__", ".pytest_cache", "node_modules"))
    (_hermes_home / "config.yaml").write_text(yaml.safe_dump({"plugins": {
        "enabled": [PLUGIN], "entries": {PLUGIN: {"settings": {"api_base_url": stand_in.url}}}}}),
        encoding="utf-8")
    empty_bundled = _hermes_home.parent / "bundled"
    empty_bundled.mkdir()
    monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: empty_bundled)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-test-key")
    manager = PluginManager()
    manager.discover_and_load()
    assert registry.get_entry(TOOL, scope=manager.scope_key) is not None

    def call(args: dict, session_id: str = "") -> dict:
        return json.loads(registry.dispatch(TOOL, args, scope=manager.scope_key, session_id=session_id))

    try:
        yield manager, call
    finally:
        manager.unload()


@pytest.fixture
def api(plugin_root):
    """The plugin's router, imported standalone like the plugin host does (no package)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    spec = importlib.util.spec_from_file_location("sfx_plugin_api_under_test", plugin_root / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router)
    return module, TestClient(app)


def test_each_generation_is_recorded_and_served_to_the_desktop(loaded, api, stand_in, _hermes_home):
    manager, call = loaded
    result = call({"prompt": "Retro coin pickup", "duration_seconds": 2, "prompt_influence": 0.6,
                   "model": "eleven_text_to_sound_v3"}, session_id="20261023_120000_abcdef")
    assert result["success"] is True
    sound_id = result["sound_id"]
    assert stand_in.bodies[-1]["model_id"] == "eleven_text_to_sound_v3"  # per-call model overrides the setting
    assert result["desktop_card"] == f'::elevenlabs-sfx{{id="{sound_id}"}}'
    assert Path(result["file_path"]).stem.endswith(sound_id)

    _, client = api
    listing = client.get("/sounds").json()
    (row,) = listing["sounds"]
    assert row == {**row, "id": sound_id, "prompt": "Retro coin pickup", "duration_seconds": 2.0,
                   "loop": False, "prompt_influence": 0.6, "model": "eleven_text_to_sound_v3",
                   "output_format": "mp3_44100_128", "billed_characters": "20",
                   "session_id": "20261023_120000_abcdef", "kind": "mp3", "exists": True,
                   "size": len(AUDIO), "path": result["file_path"]}
    assert listing["totals"]["mp3"] == {"count": 1, "bytes": len(AUDIO), "missing": 0}

    assert client.get(f"/sound?id={sound_id}").json()["sound"]["id"] == sound_id
    audio = client.get(f"/audio?id={sound_id}&format=original").json()
    assert audio["mime"] == "audio/mpeg" and audio["file_name"].endswith(".mp3")
    assert base64.b64decode(audio["data_url"].split(",", 1)[1]) == AUDIO

    # The record lives in the plugin's own data dir (the one ctx.state uses), never in the plugin folder.
    from hermes_cli.plugins_state import PluginState

    assert (PluginState(PLUGIN).data_dir / "sounds.jsonl").is_file()
    assert not list((_hermes_home / "plugins" / PLUGIN).rglob("sounds.jsonl"))


def test_a_failed_or_refused_call_records_nothing(loaded, api):
    _, call = loaded
    assert "error" in call({"prompt": "x", "model": "eleven_text_to_sound_v9"})
    assert "error" in call({"prompt": ""})
    assert api[1].get("/sounds").json()["total"] == 0


def test_bad_ids_and_records_never_reach_files_outside_the_audio_folder(api, _hermes_home):
    module, client = api
    prov = module._prov
    secret = _hermes_home / ".env"
    secret.write_text("ELEVENLABS_API_KEY=sk_secret\n", encoding="utf-8")
    audio_dir = _hermes_home / "cache" / "generated" / "audio"
    audio_dir.mkdir(parents=True)
    link = audio_dir / "sfx_link_20261023_120000_aaaaaaaaaaaa.mp3"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError):
        link = None
    prov.record({"id": "aaaaaaaaaaaa", "prompt": "symlink", "output_format": "mp3_44100_128",
                 "file_name": link.name if link else "sfx_gone_20261023_120000_aaaaaaaaaaaa.mp3"})
    prov.record({"id": "bbbbbbbbbbbb", "prompt": "traversal", "output_format": "mp3_44100_128",
                 "file_name": "../../.env"})

    assert client.get("/audio?id=aaaaaaaaaaaa").status_code == 410
    assert client.get("/audio?id=bbbbbbbbbbbb").status_code == 410
    rows = {r["id"]: r for r in client.get("/sounds").json()["sounds"]}
    assert rows["aaaaaaaaaaaa"]["exists"] is False and rows["bbbbbbbbbbbb"]["path"] is None
    assert client.get("/audio?id=../../x").status_code == 400
    assert client.get("/sound?id=cccccccccccc").status_code == 404
    assert client.get("/audio?id=aaaaaaaaaaaa&format=flac").status_code == 400


def test_each_profile_sees_only_its_own_library(api, tmp_path, monkeypatch):
    module, client = api
    homes = [tmp_path / "p1", tmp_path / "p2"]
    for i, home in enumerate(homes):
        monkeypatch.setenv("HERMES_HOME", str(home))
        audio_dir = home / "cache" / "generated" / "audio"
        audio_dir.mkdir(parents=True)
        name = f"sfx_p{i}_20261023_120000_{i:012x}.wav"
        (audio_dir / name).write_bytes(b"RIFF....WAVE")
        module._prov.record({"id": f"{i:012x}", "prompt": f"profile {i}", "output_format": "wav_48000",
                             "file_name": name})
    for i, home in enumerate(homes):
        monkeypatch.setenv("HERMES_HOME", str(home))
        assert [r["prompt"] for r in client.get("/sounds").json()["sounds"]] == [f"profile {i}"]


def test_conversion_needs_ffmpeg_and_says_so(api, _hermes_home, monkeypatch):
    module, client = api
    audio_dir = _hermes_home / "cache" / "generated" / "audio"
    audio_dir.mkdir(parents=True)
    name = "sfx_beep_20261023_120000_dddddddddddd.wav"
    _write_tone(audio_dir / name)
    module._prov.record({"id": "dddddddddddd", "prompt": "beep", "output_format": "wav_48000", "file_name": name})

    monkeypatch.setattr(module.shutil, "which", lambda _name: None)
    assert client.get("/sounds").json()["conversions"] == []
    refused = client.get("/audio?id=dddddddddddd&format=ogg")
    assert refused.status_code == 409 and "ffmpeg" in refused.json()["detail"]
    # The original always downloads, ffmpeg or not.
    assert client.get("/audio?id=dddddddddddd&format=original").status_code == 200
    assert client.get("/audio?id=dddddddddddd&format=wav").json()["format"] == "wav"  # same container: no ffmpeg


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed on this runner")
def test_converts_to_every_offered_format_with_ffmpeg(api, _hermes_home):
    module, client = api
    audio_dir = _hermes_home / "cache" / "generated" / "audio"
    audio_dir.mkdir(parents=True)
    name = "sfx_beep_20261023_120000_eeeeeeeeeeee.wav"
    _write_tone(audio_dir / name)
    module._prov.record({"id": "eeeeeeeeeeee", "prompt": "beep", "output_format": "wav_48000", "file_name": name})
    for fmt in client.get("/sounds").json()["conversions"]:
        payload = client.get(f"/audio?id=eeeeeeeeeeee&format={fmt}").json()
        data = base64.b64decode(payload["data_url"].split(",", 1)[1])
        assert payload["format"] == fmt and len(data) > 100
        assert data[:4] in {b"RIFF", b"OggS", b"ID3\x03", b"ID3\x04"} or data[:2] == b"\xff\xfb"
    assert not list((module._prov.data_dir() / "convert").iterdir())  # temp files cleaned up


def test_a_record_file_bigger_than_the_read_window_keeps_whole_records(api, monkeypatch):
    module, client = api
    prov = module._prov
    for i in range(30):
        prov.record({"id": f"{i:012x}", "prompt": "x" * 40})
    size = (prov.data_dir() / prov.FILE_NAME).stat().st_size
    monkeypatch.setattr(prov, "_TAIL_BYTES", size)  # exactly the file: the first record must survive
    assert len(prov.read_all()) == 30
    monkeypatch.setattr(prov, "_TAIL_BYTES", size - 10)  # mid-record start: that partial line is skipped
    assert len(prov.read_all()) == 29


def test_the_bundled_skill_is_registered(loaded):
    manager, _ = loaded
    skills = manager._plugin_skills
    (qualified,) = [k for k in skills if k.endswith(":elevenlabs-sound-design")]
    # The registered description is what <available_skills> shows: the 60-character rule applies to it.
    assert len(skills[qualified]["description"]) <= 60 and skills[qualified]["description"].endswith(".")
    text = Path(skills[qualified]["path"]).read_text(encoding="utf-8")
    front = yaml.safe_load(text.split("---", 2)[1])
    assert front["name"] == "elevenlabs-sound-design" and len(front["description"]) <= 60
    assert '::elevenlabs-sfx{id="<sound_id>"}' in text and "MEDIA:" in text


def _write_tone(path: Path, seconds: float = 0.5, rate: int = 48000) -> None:
    import math
    import struct
    import wave

    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / rate)))
                                 for i in range(int(seconds * rate))))
