from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import CONFIG, PACKAGE, PLUGIN, ROOT, SAVED_ENV, SECRETS, SESSION_ENV
from fakes import FakeClient

guard = __import__(f"{PACKAGE}.guard", fromlist=["check"])
cli = __import__(f"{PACKAGE}.cli", fromlist=["run_setup"])
settings_mod = __import__(f"{PACKAGE}.settings", fromlist=["Settings"])

IST_NOON = datetime(2026, 11, 18, 6, 30, tzinfo=UTC)  # 12:00 IST
IST_NIGHT = datetime(2026, 11, 18, 17, 0, tzinfo=UTC)  # 22:30 IST


# ---- guard ---------------------------------------------------------------------------------------------
def test_indian_calls_only_in_trai_hours() -> None:
    args = {"to": "+919876543210", "reason": "confirm booking"}
    assert guard.check("mcp_promptli_place_call", args, now=IST_NOON) is None
    blocked = guard.check("mcp_promptli_place_call", args, now=IST_NIGHT)
    assert blocked and blocked["action"] == "block" and "9 AM-9 PM IST" in blocked["message"]
    assert guard.check("mcp_promptli_place_call", {"to": "+15551234567"}, now=IST_NIGHT) is None


def test_scheduled_indian_calls_must_land_in_the_window() -> None:
    ok = {"to": "+919876543210", "run_at": "2026-11-19T10:00:00+05:30", "reason": "x"}
    late = {**ok, "run_at": "2026-11-19T21:30:00+05:30"}
    naive = {**ok, "run_at": "2026-11-19T10:00:00"}
    assert guard.check("mcp__promptli__schedule_call", ok) is None
    assert guard.check("mcp__promptli__schedule_call", late)["action"] == "block"
    assert guard.check("mcp__promptli__schedule_call", naive)["action"] == "block"


def test_openings_must_disclose_the_ai() -> None:
    base = {"to": "+15551234567", "reason": "x"}
    assert guard.check("mcp_promptli_place_call", {**base, "opening": "Hi, it's Sam's office."})["action"] == "block"
    assert guard.check("mcp_promptli_place_call", {**base, "opening": "Hi, this is Ada, an AI assistant."}) is None
    assert guard.check("mcp_promptli_place_call", base) is None  # no opening: the assistant's own greeting


def test_strangers_cannot_use_outbound_tools() -> None:
    SESSION_ENV.update({"HERMES_SESSION_PLATFORM": "promptli", "HERMES_SESSION_USER_ID": "+15557770000"})
    blocked = guard.pre_tool_call(tool_name="mcp_promptli_send_email", args={"to": "bob@x.co"})
    assert blocked and blocked["action"] == "block"
    assert guard.pre_tool_call(tool_name="web_search", args={}) is None


def test_the_owner_can_use_outbound_tools_from_promptli() -> None:
    SECRETS["PROMPTLI_ALLOWED_USERS"] = "+15550109999"
    SESSION_ENV.update({"HERMES_SESSION_PLATFORM": "promptli", "HERMES_SESSION_USER_ID": "+15550109999"})
    assert guard.pre_tool_call(tool_name="mcp_promptli_send_text", args={"to": ["+1555"]}) is None


def test_cli_sessions_are_not_restricted() -> None:
    SESSION_ENV.update({"HERMES_SESSION_PLATFORM": "cli"})
    assert guard.pre_tool_call(tool_name="mcp_promptli_send_email", args={}) is None


# ---- settings ------------------------------------------------------------------------------------------
def test_settings_normalize_and_default_safely() -> None:
    SECRETS.update({"PROMPTLI_ALLOWED_USERS": " Jane@Example.com , +1 (555) 010-1111 ", "PROMPTLI_VOICE": "loud"})
    s = settings_mod.Settings.load()
    assert s.allowed == frozenset({"jane@example.com", "+15550101111"})
    assert s.is_allowed("JANE@example.com") and not s.is_allowed("+15557770000")
    assert s.voice == "hosted" and s.toolsets == ("web", "todo", "no_mcp")
    assert s.base_url == "https://api.comms.promptli.com"


# ---- cli -----------------------------------------------------------------------------------------------
class FakeKeys:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def create(self, name: str, *, identity: str, description: str = "") -> Any:
        self.created.append({"name": name, "identity": identity})
        return type("K", (), {"key": "psk_test_minted"})()


class FakeIdentities:
    async def upsert(self, handle: str) -> Any:
        ident = type("I", (), {"handle": handle, "id": "idn_new"})()
        return type("U", (), {"identity": ident, "created": True})()


async def test_org_key_mints_an_identity_key(monkeypatch: pytest.MonkeyPatch) -> None:
    org = FakeClient(handle=None)
    org.api_keys = FakeKeys()  # type: ignore[attr-defined]
    org.identities = FakeIdentities()  # type: ignore[attr-defined]
    scoped = FakeClient(handle="ada")
    monkeypatch.setattr(cli, "_client", lambda key, _url: org if key == "psk_test_org" else scoped)
    conn = await cli.connect_key("psk_test_org", "https://x", None, lambda _q: "ada")
    assert conn.api_key == "psk_test_minted" and conn.handle == "ada"
    assert org.api_keys.created[0]["identity"] == "idn_new"  # type: ignore[attr-defined]


def test_setup_saves_through_hermes_and_never_prints_the_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_connect(key: str, base_url: str, handle: Any, ask: Any) -> Any:
        return cli.Connected(key, "ada", "test", "ada@agents.promptli.com", "+15550100000")

    monkeypatch.setattr(cli, "connect_key", fake_connect)
    answers = iter(["y", "1", "jane@example.com, +15550109999", "me@example.com", "hosted", "y"])
    code = cli.run_setup(
        argparse.Namespace(yes=False), ask=lambda _q: next(answers), secret_ask=lambda _q: "psk_test_not_a_real_key"
    )
    out = capsys.readouterr().out
    assert code == 0 and "psk_test_not_a_real_key" not in out
    assert SAVED_ENV["PROMPTLI_API_KEY"] == "psk_test_not_a_real_key"
    assert SAVED_ENV["PROMPTLI_IDENTITY"] == "ada"
    assert SAVED_ENV["PROMPTLI_ALLOWED_USERS"] == "+15550109999,jane@example.com"
    assert SAVED_ENV["PROMPTLI_ALLOW_ALL_USERS"] == "false"
    assert CONFIG["mcp_servers"]["promptli"] == {
        "url": "https://api.comms.promptli.com/mcp",
        "auth": "oauth",
        "trust": "untrusted",
    }


def test_mcp_entry_follows_the_base_url() -> None:
    assert cli.mcp_entry("http://localhost:8000")["url"] == "http://localhost:8000/mcp"


async def test_doctor_without_a_key() -> None:
    checks = await cli.doctor_checks(settings_mod.Settings.load())
    assert len(checks) == 1 and not checks[0].ok


# ---- registration and files ---------------------------------------------------------------------------
class FakeCtx:
    def __init__(self) -> None:
        self.platforms: list[dict[str, Any]] = []
        self.hooks: list[tuple[str, Any]] = []
        self.skills: list[tuple[str, Path]] = []
        self.cli: list[dict[str, Any]] = []

    def register_platform(self, **kw: Any) -> None:
        self.platforms.append(kw)

    def register_hook(self, name: str, fn: Any) -> None:
        self.hooks.append((name, fn))

    def register_skill(self, name: str, path: Path) -> None:
        self.skills.append((name, path))

    def register_cli_command(self, **kw: Any) -> None:
        self.cli.append(kw)


def test_register_wires_everything() -> None:
    ctx = FakeCtx()
    PLUGIN.register(ctx)
    (entry,) = ctx.platforms
    assert entry["name"] == "promptli" and entry["allowed_users_env"] == "PROMPTLI_ALLOWED_USERS"
    assert entry["allowlist_suppresses_pairing"] is True and entry["display_tier"] == "minimal"
    assert entry["allow_update_command"] is False
    assert [h for h, _ in ctx.hooks] == ["pre_tool_call"]
    assert ctx.skills[0][0] == "promptli-comms" and ctx.skills[0][1].exists()
    assert ctx.cli[0]["name"] == "promptli"
    parser = argparse.ArgumentParser()
    ctx.cli[0]["setup_fn"](parser)
    assert parser.parse_args(["doctor", "--json"]).promptli_command == "doctor"


def test_manifest_and_skill_frontmatter() -> None:
    manifest = (ROOT / "plugin.yaml").read_text()
    assert "kind: platform" in manifest and "name: PROMPTLI_API_KEY" in manifest
    assert "provides_hooks:\n  - pre_tool_call" in manifest
    skill = (ROOT / "skills" / "promptli-comms" / "SKILL.md").read_text()
    assert skill.startswith("---\nname: promptli-comms\ndescription: ")
