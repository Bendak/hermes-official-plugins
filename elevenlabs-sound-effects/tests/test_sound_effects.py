"""The plugin, installed into a temp HERMES_HOME and enabled there, is loaded by core's real
PluginManager; its tool is dispatched through the real registry against loopback stand-ins for
``POST /v1/sound-generation`` that enforce the request shape of ElevenLabs' published OpenAPI spec.
"""

from __future__ import annotations

import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import hermes_yaml as yaml
import pytest

PLUGIN = "elevenlabs-sound-effects"
TOOL = "elevenlabs_sound_effect"
AUDIO = b"RIFF\x24\x00\x00\x00WAVEfmt fake-sound-effect-bytes"
# From https://api.elevenlabs.io/openapi.json: Body_Sound_Generation_v1_sound_generation_post.
BODY_FIELDS = {"text", "loop", "duration_seconds", "prompt_influence", "model_id"}
MODELS = {"eleven_text_to_sound_v2", "eleven_text_to_sound_v3"}


class StandIn:
    """Records each request; answers ``reply`` = (status, headers, body bytes)."""

    def __init__(self):
        self.requests: list[dict] = []
        self.reply = (200, {"Content-Type": "audio/wav", "character-cost": "120"}, AUDIO)
        stand_in = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                url = urlsplit(self.path)
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stand_in.requests.append({"path": url.path, "query": parse_qs(url.query),
                                          "key": self.headers.get("xi-api-key"), "body": body})
                problem = (url.path != "/v1/sound-generation" and "path"
                           or set(body) - BODY_FIELDS and f"unknown fields {sorted(set(body) - BODY_FIELDS)}"
                           or not isinstance(body.get("text"), str) and "text"
                           or body.get("model_id", "eleven_text_to_sound_v2") not in MODELS and "model_id")
                status, headers, payload = (
                    (422, {"Content-Type": "application/json"},
                     json.dumps({"detail": [{"loc": ["body"], "msg": problem}]}).encode())
                    if problem else stand_in.reply)
                self.send_response(status)
                for name, value in {"Content-Length": str(len(payload)), **headers}.items():
                    self.send_header(name, value)
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client hung up on purpose (oversize body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stand_ins():
    servers = [StandIn(), StandIn()]
    yield servers
    for server in servers:
        server.close()


def _write_settings(home: Path, **settings) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {
        "enabled": [PLUGIN], "entries": {PLUGIN: {"settings": settings}}}}), encoding="utf-8")


@pytest.fixture
def loaded(_hermes_home, plugin_root, stand_ins, monkeypatch):
    """Install + enable the plugin with user settings, load it through core, yield a dispatcher."""
    from hermes_cli import plugins as plugins_mod
    from hermes_cli.plugins import PluginManager
    from tools.registry import registry

    shutil.copytree(plugin_root, _hermes_home / "plugins" / PLUGIN,
                    ignore=shutil.ignore_patterns("tests", "__pycache__", ".pytest_cache"))
    _write_settings(_hermes_home, output_format="wav_48000", sound_model="eleven_text_to_sound_v3",
                    api_base_url=stand_ins[0].url)
    empty_bundled = _hermes_home.parent / "bundled"
    empty_bundled.mkdir()
    monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: empty_bundled)
    manager = PluginManager()
    manager.discover_and_load()
    entry = registry.get_entry(TOOL, scope=manager.scope_key)
    assert entry is not None, "the plugin's tool was not registered through real discovery"

    def call(args: dict) -> dict:
        return json.loads(registry.dispatch(TOOL, args, scope=manager.scope_key))

    try:
        yield entry, call
    finally:
        manager.unload()


def test_generates_a_sound_effect_with_the_users_settings(loaded, stand_ins, monkeypatch, _hermes_home):
    entry, call = loaded
    assert entry.toolset == "elevenlabs_sound_effects"
    assert entry.check_fn() is False  # no key: the tool stays out of the model's tool list

    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-test-key")
    assert entry.check_fn() is True
    result = call({"prompt": "Glass shattering on a tile floor!", "duration_seconds": 2.5,
                   "loop": "true", "prompt_influence": 0.7})

    (request,) = stand_ins[0].requests
    assert request["key"] == "xi-test-key"
    assert request["query"] == {"output_format": ["wav_48000"]}
    assert request["body"] == {"text": "Glass shattering on a tile floor!", "loop": True,
                               "duration_seconds": 2.5, "prompt_influence": 0.7,
                               "model_id": "eleven_text_to_sound_v3"}
    path = Path(result["file_path"])
    assert result["success"] is True and result["media_tag"] == f"MEDIA:{path}"
    assert path.parent == _hermes_home / "cache" / "generated" / "audio"
    assert path.name.startswith("sfx_glass-shattering-on-a-tile-floor_") and path.suffix == ".wav"
    assert path.read_bytes() == AUDIO
    assert result["billed_characters"] == "120"


def test_each_profile_gets_its_own_key_settings_and_output_dir(loaded, stand_ins, _hermes_home):
    """One process serving two profiles (the multiplexed gateway): key, settings and the output dir
    are resolved per call from the calling profile, never cached at registration."""
    from agent.secret_scope import (
        reset_multiplex_context,
        reset_secret_scope,
        set_multiplex_context,
        set_secret_scope,
    )
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    _, call = loaded
    other = _hermes_home / "profiles" / "b"
    _write_settings(other, output_format="opus_48000_128", api_base_url=stand_ins[1].url)
    results = []
    for home, key in ((_hermes_home, "key-a"), (other, "key-b"), (_hermes_home, "key-a")):
        tokens = (set_multiplex_context(True), set_hermes_home_override(home),
                  set_secret_scope({"ELEVENLABS_API_KEY": key}, profile_home=str(home)))
        try:
            results.append((home, call({"prompt": "door creak"})))
        finally:
            reset_secret_scope(tokens[2])
            reset_hermes_home_override(tokens[1])
            reset_multiplex_context(tokens[0])

    assert [r["key"] for r in stand_ins[0].requests] == ["key-a", "key-a"]
    assert [r["key"] for r in stand_ins[1].requests] == ["key-b"]
    assert stand_ins[1].requests[0]["query"] == {"output_format": ["opus_48000_128"]}
    assert stand_ins[1].requests[0]["body"]["model_id"] == "eleven_text_to_sound_v2"  # B's default
    for home, result in results:
        path = Path(result["file_path"])
        assert path.parent == home / "cache" / "generated" / "audio"
        assert path.suffix == (".opus" if home == other else ".wav")


def test_bad_input_never_bills_and_errors_reach_the_model(loaded, stand_ins, monkeypatch, _hermes_home):
    _, call = loaded
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-test-key")

    for args in ({"prompt": "   "}, {"prompt": "thunder", "duration_seconds": 45},
                 {"prompt": "thunder", "duration_seconds": True},
                 {"prompt": "thunder", "prompt_influence": "lots"}, {"prompt": "thunder", "loop": {"x": 1}}):
        assert "error" in call(args), args
    _write_settings(_hermes_home, output_format="pcm_16000", api_base_url=stand_ins[0].url)
    assert "output_format" in call({"prompt": "thunder"})["error"]
    assert stand_ins[0].requests == []  # every refusal happened before a (billed) request

    _write_settings(_hermes_home, api_base_url=stand_ins[0].url)
    stand_ins[0].reply = (401, {"Content-Type": "application/json"},
                          json.dumps({"detail": {"status": "quota_exceeded",
                                                 "message": "You have 0 credits remaining."}}).encode())
    error = call({"prompt": "thunder"})["error"]
    assert "HTTP 401" in error and "0 credits remaining" in error

    stand_ins[0].reply = (200, {"Content-Type": "audio/mpeg", "Content-Length": str(60 * 1024 * 1024)}, b"x")
    assert "byte cap" in call({"prompt": "thunder"})["error"]
    assert not list((_hermes_home / "cache" / "generated" / "audio").glob("*"))
