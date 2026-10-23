"""The ``elevenlabs_sound_effect`` tool: text → audio file through ElevenLabs' Sound Effects API.

``POST {api_base_url}/v1/sound-generation?output_format=<fmt>`` with ``{text, duration_seconds?,
prompt_influence?, loop, model_id}`` and the ``xi-api-key`` header returns the audio bytes
(https://elevenlabs.io/docs/api-reference/text-to-sound-effects/convert). The file lands in
``$HERMES_HOME/cache/generated/audio/``, which the gateway may deliver from, and the result carries a
``MEDIA:<path>`` line the model puts in its reply to send it.

Model, format and endpoint are user settings (``plugins.entries.<id>.settings``), read per call so a
multiplexed gateway serves each profile its own; the key comes from the profile's secret scope.
"""

from __future__ import annotations

import datetime
import re
import uuid
from collections.abc import Callable
from typing import Any

from agent.secret_scope import get_secret
from tools.registry import tool_error, tool_result

API_KEY_ENV = "ELEVENLABS_API_KEY"
DEFAULTS = {
    "sound_model": "eleven_text_to_sound_v2",
    "output_format": "mp3_44100_128",
    "api_base_url": "https://api.elevenlabs.io",
}
MIN_SECONDS, MAX_SECONDS = 0.5, 30.0
_TIMEOUT_SECONDS = 180.0
_MAX_AUDIO_BYTES = 50 * 1024 * 1024
# Codec prefix of ``output_format`` → file extension (every one is on the gateway's media list).
_EXTENSIONS = {"mp3": "mp3", "opus": "opus", "wav": "wav"}

SCHEMA = {
    "name": "elevenlabs_sound_effect",
    "description": (
        "Generate a sound effect from a text description with ElevenLabs (up to 30 seconds): foley, "
        "impacts, ambience, UI sounds, creature noises, short musical stingers or drum loops. "
        "Describe the sound concretely (source, material, space, intensity), e.g. 'heavy wooden door "
        "creaking open in a stone hall'. Returns the saved file path and a MEDIA: line; include that "
        "MEDIA: line in your reply to send the audio to the user. Each call is billed to the user's "
        "ElevenLabs account."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "What the sound effect should sound like."},
            "duration_seconds": {
                "type": "number", "minimum": MIN_SECONDS, "maximum": MAX_SECONDS,
                "description": "Length in seconds (0.5-30). Omit to let ElevenLabs pick from the prompt.",
            },
            "loop": {
                "type": "boolean",
                "description": "Make the sound loop seamlessly (ambience, music beds). Default false.",
            },
            "prompt_influence": {
                "type": "number", "minimum": 0, "maximum": 1,
                "description": "0-1: higher follows the prompt more literally, lower varies more. Default 0.3.",
            },
        },
        "required": ["prompt"],
    },
}


def check_available() -> bool:
    return bool((get_secret(API_KEY_ENV, "") or "").strip())


def _number(args: dict, key: str, low: float, high: float) -> float | None:
    raw = args.get(key)
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number between {low:g} and {high:g}") from None
    if not low <= value <= high:
        raise ValueError(f"{key} must be between {low:g} and {high:g} (got {value:g})")
    return value


def _flag(raw: Any) -> bool:
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


def build_request(args: dict, model: str) -> dict:
    """The JSON body for one call; raises ``ValueError`` with a model-readable message."""
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required: describe the sound effect to generate")
    body: dict[str, Any] = {"text": prompt, "loop": _flag(args.get("loop")), "model_id": model}
    duration = _number(args, "duration_seconds", MIN_SECONDS, MAX_SECONDS)
    if duration is not None:
        body["duration_seconds"] = duration
    influence = _number(args, "prompt_influence", 0.0, 1.0)
    if influence is not None:
        body["prompt_influence"] = influence
    return body


def _api_error(response) -> str:
    """ElevenLabs errors come as ``{"detail": {"status", "message"}}`` or FastAPI's
    ``{"detail": [{"loc", "msg"}]}``; fall back to the raw body."""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict):
        message = detail.get("message") or detail.get("status") or str(detail)
    elif isinstance(detail, list):
        message = "; ".join(
            f"{'.'.join(str(p) for p in item.get('loc', [])[1:]) or 'request'}: {item.get('msg')}"
            for item in detail if isinstance(item, dict)) or str(detail)
    else:
        message = (response.text or "").strip()[:500] or response.reason_phrase
    hint = {401: " (check ELEVENLABS_API_KEY and your plan's remaining credits)",
            429: " (rate limited or out of credits; try again later)"}.get(response.status_code, "")
    return f"ElevenLabs returned HTTP {response.status_code}: {message}{hint}"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "sound"


def _save(audio: bytes, prompt: str, output_format: str) -> str:
    """Write under ``cache/generated/audio``: outside the transient caches the gateway sweeps, and
    inside the roots it delivers ``MEDIA:`` files from. Resolved per call (multiplexed profiles)."""
    from hermes_constants import get_hermes_home

    extension = _EXTENSIONS.get(output_format.split("_", 1)[0], "mp3")
    directory = get_hermes_home() / "cache" / "generated" / "audio"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = directory / f"sfx_{_slug(prompt)}_{stamp}_{uuid.uuid4().hex[:8]}.{extension}"
    path.write_bytes(audio)
    return str(path)


def generate(args: dict, settings: Callable[[str], Any]) -> str:
    import httpx

    api_key = (get_secret(API_KEY_ENV, "") or "").strip()
    if not api_key:
        return tool_error(f"{API_KEY_ENV} is not set. Add your ElevenLabs API key with "
                          f"`hermes config set {API_KEY_ENV} <key>` or in Settings → Plugins.")
    model = settings("sound_model") or DEFAULTS["sound_model"]
    output_format = settings("output_format") or DEFAULTS["output_format"]
    base_url = str(settings("api_base_url") or DEFAULTS["api_base_url"]).rstrip("/")
    try:
        body = build_request(args, model)
    except ValueError as exc:
        return tool_error(str(exc))
    try:
        response = httpx.post(
            f"{base_url}/v1/sound-generation", params={"output_format": output_format}, json=body,
            headers={"xi-api-key": api_key, "accept": "audio/*"}, timeout=_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        return tool_error(f"Could not reach ElevenLabs at {base_url}: {type(exc).__name__}: {exc}")
    if response.status_code != 200:
        return tool_error(_api_error(response))
    audio = response.content
    if not audio:
        return tool_error("ElevenLabs returned an empty audio response")
    if len(audio) > _MAX_AUDIO_BYTES:
        return tool_error(f"ElevenLabs returned {len(audio)} bytes, over the {_MAX_AUDIO_BYTES} byte cap")
    path = _save(audio, body["text"], output_format)
    result = {
        "success": True, "file_path": path, "media_tag": f"MEDIA:{path}",
        "model": model, "output_format": output_format, "loop": body["loop"],
        "duration_seconds": body.get("duration_seconds"), "bytes": len(audio),
    }
    if cost := response.headers.get("character-cost"):
        result["billed_characters"] = cost
    return tool_result(result)
