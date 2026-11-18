"""Test setup: load the plugin directory as a package, with Hermes stand-ins or a real Hermes checkout.

- Default (unit tests): small stand-ins for the public Hermes modules the plugin imports, with the same names
  and signatures, so the suite needs only the real `promptli` / `promptli-gateway` packages.
- `HERMES_AGENT_SRC=/path/to/hermes-agent pytest -m hermes`: the real Hermes modules (integration tests).
"""

from __future__ import annotations

import asyncio
import enum
import importlib.util
import os
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "promptli_hermes_plugin"
REAL_HERMES = bool(os.environ.get("HERMES_AGENT_SRC"))
SECRETS: dict[str, str] = {}
SESSION_ENV: dict[str, str] = {}
SAVED_ENV: dict[str, str] = {}
CONFIG: dict[str, Any] = {}


def _module(name: str, **members: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(members)
    sys.modules[name] = module
    return module


# ---- stand-ins for the public Hermes plugin API -------------------------------------------------------
@dataclass
class SendResult:
    success: bool
    message_id: str | None = None
    error: str | None = None
    raw_response: Any = None
    retryable: bool = False
    retry_after: float | None = None
    continuation_message_ids: tuple = ()


@dataclass
class PlatformConfig:
    enabled: bool = True
    extra: dict = field(default_factory=dict)


class Platform(str):
    @property
    def value(self) -> str:
        return str(self)


@dataclass
class SessionSource:
    platform: Any
    chat_id: str
    chat_name: str | None = None
    chat_type: str = "dm"
    user_id: str | None = None
    user_name: str | None = None
    message_id: str | None = None


class MessageType(enum.Enum):
    TEXT = "text"


class ProcessingOutcome(enum.Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


@dataclass
class MessageEvent:
    text: str
    message_type: MessageType = MessageType.TEXT
    user_id: str | None = None
    user_name: str | None = None
    source: Any = None
    raw_message: Any = None
    message_id: str | None = None
    metadata: dict = field(default_factory=dict)
    allow_gateway_control: bool = True
    reply_expected: bool | None = None


class BasePlatformAdapter:
    """The slice of gateway.platforms.base.BasePlatformAdapter the plugin uses. handle_message runs the
    installed handler and sends its reply with reply_to = the event's message_id, then reports completion,
    like the real pipeline (handler -> _send_final_text -> on_processing_complete)."""

    def __init__(self, config: PlatformConfig, platform: Any) -> None:
        self.config = config
        self.platform = platform
        self._running = False
        self.connected = False
        self.fatal: tuple[str, str, bool] | None = None
        self.handled: list[MessageEvent] = []
        self.handler: Any = None
        self._background_tasks: set[asyncio.Task] = set()

    def build_source(
        self,
        chat_id: str,
        chat_name: str | None = None,
        chat_type: str = "dm",
        user_id: str | None = None,
        user_name: str | None = None,
        message_id: str | None = None,
        **_: Any,
    ) -> SessionSource:
        return SessionSource(self.platform, chat_id, chat_name, chat_type, user_id, user_name, message_id)

    async def handle_message(self, event: MessageEvent) -> None:
        self.handled.append(event)
        if self.handler is None:
            return

        async def run() -> None:
            reply = await self.handler(event)
            if reply:
                await self.send(event.source.chat_id, reply, reply_to=event.message_id)
            await self.on_processing_complete(event, ProcessingOutcome.SUCCESS)

        task = asyncio.ensure_future(run())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        return None

    def _mark_connected(self, **_: Any) -> None:
        self.connected = True

    def _mark_disconnected(self) -> None:
        self.connected = False

    def _set_fatal_error(self, code: str, message: str, *, retryable: bool) -> None:
        self.fatal = (code, message, retryable)

    def _wire_plugin_handlers(self, native: Any = None) -> None:
        return None


@dataclass
class PlatformEntry:
    name: str
    label: str
    adapter_factory: Any
    check_fn: Any
    validate_config: Any = None
    required_env: list = field(default_factory=list)
    install_hint: str = ""
    setup_fn: Any = None
    allowed_users_env: str = ""
    allow_all_env: str = ""
    max_message_length: int = 0
    pii_safe: bool = False
    emoji: str = ""
    allow_update_command: bool = True
    platform_hint: str = ""
    env_enablement_fn: Any = None
    cron_deliver_env_var: str = ""
    parse_target_ref_fn: Any = None
    validate_target_ref_fn: Any = None
    standalone_sender_fn: Any = None
    display_tier: str = ""
    allowlist_suppresses_pairing: bool = False
    dashboard_description: str = ""
    docs_url: str = ""
    dashboard_env_vars: tuple = ()


def get_scoped_secret(name: str, default: Any = None, **_: Any) -> Any:
    return SECRETS.get(name, default)


def seed_extra_from_env(spec: Any, *, home_env: str | None = None) -> dict:
    out: dict[str, Any] = {}
    for env, key, conv in spec:
        value = SECRETS.get(env, "").strip()
        if value:
            out[key] = conv(value) if conv else value
    if home_env and SECRETS.get(home_env):
        out["home_channel"] = {"chat_id": SECRETS[home_env], "name": "Home"}
    return out


def get_session_env(name: str, default: str = "") -> str:
    return SESSION_ENV.get(name, default)


def plugin_data_dir(name: str) -> Path:  # pragma: no cover - tests pass state_path explicitly
    raise RuntimeError("tests pass state_path")


def save_env_value(key: str, value: str) -> None:
    SAVED_ENV[key] = value


def load_config() -> dict:
    return {k: (dict(v) if isinstance(v, dict) else v) for k, v in CONFIG.items()}


def save_config(config: dict, *, merge_existing: bool = False, **_: Any) -> None:
    CONFIG.update(config)


def _install_stubs() -> None:
    _module("gateway")
    _module("gateway.config", Platform=Platform, PlatformConfig=PlatformConfig)
    _module("gateway.platforms")
    _module("gateway.platforms.base", BasePlatformAdapter=BasePlatformAdapter, SendResult=SendResult)
    _module(
        "gateway.platforms.event",
        MessageEvent=MessageEvent,
        MessageType=MessageType,
        ProcessingOutcome=ProcessingOutcome,
    )
    _module("gateway.platforms._shared", get_scoped_secret=get_scoped_secret, seed_extra_from_env=seed_extra_from_env)
    _module("gateway.platform_registry", PlatformEntry=PlatformEntry)
    _module("gateway.session_context", get_session_env=get_session_env)
    _module("plugins")
    _module("plugins.plugin_storage", plugin_data_dir=plugin_data_dir)
    _module("hermes_cli")
    _module("hermes_cli.config", save_env_value=save_env_value, load_config=load_config, save_config=save_config)


def _load_plugin() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(PACKAGE, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


if REAL_HERMES:
    sys.path.insert(0, os.environ["HERMES_AGENT_SRC"])
else:
    _install_stubs()
PLUGIN = _load_plugin()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        is_hermes = item.get_closest_marker("hermes") is not None
        if is_hermes and not REAL_HERMES:
            item.add_marker(pytest.mark.skip(reason="set HERMES_AGENT_SRC to a Hermes checkout"))
        if not is_hermes and REAL_HERMES:
            item.add_marker(pytest.mark.skip(reason="unit tests run against the Hermes stand-ins"))


@pytest.fixture(autouse=True)
def _clean_env() -> Any:
    SECRETS.clear()
    SESSION_ENV.clear()
    SAVED_ENV.clear()
    CONFIG.clear()
    yield
