"""Settings for the Promptli platform, read through Hermes's profile-scoped secret reader.

Never `os.environ`: under a multiplexed gateway the process env holds the default profile's values, and a raw
read would connect the wrong profile with the wrong key (catalog rule 11).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

PLATFORM = "promptli"
PLUGIN_NAME = "promptli-comms"
DEFAULT_BASE_URL = "https://api.comms.promptli.com"
CONSOLE_URL = "https://comms.promptli.com"
HOSTED_MCP_URL = "https://api.comms.promptli.com/mcp"

API_KEY_ENV = "PROMPTLI_API_KEY"
IDENTITY_ENV = "PROMPTLI_IDENTITY"
BASE_URL_ENV = "PROMPTLI_BASE_URL"
ALLOWED_USERS_ENV = "PROMPTLI_ALLOWED_USERS"
ALLOW_ALL_ENV = "PROMPTLI_ALLOW_ALL_USERS"
HOME_CHANNEL_ENV = "PROMPTLI_HOME_CHANNEL"
VOICE_ENV = "PROMPTLI_VOICE"
TOOLSETS_ENV = "PROMPTLI_TOOLSETS"

VOICE_MODES = ("hosted", "agent", "off")
# Promptli sessions are opened by outside senders. They get the web and a todo list, and no MCP servers:
# `session_search`, memory and the Promptli MCP read tools would show one person another person's conversations.
DEFAULT_TOOLSETS = ("web", "todo", "no_mcp")
_TRUTHY = ("1", "true", "yes", "on")
_E164 = re.compile(r"^\+[1-9]\d{6,14}$")


def secret(name: str, default: str = "") -> str:
    """One setting from the active profile's secret scope (``.env`` / secret sources)."""
    from gateway.platforms._shared import get_scoped_secret

    value = get_scoped_secret(name, default)
    return str(value if value is not None else default).strip()


def normalize_address(raw: object) -> str:
    """Canonical form of an email address or phone number: lowercased email, E.164 number."""
    text = str(raw or "").strip()
    if "@" in text:
        return text.lower()
    digits = re.sub(r"[\s().-]", "", text)
    return digits if _E164.match(digits) else text


def address_kind(address: str) -> str:
    """'email', 'sms' or '' for anything that is neither."""
    if "@" in address and "." in address.rsplit("@", 1)[-1]:
        return "email"
    return "sms" if _E164.match(address) else ""


def address_list(raw: str) -> frozenset[str]:
    return frozenset(a for a in (normalize_address(x) for x in raw.split(",")) if a)


@dataclass(frozen=True)
class Settings:
    api_key: str = ""
    identity: str = ""
    base_url: str = DEFAULT_BASE_URL
    allowed: frozenset[str] = field(default_factory=frozenset[str])
    allow_all: bool = False
    home_channel: str = ""
    voice: str = "hosted"
    toolsets: tuple[str, ...] | None = DEFAULT_TOOLSETS

    @classmethod
    def load(cls, extra: dict[str, Any] | None = None) -> Settings:
        """Env (profile-scoped) wins, then the platform's ``config.yaml`` extra, then defaults."""
        extra = extra or {}

        def pick(env: str, key: str, default: str = "") -> str:
            return secret(env) or str(extra.get(key) or default).strip()

        voice = pick(VOICE_ENV, "voice", "hosted").lower()
        raw_toolsets = pick(TOOLSETS_ENV, "toolsets")
        toolsets: tuple[str, ...] | None = DEFAULT_TOOLSETS
        if raw_toolsets.lower() == "inherit":
            toolsets = None
        elif raw_toolsets:
            toolsets = tuple(t.strip() for t in raw_toolsets.split(",") if t.strip())
        return cls(
            api_key=secret(API_KEY_ENV),
            identity=pick(IDENTITY_ENV, "identity"),
            base_url=(pick(BASE_URL_ENV, "base_url") or DEFAULT_BASE_URL).rstrip("/"),
            allowed=address_list(pick(ALLOWED_USERS_ENV, "allowed_users")),
            allow_all=pick(ALLOW_ALL_ENV, "allow_all_users").lower() in _TRUTHY,
            home_channel=normalize_address(pick(HOME_CHANNEL_ENV, "home_channel")),
            voice=voice if voice in VOICE_MODES else "hosted",
            toolsets=toolsets,
        )

    def is_allowed(self, address: str) -> bool:
        """May this sender reach the agent? With neither list nor allow-all set, nobody may (no pairing
        codes are ever sent to strangers)."""
        return self.allow_all or normalize_address(address) in self.allowed

    def is_owner(self, address: str) -> bool:
        """Explicitly listed senders (and the home channel) are the owner's people: they may use /commands."""
        a = normalize_address(address)
        return bool(a) and (a in self.allowed or a == self.home_channel)

    def may_receive(self, address: str) -> bool:
        """Outbound sends that are not a reply (cron, `hermes send`) only reach configured addresses."""
        a = normalize_address(address)
        return bool(a) and (a in self.allowed or a == self.home_channel)
