"""hermes-elevenlabs-sound-effects — ElevenLabs sound effect generation as a Hermes tool.

Wiring goes only through the public plugin API: ``ctx.register_tool`` for the tool and
``ctx.get_config`` for the user's settings (read on every call, never cached at registration).
"""

from __future__ import annotations

from . import sound_effects as _sfx

TOOLSET = "elevenlabs_sound_effects"


def register(ctx) -> None:
    def setting(key: str):
        return ctx.get_config(key, default=_sfx.DEFAULTS.get(key))

    ctx.register_tool(
        name="elevenlabs_sound_effect", toolset=TOOLSET, schema=_sfx.SCHEMA,
        handler=lambda args, **_: _sfx.generate(args, setting),
        check_fn=_sfx.check_available, requires_env=[_sfx.API_KEY_ENV], emoji="🔊",
    )


__all__ = ["register"]
