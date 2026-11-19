"""Promptli Comms for Hermes Agent: its own email address and phone number.

``register(ctx)`` wires, through the public plugin API only:

* the ``promptli`` gateway platform (email, SMS and calls over Agent Link, one session per person),
* a ``pre_tool_call`` hook (TRAI calling hours, AI disclosure on calls, no outbound sends for strangers),
* the ``promptli-comms`` skill,
* ``hermes promptli setup | status | doctor | mcp``.

The Promptli tools themselves come from the hosted MCP server (``hermes promptli mcp``), signed in with the
browser through Hermes's own MCP OAuth. Heavy imports (the SDK, the adapter) happen only when used, so every
``hermes`` invocation stays fast.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

__all__ = ["register"]

__version__ = "0.1.1"


def _adapter() -> Any:
    from . import adapter

    return adapter


def _make_adapter(config: Any) -> Any:
    return _adapter().PromptliAdapter(config)


def _check_requirements() -> bool:
    try:
        import promptli  # noqa: F401
        import promptli_gateway  # noqa: F401
    except ImportError:
        return False
    return _adapter().check_requirements()


def _validate_config(config: Any) -> bool:
    return _adapter().validate_config(config)


def _env_enablement() -> dict[str, Any] | None:
    return _adapter().env_enablement()


async def _standalone_send(pconfig: Any, chat_id: str, message: str, **kwargs: Any) -> dict[str, Any]:
    return await _adapter().standalone_send(pconfig, chat_id, message, **kwargs)


def _parse_target_ref(raw: str) -> Any:
    return _adapter().parse_target_ref(raw)


def _validate_target_ref(raw: str) -> Any:
    return _adapter().validate_target_ref(raw)


def _interactive_setup() -> None:
    from .cli import interactive_setup

    interactive_setup()


def _register_platform(ctx: Any) -> None:
    from gateway.platform_registry import PlatformEntry

    from .prompts import PLATFORM_HINT
    from .settings import ALLOW_ALL_ENV, ALLOWED_USERS_ENV, API_KEY_ENV, HOME_CHANNEL_ENV, PLATFORM

    kwargs: dict[str, Any] = dict(
        name=PLATFORM,
        label="Promptli Comms",
        adapter_factory=_make_adapter,
        check_fn=_check_requirements,
        validate_config=_validate_config,
        required_env=[API_KEY_ENV],
        install_hint="Run `hermes plugins enable promptli-comms` so promptli and promptli-gateway install, "
        "then `hermes promptli setup`.",
        setup_fn=_interactive_setup,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var=HOME_CHANNEL_ENV,
        standalone_sender_fn=_standalone_send,
        parse_target_ref_fn=_parse_target_ref,
        validate_target_ref_fn=_validate_target_ref,
        allowed_users_env=ALLOWED_USERS_ENV,
        allow_all_env=ALLOW_ALL_ENV,
        max_message_length=0,  # the adapter splits texts itself; an email is one message
        pii_safe=True,
        emoji="📨",
        allow_update_command=False,  # never let an email or text run /update
        platform_hint=PLATFORM_HINT,
    )
    known = {f.name for f in dataclasses.fields(PlatformEntry)}
    for key, value in (
        ("allowlist_suppresses_pairing", True),  # never email or text a pairing code to a stranger
        ("display_tier", "minimal"),  # no tool-progress messages: each would be an email or a billed text
        ("dashboard_description", "Email, SMS and phone calls for your agent (Promptli Comms)."),
        ("docs_url", "https://github.com/promptli-dev/promptli-hermes-plugin#readme"),
        ("dashboard_env_vars", (API_KEY_ENV, ALLOWED_USERS_ENV, HOME_CHANNEL_ENV)),
    ):
        if key in known:  # older cores reject unknown register_platform kwargs
            kwargs[key] = value
    ctx.register_platform(**kwargs)


def _register_cli(ctx: Any) -> None:
    from .cli import handle, setup_argparse

    ctx.register_cli_command(
        name="promptli",
        help="Promptli Comms: email, SMS and calls for Hermes (setup, status, doctor, mcp)",
        setup_fn=setup_argparse,
        handler_fn=handle,
    )


def register(ctx: Any) -> None:
    from . import guard

    ctx.register_hook("pre_tool_call", guard.pre_tool_call)
    ctx.register_skill("promptli-comms", Path(__file__).parent / "skills" / "promptli-comms" / "SKILL.md")
    _register_cli(ctx)
    _register_platform(ctx)
