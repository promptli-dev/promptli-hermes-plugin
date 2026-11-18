"""Durable plugin state in Hermes's per-plugin data directory.

Reuses promptli-gateway's sqlite store (Agent Link cursor, processed event ids) and adds:
- ``routes``: how to answer each inbound message (the token Hermes echoes back as ``reply_to``) and the last
  channel each person used, so a reply that Hermes delivers after a restart still goes to the right place;
- ``kv``: the stable Agent Link instance id (reconnects with the same id resume after the last ack).
"""

from __future__ import annotations

import json
import secrets
import socket
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from promptli_gateway.state import GatewayState

ROUTE_TTL_S = 30 * 86400


@dataclass
class Route:
    """Where a reply goes. ``channel``: email | sms | call | none."""

    channel: str
    chat_id: str
    address: str = ""  # who wrote (email address or E.164 number)
    message_id: str = ""  # email: the message to reply to (threaded)
    recipients: list[str] = field(default_factory=list[str])  # sms: one number, or a group's numbers
    event_id: str = ""
    seq: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> Route:
        data: dict[str, Any] = json.loads(raw)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class PluginState(GatewayState):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._db.execute(
            "create table if not exists routes (k text primary key, v text not null, updated_at real not null)"
        )

    # ---- kv ----------------------------------------------------------------------------------------
    def get(self, key: str) -> str | None:
        row = self._db.execute("select v from kv where k=?", (key,)).fetchone()
        return str(row[0]) if row else None

    def put(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("insert into kv(k,v) values(?,?) on conflict(k) do update set v=excluded.v", (key, value))

    def instance_id(self) -> str:
        """One stable Agent Link instance per Hermes home (profile)."""
        existing = self.get("instance_id")
        if existing:
            return existing
        new = f"hermes-{socket.gethostname().split('.')[0]}-{secrets.token_hex(4)}"
        self.put("instance_id", new)
        return new

    # ---- routes ------------------------------------------------------------------------------------
    def save_route(self, token: str, route: Route) -> None:
        now = time.time()
        raw = route.to_json()
        with self._lock:
            for key in (f"tok:{token}", f"chat:{route.chat_id}"):
                self._db.execute(
                    "insert into routes(k,v,updated_at) values(?,?,?) "
                    "on conflict(k) do update set v=excluded.v, updated_at=excluded.updated_at",
                    (key, raw, now),
                )

    def route_for_token(self, token: str | None) -> Route | None:
        return self._route(f"tok:{token}") if token else None

    def last_route(self, chat_id: str) -> Route | None:
        return self._route(f"chat:{chat_id}")

    def _route(self, key: str) -> Route | None:
        row = self._db.execute("select v from routes where k=?", (key,)).fetchone()
        return Route.from_json(row[0]) if row else None

    def prune_routes(self, older_than_s: float = ROUTE_TTL_S) -> None:
        with self._lock:
            self._db.execute("delete from routes where updated_at < ?", (time.time() - older_than_s,))
