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
import time
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
        self.audio = AUDIO
        stand_in = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                stand_in.bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", str(len(stand_in.audio)))
                self.send_header("character-cost", "20")
                self.end_headers()
                self.wfile.write(stand_in.audio)

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
    assert result["media_tag"] == f"MEDIA:{result['file_path']}"
    assert Path(result["file_path"]).stem.endswith(sound_id)

    _, client = api
    listing = client.get("/sounds").json()
    (row,) = listing["sounds"]
    assert row == {**row, "id": sound_id, "prompt": "Retro coin pickup", "duration_seconds": 2.0,
                   "loop": False, "prompt_influence": 0.6, "model": "eleven_text_to_sound_v3",
                   "output_format": "mp3_44100_128", "billed_characters": "20",
                   "session_id": "20261023_120000_abcdef", "kind": "mp3", "exists": True, "length_seconds": None,
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

    hard = audio_dir / "sfx_hard_20261023_120000_dddddddddddd.mp3"
    try:
        os.link(secret, hard)  # same user, same filesystem: no symlink to resolve
    except (OSError, NotImplementedError):
        hard = None
    prov.record({"id": "dddddddddddd", "prompt": "hard link", "output_format": "mp3_44100_128",
                 "file_name": hard.name if hard else "sfx_gone_20261023_120000_dddddddddddd.mp3"})

    assert client.get("/audio?id=aaaaaaaaaaaa").status_code == 410
    assert client.get("/audio?id=bbbbbbbbbbbb").status_code == 410
    assert client.get("/audio?id=dddddddddddd").status_code == 410
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
    listing = client.get("/sounds").json()
    assert listing["readable"] == 29 and listing["total"] == 30  # the count covers records past the window


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

# How a generated sound reaches the user. The card line (``desktop_card``) is offered only after a Desktop
# with this plugin's Desktop half on told the backend it saw THIS call start in THIS chat (``POST
# /card-ready`` with the event's session id, from its ``tool.start`` listener); otherwise only the
# ``MEDIA:`` line, which Desktop's built-in player and every messaging surface play, so no surface ever
# gets a card line it would show as text. Every doubt (unknown surface, no session id, plugin host, two
# unnamed sightings) answers MEDIA. The length reported is the length of the audio ElevenLabs returned.


@pytest.fixture
def desktop(loaded):
    """``call`` as a Desktop chat turn: the session context a Desktop turn binds (source, durable id, UI tab
    id) and the durable id the dispatcher passes the handler."""
    from gateway.session_context import clear_session_vars, set_session_vars

    _, call = loaded

    def turn(args: dict, session: str = "desk-1", ui: str = "", source: str = "desktop", platform: str = "") -> dict:
        tokens = set_session_vars(platform=platform, source=source, session_id=session, ui_session_id=ui)
        try:
            return call(args, session_id=session)
        finally:
            clear_session_vars(tokens)

    return turn


def test_the_card_line_is_for_the_call_the_desktop_saw_in_that_chat(desktop, api):
    module, client = api
    prompt = "  Rain on a tin roof 🌧️\nthen distant thunder"  # exact bytes, surrounding spaces included
    see = lambda session="desk-1": client.post("/card-ready", json={"prompt": prompt, "session": session})

    plain = desktop({"prompt": prompt, "duration_seconds": 2})
    assert plain["success"] is True and "desktop_card" not in plain  # Desktop half off / no Desktop
    assert plain["media_tag"] == f"MEDIA:{plain['file_path']}"

    assert see().json() == {"ok": True}
    carded = desktop({"prompt": prompt, "duration_seconds": 2})
    assert carded["desktop_card"] == f'::elevenlabs-sfx{{id="{carded["sound_id"]}"}}'
    assert carded["media_tag"] == f"MEDIA:{carded['file_path']}"  # still there for the other surfaces
    assert "desktop_card" not in desktop({"prompt": prompt, "duration_seconds": 2})  # that sighting is used up

    see()
    assert "desktop_card" not in desktop({"prompt": "Rain on a tin roof", "duration_seconds": 2})  # other prompt
    # Another chat repeating the prompt (a CLI or TUI session, a second Desktop chat) is not the call seen.
    assert "desktop_card" not in desktop({"prompt": prompt}, session="cli-session-B", source="cli")
    assert "desktop_card" not in desktop({"prompt": prompt}, session="desk-2")
    assert "desktop_card" in desktop({"prompt": prompt})  # the chat it was seen in still gets it

    see()
    seen = module._prov.data_dir() / module._prov.SEEN_FILE_NAME
    stale = json.loads(seen.read_text().splitlines()[-1])
    seen.write_text(json.dumps({**stale, "at": time.time() - 200}) + "\n")
    assert "desktop_card" not in desktop({"prompt": prompt, "duration_seconds": 2})  # an earlier call's sighting

    see()
    assert client.delete("/card-ready").json() == {"ok": True}  # the Desktop half was switched off
    assert "desktop_card" not in desktop({"prompt": prompt, "duration_seconds": 2})
    assert client.delete("/card-ready").json() == {"ok": True}  # nothing to forget is fine


def test_a_sighting_names_the_chat_by_its_ui_tab_id_on_older_hosts(desktop, api):
    """0.22.x Desktop events carry the UI tab id, not the durable session id the handler gets; the
    turn's session context has both."""
    _, client = api
    client.post("/card-ready", json={"prompt": "door knock", "session": "tab-7f3a"})
    assert "desktop_card" not in desktop({"prompt": "door knock"}, session="20261023_120000_abcdef", ui="tab-0000")
    assert "desktop_card" in desktop({"prompt": "door knock"}, session="20261023_120000_abcdef", ui="tab-7f3a")


def test_an_unnamed_sighting_counts_only_when_it_is_the_only_one(desktop, api):
    """A Desktop half from before sightings named the chat posts only the prompt: one fresh sighting
    vouches for one call; two are ambiguous (two chats ran the prompt) and vouch for neither."""
    _, client = api
    client.post("/card-ready", json={"prompt": "glass tap"})
    assert "desktop_card" in desktop({"prompt": "glass tap"})
    assert "desktop_card" not in desktop({"prompt": "glass tap"})  # used up
    client.post("/card-ready", json={"prompt": "glass tap", "owner": "a"})
    client.post("/card-ready", json={"prompt": "glass tap", "owner": "b"})
    assert "desktop_card" not in desktop({"prompt": "glass tap"})
    assert "desktop_card" not in desktop({"prompt": "glass tap"})


def test_an_unknown_or_messaging_surface_never_gets_the_card_line(desktop, loaded, api):
    """A Desktop open on the same profile hears a Telegram turn's tool events too and vouches for a
    card; the Telegram user must still get the MEDIA: line only. Same when the turn's surface or
    session cannot be told at all."""
    _, call = loaded
    _, client = api
    see = lambda session="desk-1": client.post("/card-ready", json={"prompt": "door knock", "session": session})
    see()
    result = desktop({"prompt": "door knock"}, source="", platform="telegram")
    assert "desktop_card" not in result and result["media_tag"].startswith("MEDIA:")
    assert "desktop_card" not in desktop({"prompt": "door knock"}, source="")  # no surface known
    assert "desktop_card" not in call({"prompt": "door knock"}, session_id="desk-1")  # no session context at all
    assert "desktop_card" not in desktop({"prompt": "door knock"}, session="")  # no session id
    assert "desktop_card" in desktop({"prompt": "door knock"}, source="", platform="local")  # the Desktop turn


def test_inside_the_plugin_host_the_card_line_is_never_offered(desktop, api, monkeypatch):
    """Under ``plugins.isolation: host`` the handler runs in the plugin host process, which gets none of
    the caller's session context: a Telegram turn would read as a non-messaging one there."""
    _, client = api
    client.post("/card-ready", json={"prompt": "door knock", "session": "desk-1"})
    monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", "1")
    result = desktop({"prompt": "door knock"})
    assert "desktop_card" not in result and result["media_tag"].startswith("MEDIA:")
    monkeypatch.delenv("HERMES_PLUGIN_HOST_PROCESS")
    assert "desktop_card" in desktop({"prompt": "door knock"})  # the same sighting, in process


def test_switching_one_desktop_off_keeps_the_other_desktops_cards(desktop, api):
    _, client = api
    client.post("/card-ready", json={"prompt": "glass tap", "owner": "window-a", "session": "desk-1"})
    client.post("/card-ready", json={"prompt": "door knock", "owner": "window-b", "session": "desk-1"})
    assert client.delete("/card-ready?owner=window-a").json() == {"ok": True}
    assert "desktop_card" not in desktop({"prompt": "glass tap"})
    assert "desktop_card" in desktop({"prompt": "door knock"})


def test_the_card_handshake_stays_small_and_refuses_junk(desktop, api):
    module, client = api
    assert client.post("/card-ready", json={"prompt": "x" * 5000}).status_code == 422
    assert client.post("/card-ready", json={"prompt": "x", "session": "s" * 300}).status_code == 422
    assert client.post("/card-ready", json={}).status_code == 422
    for n in range(1200):
        client.post("/card-ready", json={"prompt": f"sound {n}", "session": "desk-1"})
    seen = module._prov.data_dir() / module._prov.SEEN_FILE_NAME
    assert seen.stat().st_size <= 64 * 1024
    assert "desktop_card" in desktop({"prompt": "sound 1199"})  # the newest sightings survive the trim
    assert "desktop_card" not in desktop({"prompt": "sound 3"})


def test_the_reported_length_is_the_returned_audio_not_the_request(loaded, api, stand_in, audio):
    _, call = loaded
    _, client = api
    stand_in.audio = audio.mp3(62)  # a 1 s loop request answered with ~1.6 s of MP3, as ElevenLabs does
    result = call({"prompt": "soft hum loop", "duration_seconds": 1, "loop": True})
    length = round((62 * 1152 - 2151) / 44100, 3)
    assert result["length_seconds"] == length and result["requested_duration_seconds"] == 1.0
    assert f"{length:g} s for the 1 s requested" in result["length_note"]
    (row,) = client.get("/sounds").json()["sounds"]
    assert (row["length_seconds"], row["duration_seconds"]) == (length, 1.0)  # duration = what was billed

    stand_in.audio = audio.mp3(78, padding=1080)  # 78 x 1152 - 1656 = 88200 samples = 2.0 s
    exact = call({"prompt": "door knock", "duration_seconds": 2})
    assert exact["length_seconds"] == 2.0 and "length_note" not in exact
    auto = call({"prompt": "door knock"})
    assert auto["length_seconds"] == 2.0 and auto["requested_duration_seconds"] is None


def test_sightings_writers_and_the_tool_wait_for_each_other(loaded, api):
    """The plugin host serves routes on worker threads (and several processes can serve a profile): an
    append landing between a trim's read and its replace would be lost, so every write takes the lock.
    The tool's read-and-consume takes it too (on Windows a replace fails while a reader has the file)."""
    import threading

    module, _ = api
    prov = module._prov
    prov.mark_card_ready("first", "w", "s")
    seen = prov.data_dir() / prov.SEEN_FILE_NAME
    before = seen.read_bytes()
    writer = threading.Thread(target=prov.mark_card_ready, args=("second", "w", "s"))
    answers = []
    reader = threading.Thread(target=lambda: answers.append(prov.card_ready("first", time.time() - 60, {"s"})))
    with prov._seen_lock():
        writer.start()
        reader.start()
        writer.join(0.3)
        assert writer.is_alive() and seen.read_bytes() == before  # held off while another writer works
        assert reader.is_alive() and answers == []  # the tool waits as well
    writer.join(5)
    reader.join(5)
    assert answers == [True] and prov.card_ready("second", time.time() - 60, {"s"})


def test_many_concurrent_sightings_stay_bounded_and_whole(loaded, api):
    from concurrent.futures import ThreadPoolExecutor

    module, _ = api
    prov = module._prov
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda n: prov.mark_card_ready(f"sound {n}", "w", "s"), range(1600)))  # past the 64 KB trim
    seen = prov.data_dir() / prov.SEEN_FILE_NAME
    assert seen.stat().st_size <= 64 * 1024
    assert all(json.loads(line)["key"] for line in seen.read_text().splitlines())  # no torn lines
    assert prov.card_ready("sound 1599", time.time() - 60, {"s"})


def test_marks_clears_and_reads_running_together_never_fail(loaded, api):
    """Sightings, switch-offs and the tool's consume from many threads at once: no error, no leftover temp
    file, every remaining line whole (on Windows this is the replace-while-open case)."""
    from concurrent.futures import ThreadPoolExecutor

    module, _ = api
    prov = module._prov

    def work(n: int) -> None:
        prov.mark_card_ready(f"sound {n % 20}", f"w{n % 3}", f"s{n % 4}")
        if n % 7 == 0:
            prov.clear_card_marks(f"w{n % 3}")
        prov.card_ready(f"sound {n % 20}", time.time() - 60, {f"s{n % 4}"})

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(work, range(600)))
    directory = prov.data_dir()
    assert not list(directory.glob("*.tmp"))
    assert all(json.loads(line)["key"] for line in (directory / prov.SEEN_FILE_NAME).read_text().splitlines())


def test_a_failed_rewrite_leaves_no_temp_file_behind(loaded, api, monkeypatch):
    """Disk full (or a refused replace) mid-rewrite: the error reaches the caller, the half-written copy
    does not stay in the data dir, and the tool answers MEDIA."""
    import errno

    module, client = api
    prov = module._prov
    prov.mark_card_ready("door knock", "w", "s")

    def full(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(prov.os, "replace", full)
    assert client.delete("/card-ready?owner=w").status_code == 500
    assert prov.card_ready("door knock", time.time() - 60, {"s"}) is False
    assert not list(prov.data_dir().glob("*.tmp"))
