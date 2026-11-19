"""`hermes promptli setup | status | doctor | mcp` (also the `hermes gateway setup` wizard).

Keys are read from a hidden prompt, never from argv, never printed, and saved only through Hermes's own
``.env`` writer (``hermes_cli.config.save_env_value``). An organization key is used once to mint an
identity-scoped key and is never stored.
"""

from __future__ import annotations

import argparse
import asyncio
import email.utils
import getpass
import json
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .prompts import STRANGER_WARNING
from .settings import (
    ALLOW_ALL_ENV,
    ALLOWED_USERS_ENV,
    API_KEY_ENV,
    BASE_URL_ENV,
    CONSOLE_URL,
    DEFAULT_BASE_URL,
    HOME_CHANNEL_ENV,
    HOSTED_MCP_URL,
    IDENTITY_ENV,
    PLATFORM,
    VOICE_ENV,
    Settings,
    address_list,
    normalize_address,
)

MCP_SERVER = "promptli"
USER_AGENT = "promptli-hermes/0.1.1 setup"

Ask = Callable[[str], str]


def _save_env(key: str, value: str) -> None:
    from hermes_cli.config import save_env_value

    save_env_value(key, value)


def _client(api_key: str, base_url: str) -> Any:
    from promptli import AsyncPromptli

    return AsyncPromptli(api_key, base_url=base_url, user_agent=USER_AGENT)


# ---- key → identity-scoped key -----------------------------------------------------------------------
@dataclass
class Connected:
    api_key: str
    handle: str
    env: str
    email: str | None = None
    number: str | None = None
    actions: list[str] = field(default_factory=list[str])


async def connect_key(api_key: str, base_url: str, handle: str | None, ask: Ask) -> Connected:
    """Check a key and return an identity-scoped key for one agent (minted from an org key if needed)."""
    async with _client(api_key, base_url) as p:
        who = await p.whoami()
        actions: list[str] = []
        if who.identity:
            scoped, handle = api_key, who.identity.handle
        else:
            handle = handle or ask("This is an organization key. Agent handle (existing or new): ").strip()
            if not handle:
                raise SystemExit("An agent handle is needed for an organization key.")
            up = await p.identities.upsert(handle)
            handle = up.identity.handle
            actions.append("created agent" if up.created else "agent exists")
            minted = await p.api_keys.create(
                f"Hermes on {socket.gethostname().split('.')[0]}",
                identity=up.identity.id,
                description="Minted by promptli-hermes setup (the organization key is not stored)",
            )
            scoped = minted.key
            actions.append("minted an identity-scoped key (organization key not stored)")
    async with _client(scoped, base_url) as p:
        me = p.identity(handle)
        ident = await me.get()
        st = await me.channel_status()
    return Connected(scoped, str(handle), who.env, ident.email_address, st.sms.number, actions)


# ---- MCP -----------------------------------------------------------------------------------------------
def mcp_entry(base_url: str) -> dict[str, Any]:
    url = HOSTED_MCP_URL if base_url.rstrip("/") == DEFAULT_BASE_URL else base_url.rstrip("/") + "/mcp"
    # trust: untrusted → every write-capable call (send, call, schedule) asks for approval first.
    return {"url": url, "auth": "oauth", "trust": "untrusted"}


def add_mcp_server(base_url: str) -> dict[str, Any]:
    from hermes_cli.config import load_config, save_config

    cfg = load_config()
    servers = dict(cfg.get("mcp_servers") or {})
    entry = {**dict(servers.get(MCP_SERVER) or {}), **mcp_entry(base_url)}
    servers[MCP_SERVER] = entry
    save_config({"mcp_servers": servers}, merge_existing=True)
    return entry


def mcp_status() -> dict[str, Any] | None:
    from hermes_cli.config import load_config

    servers = load_config().get("mcp_servers") or {}
    entry = servers.get(MCP_SERVER)
    return dict(entry) if isinstance(entry, dict) else None


# ---- setup ---------------------------------------------------------------------------------------------
def run_setup(args: argparse.Namespace | None = None, *, ask: Ask = input, secret_ask: Ask = getpass.getpass) -> int:
    yes = bool(getattr(args, "yes", False))
    print("Promptli Comms for Hermes\n")
    print("Before you continue:")
    print(STRANGER_WARNING)
    if not yes and ask("\nI understand. Continue? [y/N] ").strip().lower() not in ("y", "yes"):
        return 1
    current = Settings.load()
    base_url = (getattr(args, "base_url", None) or current.base_url or DEFAULT_BASE_URL).rstrip("/")
    key = current.api_key
    if key and ask("\nA Promptli key is already set. Keep it? [Y/n] ").strip().lower() in ("n", "no"):
        key = ""
    if not key:
        print(
            "\nCreate an identity-scoped key for your agent in the Promptli console "
            f"({CONSOLE_URL} → API keys), or paste an organization key and setup mints one for you."
        )
        key = secret_ask("Promptli API key (input hidden): ").strip()
    if not key:
        print("No key: nothing changed.")
        return 1
    try:
        conn = asyncio.run(connect_key(key, base_url, getattr(args, "identity", None) or current.identity, ask))
    except Exception as e:  # PromptliError or network: never echo the key
        print(f"Could not use that key: {getattr(e, 'code', type(e).__name__)}: {getattr(e, 'message', e)}")
        return 1
    _save_env(API_KEY_ENV, conn.api_key)
    _save_env(IDENTITY_ENV, conn.handle)
    if base_url != DEFAULT_BASE_URL:
        _save_env(BASE_URL_ENV, base_url)
    print(f"\nAgent @{conn.handle} is ready ({conn.env} mode).")
    print(f"  email  {conn.email or '-'}")
    print(f"  phone  {conn.number or 'none (add one in the console for SMS and calls)'}")
    for a in conn.actions:
        print(f"  - {a}")

    print("\nWho may reach this agent?")
    print("  1) Only people I list (recommended)")
    print("  2) Anyone who emails, texts or calls (sessions are restricted to web + todo)")
    choice = (getattr(args, "access", None) or ask("Choose 1 or 2 [1]: ").strip() or "1").strip()
    if choice == "2":
        _save_env(ALLOW_ALL_ENV, "true")
    else:
        _save_env(ALLOW_ALL_ENV, "false")
        listed = ask("Allowed emails / numbers, comma-separated (E.164, e.g. +15551234567): ").strip()
        if listed:
            _save_env(ALLOWED_USERS_ENV, ",".join(sorted(address_list(listed))))
    home = ask("Your own email or number for cron and notifications (empty to skip): ").strip()
    if home:
        _save_env(HOME_CHANNEL_ENV, normalize_address(home))
    voice = (
        ask(
            "Phone calls: hosted (Promptli's assistant talks, Hermes answers its questions), "
            "agent (Hermes speaks every word) or off [hosted]: "
        )
        .strip()
        .lower()
        or "hosted"
    )
    _save_env(VOICE_ENV, voice if voice in ("hosted", "agent", "off") else "hosted")

    if getattr(args, "mcp", True) and ask(
        "\nAdd the Promptli MCP tools (send email/texts, calls, contacts) to Hermes? [Y/n] "
    ).strip().lower() not in ("n", "no"):
        add_mcp_server(base_url)
        print(f"Added mcp_servers.{MCP_SERVER} (OAuth, trust: untrusted).")
        print(f"Log in with your browser: hermes mcp login {MCP_SERVER}")
    print("\nNext: restart the gateway so it connects:  hermes gateway restart   (or: hermes gateway run)")
    print("Check everything:                        hermes promptli doctor")
    return 0


def interactive_setup() -> None:
    """`hermes gateway setup` → Promptli."""
    run_setup(None)


# ---- status / doctor --------------------------------------------------------------------------------------
async def _status(settings: Settings) -> dict[str, Any]:
    data: dict[str, Any] = {
        "configured": bool(settings.api_key),
        "identity": settings.identity or None,
        "base_url": settings.base_url,
        "access": "anyone" if settings.allow_all else f"{len(settings.allowed)} listed address(es)",
        "home_channel": settings.home_channel or None,
        "calls": settings.voice,
        "toolsets": list(settings.toolsets) if settings.toolsets is not None else "inherit",
        "mcp": mcp_status(),
    }
    if not settings.api_key:
        return data
    try:
        async with _client(settings.api_key, settings.base_url) as p:
            who = await p.whoami()
            handle = who.identity.handle if who.identity else settings.identity
            st = await p.identity(handle).channel_status()
            data.update(
                identity=handle, env=who.env, email=st.email.address, sms=st.sms.number, sms_status=st.sms.status
            )
    except Exception as e:
        data["error"] = f"{getattr(e, 'code', type(e).__name__)}: {getattr(e, 'message', e)}"
    return data


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


async def doctor_checks(settings: Settings, *, check_link: bool = True) -> list[Check]:
    out: list[Check] = []
    if not settings.api_key:
        return [Check("key", False, f"{API_KEY_ENV} is not set: run `hermes promptli setup`")]
    from promptli import PromptliError

    async with _client(settings.api_key, settings.base_url) as p:
        try:
            who = await p.whoami()
        except PromptliError as e:
            return [Check("key", False, f"{e.code}: {e.message}")]
        handle = who.identity.handle if who.identity else settings.identity
        out.append(
            Check(
                "key",
                bool(who.identity) and who.key_kind != "provisional",
                f"{who.env} key for @{handle}" if who.identity else "organization key: run setup to mint an agent key",
            )
        )
        if not handle:
            return out
        try:
            st = await p.identity(handle).channel_status()
            out.append(Check("email", st.email.ready, st.email.address or "no mailbox"))
            out.append(
                Check(
                    "sms",
                    st.sms.ready or not st.sms.number,
                    f"{st.sms.number} {st.sms.status or ''}".strip() if st.sms.number else "no number (optional)",
                )
            )
        except PromptliError as e:
            out.append(Check("channels", False, f"{e.code}: {e.message}"))
        if check_link:
            # A separate instance id: the running gateway's connection is never taken over.
            link = p.identity(handle).link(
                instance=f"doctor-{socket.gethostname().split('.')[0]}", user_agent=USER_AGENT
            )
            try:
                await asyncio.wait_for(link.start(), timeout=15)
                out.append(Check("agent link", True, f"connected (latest seq {link.hello.get('latest_seq')})"))
            except Exception as e:
                out.append(Check("agent link", False, str(e)))
            finally:
                await link.close()
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10) as http:
                r = await http.get(settings.base_url + "/health")
            if "date" in r.headers:
                skew = abs(email.utils.parsedate_to_datetime(r.headers["date"]).timestamp() - time.time())
                out.append(Check("clock", skew < 60, f"skew {skew:.0f}s"))
        except Exception as e:
            out.append(Check("api reachable", False, str(e)))
    out.append(
        Check(
            "access",
            settings.allow_all or bool(settings.allowed),
            "anyone may reach the agent"
            if settings.allow_all
            else (
                f"{len(settings.allowed)} allowed address(es)"
                if settings.allowed
                else f"nobody can reach the agent: set {ALLOWED_USERS_ENV} or {ALLOW_ALL_ENV}=true"
            ),
        )
    )
    mcp = mcp_status()
    out.append(
        Check(
            "mcp tools",
            bool(mcp) and str(mcp.get("trust", "full")) == "untrusted",
            "not configured: `hermes promptli mcp`, then `hermes mcp login promptli`"
            if not mcp
            else f"{mcp.get('url')} (trust: {mcp.get('trust', 'full')})"
            + ("" if str(mcp.get("trust", "full")) == "untrusted" else " - set trust: untrusted so sends ask first"),
        )
    )
    return out


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str))


def _cmd(args: argparse.Namespace) -> int:
    sub = getattr(args, "promptli_command", None)
    settings = Settings.load()
    if sub == "setup":
        return run_setup(args)
    if sub == "status":
        data = asyncio.run(_status(settings))
        if getattr(args, "json", False):
            _print_json(data)
        else:
            for k in ("identity", "email", "sms", "access", "home_channel", "calls", "toolsets"):
                print(f"{k:<13} {data.get(k) if data.get(k) is not None else '-'}")
            mcp = data.get("mcp")
            print(f"{'mcp':<13} {mcp.get('url') if mcp else 'not configured'}")
            if data.get("error"):
                print(f"{'error':<13} {data['error']}")
        return 0
    if sub == "doctor":
        checks = asyncio.run(doctor_checks(settings, check_link=not getattr(args, "no_link", False)))
        if getattr(args, "json", False):
            _print_json({"ok": all(c.ok for c in checks), "checks": [c.__dict__ for c in checks]})
        else:
            for c in checks:
                print(f"{'ok  ' if c.ok else 'FAIL'} {c.name:<12} {c.detail}")
        return 0 if all(c.ok for c in checks) else 1
    if sub == "mcp":
        entry = add_mcp_server(settings.base_url)
        print(f"mcp_servers.{MCP_SERVER}: {entry['url']} (auth: oauth, trust: untrusted)")
        print(f"Now log in with your browser: hermes mcp login {MCP_SERVER}")
        return 0
    print("Usage: hermes promptli <setup|status|doctor|mcp>")
    return 2


def setup_argparse(parser: argparse.ArgumentParser) -> None:
    subs = parser.add_subparsers(dest="promptli_command")
    s = subs.add_parser("setup", help="Connect a Promptli agent (email + phone) to this Hermes")
    s.add_argument("--identity", help="Agent handle (for organization keys)")
    s.add_argument("--base-url", help="Promptli API base URL (local or test stacks)")
    s.add_argument("--access", choices=["1", "2"], help="1 = listed people only, 2 = anyone")
    s.add_argument("--no-mcp", dest="mcp", action="store_false", help="Do not add the Promptli MCP tools")
    s.add_argument("--yes", "-y", action="store_true", help="Accept the security warning")
    st = subs.add_parser("status", help="Agent, address, number and settings")
    st.add_argument("--json", action="store_true")
    d = subs.add_parser("doctor", help="Check the key, channels, Agent Link, clock, access and MCP tools")
    d.add_argument("--json", action="store_true")
    d.add_argument("--no-link", action="store_true", help="Skip the Agent Link connection test")
    subs.add_parser("mcp", help="Add the hosted Promptli MCP server (OAuth, approval on every send)")
    parser.set_defaults(func=handle)


def handle(args: argparse.Namespace) -> int:
    code = _cmd(args)
    if code:
        raise SystemExit(code)
    return 0


__all__ = ["PLATFORM", "handle", "interactive_setup", "run_setup", "setup_argparse"]
