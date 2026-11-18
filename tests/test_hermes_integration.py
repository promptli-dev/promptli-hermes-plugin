"""Against a real Hermes checkout: the adapter inside Hermes's own BasePlatformAdapter pipeline.

    HERMES_AGENT_SRC=/path/to/hermes-agent /path/to/hermes/python -m pytest -m hermes

Promptli is faked (no network, nothing is sent); Hermes's handle_message -> handler -> send ->
on_processing_complete path is the real one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from conftest import PACKAGE
from fakes import FakeClient, email_event, text_event

pytestmark = pytest.mark.hermes


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PROMPTLI_API_KEY", "psk_test_fake")
    monkeypatch.setenv("PROMPTLI_ALLOWED_USERS", "jane@example.com,+15550109999")


async def wait_for(pred: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


async def test_real_pipeline_email_and_text(env: None, tmp_path: Path) -> None:
    from gateway.config import PlatformConfig
    from gateway.platform_registry import PlatformEntry, platform_registry

    class Ctx:  # what Hermes's plugin loader does for an enabled plugin: register() first
        def register_platform(self, **kw: Any) -> None:
            platform_registry.register(PlatformEntry(**kw))

        def register_hook(self, *_: Any) -> None: ...
        def register_skill(self, *_: Any) -> None: ...
        def register_cli_command(self, **_: Any) -> None: ...

    __import__(PACKAGE).register(Ctx())
    adapter_mod = __import__(f"{PACKAGE}.adapter", fromlist=["PromptliAdapter"])
    fake = FakeClient()
    ad = adapter_mod.PromptliAdapter(
        PlatformConfig(enabled=True, extra={}), client_factory=lambda _s: fake, state_path=tmp_path / "s.db"
    )
    seen: list[Any] = []

    async def handler(event: Any) -> str:
        seen.append(event)
        return "NO_REPLY" if "STOP-REPLY" in event.text else f"Got it ({len(seen)})"

    ad.set_message_handler(handler)
    assert await ad.connect()
    await ad.on_link_event(email_event(1))
    await wait_for(lambda: len(fake.sent) == 1)
    assert fake.sent[0].kind == "reply" and fake.sent[0].args["message_id"] == "msg_1"
    assert seen[0].source.chat_id == "ctc_jane" and seen[0].source.user_id == "jane@example.com"
    await wait_for(lambda: bool(fake.link and fake.link.acks and fake.link.acks[-1] == 1))

    await ad.on_link_event(text_event(2, body="STOP-REPLY", sender="+15550109999", contact_key="ctc_owner"))
    await wait_for(lambda: len(seen) == 2)
    await wait_for(lambda: bool(fake.link and fake.link.acks[-1] == 2))
    assert len(fake.sent) == 1  # NO_REPLY never leaves
    assert ad.toolsets_for_source(seen[1].source) == ["web", "todo", "no_mcp"]
    await ad.disconnect()


async def test_registry_entry_loads_in_real_hermes(env: None) -> None:
    from gateway.platform_registry import PlatformEntry

    entries: list[PlatformEntry] = []

    class Ctx:
        def register_platform(self, **kw: Any) -> None:
            entries.append(PlatformEntry(**kw))  # real dataclass: unknown kwargs would fail here

        def register_hook(self, *_: Any) -> None: ...
        def register_skill(self, *_: Any) -> None: ...
        def register_cli_command(self, **_: Any) -> None: ...

    __import__(PACKAGE).register(Ctx())
    assert entries[0].name == "promptli" and entries[0].check_fn() is True
