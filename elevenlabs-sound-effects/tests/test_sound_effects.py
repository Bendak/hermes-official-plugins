"""The plugin, installed into a temp HERMES_HOME and enabled there, is loaded by core's real
PluginManager; its tool is dispatched through the real registry against a loopback stand-in for
``POST /v1/sound-generation`` that enforces the request shape of ElevenLabs' published OpenAPI spec.
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
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stand_in():
    server = StandIn()
    yield server
    server.close()


@pytest.fixture
def loaded(_hermes_home, plugin_root, stand_in, monkeypatch):
    """Install + enable the plugin with user settings, load it through core, yield a dispatcher."""
    from hermes_cli import plugins as plugins_mod
    from hermes_cli.plugins import PluginManager
    from tools.registry import registry

    shutil.copytree(plugin_root, _hermes_home / "plugins" / PLUGIN,
                    ignore=shutil.ignore_patterns("tests", "__pycache__", ".pytest_cache"))
    settings = {"output_format": "wav_48000", "sound_model": "eleven_text_to_sound_v3", "api_base_url": stand_in.url}
    (_hermes_home / "config.yaml").write_text(yaml.safe_dump({"plugins": {
        "enabled": [PLUGIN], "entries": {PLUGIN: {"settings": settings}}}}), encoding="utf-8")
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


def test_generates_a_sound_effect_with_the_users_settings(loaded, stand_in, monkeypatch, _hermes_home):
    entry, call = loaded
    assert entry.toolset == "elevenlabs_sound_effects"
    assert entry.check_fn() is False  # no key: the tool stays out of the model's tool list

    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-test-key")
    assert entry.check_fn() is True
    result = call({"prompt": "Glass shattering on a tile floor!", "duration_seconds": 2.5,
                   "loop": "true", "prompt_influence": 0.7})

    (request,) = stand_in.requests
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


def test_bad_input_never_bills_and_api_errors_reach_the_model(loaded, stand_in, monkeypatch, _hermes_home):
    _, call = loaded
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-test-key")

    for args in ({"prompt": "   "}, {"prompt": "thunder", "duration_seconds": 45},
                 {"prompt": "thunder", "prompt_influence": "lots"}):
        assert "error" in call(args)
    assert stand_in.requests == []  # refused before any (billed) request

    stand_in.reply = (401, {"Content-Type": "application/json"},
                      json.dumps({"detail": {"status": "quota_exceeded",
                                             "message": "You have 0 credits remaining."}}).encode())
    error = call({"prompt": "thunder"})["error"]
    assert "HTTP 401" in error and "0 credits remaining" in error
    assert not list((_hermes_home / "cache" / "generated" / "audio").glob("*"))
