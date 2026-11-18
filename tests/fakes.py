"""In-memory stand-ins for the Promptli API and Agent Link (no network, nothing is ever sent)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any


@dataclass
class Sent:
    kind: str
    args: dict[str, Any]


class FakeLink:
    def __init__(self, capabilities: list[str], instance: str) -> None:
        self.capabilities = capabilities
        self.instance = instance
        self.hello = {"cursor": 0, "latest_seq": 0}
        self.acks: list[int] = []
        self.frames: list[dict[str, Any]] = []  # client -> server
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.started = False
        self.closed = False

    async def start(self, *_: Any, **__: Any) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True
        self.queue.put_nowait(None)

    async def ack(self, seq: int) -> None:
        self.acks.append(seq)

    async def send(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)

    def __aiter__(self) -> Any:
        return self._iter()

    async def _iter(self) -> Any:
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item


class FakeIdentity:
    def __init__(self, client: FakeClient, handle: str) -> None:
        self.client = client
        self.handle = handle

    async def get(self) -> Any:
        return SimpleNamespace(handle=self.handle, display_name="Ada", email_address="ada@agents.promptli.com")

    async def channel_status(self) -> Any:
        return SimpleNamespace(
            email=SimpleNamespace(address="ada@agents.promptli.com", ready=True),
            sms=SimpleNamespace(number="+15550100000", ready=True, status="active"),
            issues=[],
        )

    async def reply(self, message_id: str, *, text: str | None = None, **_: Any) -> Any:
        self.client.sent.append(Sent("reply", {"message_id": message_id, "text": text}))
        return SimpleNamespace(id=f"msg_reply_{len(self.client.sent)}")

    async def send_email(self, to: list[str], subject: str = "", *, text: str | None = None, **_: Any) -> Any:
        self.client.sent.append(Sent("email", {"to": to, "subject": subject, "text": text}))
        return SimpleNamespace(id=f"msg_out_{len(self.client.sent)}")

    async def send_text(self, to: list[str], text: str = "", **_: Any) -> Any:
        if self.client.fail_with is not None:
            raise self.client.fail_with
        self.client.sent.append(Sent("text", {"to": list(to), "text": text}))
        return SimpleNamespace(id=f"txt_out_{len(self.client.sent)}")

    def link(self, *, instance: str, capabilities: Any = (), user_agent: str | None = None, **_: Any) -> FakeLink:
        self.client.link = FakeLink(list(capabilities), instance)
        return self.client.link


@dataclass
class FakeClient:
    handle: str | None = "ada"
    sent: list[Sent] = field(default_factory=list)
    link: FakeLink | None = None
    fail_with: Exception | None = None
    closed: bool = False

    async def whoami(self) -> Any:
        identity = SimpleNamespace(handle=self.handle, id="idn_1") if self.handle else None
        return SimpleNamespace(identity=identity, env="test", key_kind="identity", org_name="Org")

    def identity(self, handle: str) -> FakeIdentity:
        return FakeIdentity(self, handle)

    async def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()


def email_event(
    seq: int, body: str = "Hello there", sender: str = "Jane@Example.com", contact_key: str = "ctc_jane"
) -> dict[str, Any]:
    return {
        "id": f"evt_{seq}",
        "seq": seq,
        "type": "message.received",
        "data": {
            "message": {
                "id": f"msg_{seq}",
                "thread_id": "thr_1",
                "from": sender,
                "subject": "Hi",
                "text": body,
                "html": None,
                "attachments": [],
                "folder": "inbox",
            },
            "contacts": [{"id": contact_key, "name": "Jane"}],
            "contact_key": contact_key,
        },
    }


def text_event(
    seq: int,
    body: str = "hey",
    sender: str = "+15550101111",
    contact_key: str = "ctc_jane",
    group: bool = False,
    keyword: str | None = None,
) -> dict[str, Any]:
    return {
        "id": f"evt_{seq}",
        "seq": seq,
        "type": "text.received",
        "data": {
            "text": {"id": f"txt_{seq}", "from": sender, "body": body, "media": []},
            "conversation": {
                "id": "tcv_1",
                "is_group": group,
                "remotes": [sender, "+15550102222"] if group else [sender],
            },
            "contacts": [],
            "contact_key": contact_key,
            "keyword": keyword,
        },
    }
