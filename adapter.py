"""Promptli Comms as a Hermes gateway platform.

One outbound WebSocket (Agent Link) carries every inbound email, text and call, so no public URL, tunnel or
webhook is needed, and it resumes after the last acknowledged event when the gateway restarts.

- Each person is one Hermes session: ``chat_id`` is Promptli's ``contact_key``, the same for a person's email
  address and phone number. Group texts get one session per group.
- Inbound content is framed as untrusted (``<external_content>``) by promptli-gateway's framing.
- Hermes's reply comes back through ``send()``; the ``reply_to`` token says which message it answers, so it
  goes out on the same channel: a threaded email reply, or SMS without markdown. ``NO_REPLY`` and Hermes's
  own busy notices are never sent to a person.
- Calls: in ``hosted`` mode Promptli's assistant talks and asks Hermes when it needs an answer; in ``agent``
  mode Hermes speaks every word (Mode C). Both run through promptli-gateway's call drivers via the bridge.
- Senders are checked before Hermes sees anything: PROMPTLI_ALLOWED_USERS, or PROMPTLI_ALLOW_ALL_USERS=true.
  A stranger who is not let in is ignored without a reply (no pairing codes are sent by email or text).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType, ProcessingOutcome
from promptli import AsyncPromptli, PromptliError
from promptli.calls_live import CallDispatcher, LiveCall
from promptli_gateway.adapter import GatewayContext, TurnKind
from promptli_gateway.framing import (
    contact_label,
    frame_email,
    frame_texts,
    frame_voicemail,
    is_no_reply,
    split_sms,
    strip_markdown,
)
from promptli_gateway.supervisor import SupervisorDriver
from promptli_gateway.voice import CallTurnDriver

from .bridge import HermesTurnBridge
from .settings import PLATFORM, PLUGIN_NAME, Settings, address_kind, normalize_address
from .store import PluginState, Route

log = logging.getLogger("promptli.hermes")

USER_AGENT = "promptli-hermes/0.1.1"
# Gateway status lines (interrupting / queued / steered / saved): fine in a chat app, wrong in someone's
# inbox or as a billed text.
SYSTEM_PREFIXES = ("⚡", "⏳", "⏩", "💾")
EMAIL_SUBJECT = "Message from your agent"
# An event Hermes accepted but never reported back on (merged into another turn, dropped by a policy) is
# acknowledged after this long, so one lost completion never stalls the Agent Link cursor.
ACK_FALLBACK_S = 900.0

ClientFactory = Callable[[Settings], AsyncPromptli]


def _default_client(settings: Settings) -> AsyncPromptli:
    # Always explicit: the SDK would otherwise fall back to the process env, which may belong to another
    # profile under a multiplexed gateway.
    return AsyncPromptli(settings.api_key, base_url=settings.base_url, user_agent=USER_AGENT)


def _default_state_path() -> Path:
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir(PLUGIN_NAME) / "state.db"


def parse_target(raw: str) -> str:
    """`email:x@y`, `sms:+1555…`, `phone:+1555…` or a bare address → the normalized address."""
    text = str(raw or "").strip()
    for prefix in ("email:", "sms:", "phone:", "tel:"):
        if text.lower().startswith(prefix):
            text = text[len(prefix) :]
            break
    return normalize_address(text)


class PromptliAdapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = 0  # send() splits texts itself; emails are one message
    splits_long_messages = True

    def __init__(
        self,
        config: PlatformConfig,
        *,
        client_factory: ClientFactory | None = None,
        state_path: Path | None = None,
    ) -> None:
        super().__init__(config, Platform(PLATFORM))
        self.settings = Settings.load(getattr(config, "extra", None))
        self._client_factory = client_factory or _default_client
        self._state_path = state_path
        self.client: AsyncPromptli | None = None
        self.state: PluginState | None = None
        self.handle = ""
        self.identity_email: str | None = None
        self.identity_number: str | None = None
        self.link: Any = None
        self.dispatcher: CallDispatcher | None = None
        self.bridge = HermesTurnBridge(self)
        self.voice: CallTurnDriver | None = None
        self.supervisor: SupervisorDriver | None = None
        self._consumer: asyncio.Task[None] | None = None
        self._turns: dict[str, asyncio.Future[str | None]] = {}
        self._callers: dict[str, str] = {}  # session key (contact_key) -> caller's number, for follow-ups
        self._pending: dict[str, int] = {}  # token handed to Hermes -> Agent Link seq it came from
        self._event_ids: dict[str, str] = {}  # token -> Promptli event id
        self._open_seqs: set[int] = set()
        self._max_seen = 0
        self._fallbacks: dict[str, asyncio.TimerHandle] = {}

    # ---- lifecycle ---------------------------------------------------------------------------------
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        s = self.settings
        if not s.api_key:
            self._set_fatal_error(
                "promptli_missing_key", "PROMPTLI_API_KEY is not set: run `hermes promptli setup`.", retryable=False
            )
            return False
        self.client = self._client_factory(s)
        try:
            who = await self.client.whoami()
            self.handle = who.identity.handle if who.identity else s.identity
            if not self.handle:
                self._set_fatal_error(
                    "promptli_identity_required",
                    "This is an organization key: set PROMPTLI_IDENTITY, or run `hermes promptli setup`.",
                    retryable=False,
                )
                return False
            me = self.client.identity(self.handle)
            ident = await me.get()
            status = await me.channel_status()
        except PromptliError as e:
            retryable = e.status not in (401, 403)
            self._set_fatal_error(f"promptli_{e.code}", f"Promptli: {e.message}", retryable=retryable)
            log.error("[promptli] cannot start: %s: %s", e.code, e.message)
            return False
        self.identity_email = ident.email_address
        self.identity_number = status.sms.number
        name = ident.display_name or ident.handle
        self.state = PluginState(self._state_path or _default_state_path())
        self.state.prune_routes()
        self.state.prune()

        ctx = GatewayContext(self.handle, self.identity_email, self.identity_number, None)
        await self.bridge.start(ctx, [])
        self.voice = CallTurnDriver(
            self.bridge, identity_name=name, get_session=lambda _k: None, set_session=lambda _k, _v: None
        )
        self.supervisor = SupervisorDriver(
            self.bridge,
            self.voice,
            get_session=lambda _k: None,
            set_session=lambda _k, _v: None,
            review_every=10**9,  # reviews steer with tools Hermes sessions do not have
        )
        caps = ["channel_turns"] + (["voice"] if s.voice == "agent" else [])
        self.link = self.client.identity(self.handle).link(
            instance=self.state.instance_id(), capabilities=caps, user_agent=USER_AGENT
        )
        try:
            await self.link.start()
        except (PromptliError, TimeoutError, OSError) as e:
            fatal = isinstance(e, PromptliError) and e.status in (401, 403)
            self._set_fatal_error("promptli_link", f"Agent Link: {e}", retryable=not fatal)
            log.error("[promptli] Agent Link did not connect: %s", e)
            return False
        self.dispatcher = CallDispatcher(
            self.link,
            self._on_call,
            on_supervise=self._on_supervise if s.voice != "off" else None,
        )
        if not s.allow_all and not s.allowed:
            log.warning(
                "[promptli] neither PROMPTLI_ALLOWED_USERS nor PROMPTLI_ALLOW_ALL_USERS is set: "
                "every inbound email, text and call is ignored"
            )
        self._running = True
        self._mark_connected()
        self._consumer = asyncio.create_task(self._consume())
        self._wire_plugin_handlers(None)
        log.info(
            "[promptli] connected as @%s (%s, %s), calls: %s",
            self.handle,
            self.identity_email or "no mailbox",
            self.identity_number or "no number",
            s.voice,
        )
        return True

    async def disconnect(self) -> None:
        self._running = False
        for handle in self._fallbacks.values():
            handle.cancel()
        self._fallbacks.clear()
        if self._consumer is not None:
            self._consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._consumer
            self._consumer = None
        if self.dispatcher is not None:
            with contextlib.suppress(Exception):
                await self.dispatcher.close()
        await self.bridge.close()
        if self.link is not None:
            with contextlib.suppress(Exception):
                await self.link.close()
        if self.client is not None:
            with contextlib.suppress(Exception):
                await self.client.close()
        if self.state is not None:
            self.state.close()
            self.state = None
        self._mark_disconnected()

    # ---- inbound -----------------------------------------------------------------------------------
    async def _consume(self) -> None:
        try:
            async for event in self.link:
                if self.dispatcher is not None and await self.dispatcher.handle(event):
                    continue  # live call frames
                if "seq" in event and "id" in event:
                    try:
                        await self.on_link_event(event)
                    except Exception:
                        log.exception("[promptli] could not handle event %s", event.get("id"))
        except asyncio.CancelledError:
            raise
        except PromptliError as e:
            fatal = e.status in (401, 403)
            log.error("[promptli] Agent Link closed: %s: %s", e.code, e.message)
            self._set_fatal_error(f"promptli_{e.code}", f"Agent Link: {e.message}", retryable=not fatal)

    async def on_link_event(self, event: dict[str, Any]) -> None:
        """One Agent Link event: dedupe, check the sender, hand it to Hermes in that person's session."""
        assert self.state is not None
        seq = int(event["seq"])
        self._max_seen = max(self._max_seen, seq)
        if self.state.seen(str(event["id"])):
            await self._advance()
            return
        built = self._build(event)
        if built is None:
            self.state.mark_processed(str(event["id"]))
            await self._advance()
            return
        msg, route = built
        token = str(msg.message_id)
        self.state.save_route(token, route)
        self._pending[token] = seq
        self._event_ids[token] = str(event["id"])
        self._open_seqs.add(seq)
        loop = asyncio.get_running_loop()
        self._fallbacks[token] = loop.call_later(ACK_FALLBACK_S, lambda: asyncio.ensure_future(self._done(token)))
        await self.handle_message(msg)

    def _build(self, event: dict[str, Any]) -> tuple[MessageEvent, Route] | None:
        kind = event.get("type")
        data: dict[str, Any] = event.get("data") or {}
        token = f"pl-{event['id']}"
        if kind == "message.received":
            msg = data.get("message") or {}
            if msg.get("folder") not in (None, "inbox"):
                return None
            sender = normalize_address(msg.get("from"))
            if self.identity_email and sender == normalize_address(self.identity_email):
                return None  # our own loopback copy
            chat_id = str(data.get("contact_key") or f"email:{sender}")
            route = Route("email", chat_id, address=sender, message_id=str(msg.get("id") or ""))
            text = frame_email(event, self.identity_email)
        elif kind == "text.received":
            if data.get("keyword"):
                return None  # STOP / START / HELP are handled by the platform
            txt = data.get("text") or {}
            sender = normalize_address(txt.get("from"))
            conv = data.get("conversation") or {}
            if conv.get("is_group"):
                chat_id = f"sms-group:{conv.get('id')}"
                recipients = [str(r) for r in conv.get("remotes") or [sender]]
            else:
                chat_id = str(data.get("contact_key") or f"sms:{sender}")
                recipients = [sender]
            route = Route("sms", chat_id, address=sender, recipients=recipients)
            body = str(txt.get("body") or "").strip()
            # The owner may run Hermes commands (/new, /status) by text; strangers' text is always framed.
            text = (
                body
                if (self.settings.is_owner(sender) and body.startswith("/"))
                else frame_texts([event], self.identity_number)
            )
        elif kind == "call.ended":
            call = data.get("call") or {}
            if call.get("outcome") != "voicemail":
                return None  # live calls already ran as call turns
            sender = normalize_address(call.get("from"))
            chat_id = str(data.get("contact_key") or f"phone:{sender}")
            route = Route("none", chat_id, address=sender)
            text = frame_voicemail(event)
        else:
            return None
        if not sender or not self.settings.is_allowed(sender):
            log.info("[promptli] ignoring %s from a sender who is not allowed", kind)
            return None
        route.event_id, route.seq = str(event["id"]), int(event["seq"])
        label = contact_label(event) or sender
        return self._event(chat_id, text, token=token, sender=sender, label=label), route

    def _event(self, chat_id: str, text: str, *, token: str, sender: str, label: str) -> MessageEvent:
        source = self.build_source(
            chat_id=chat_id, chat_name=label, chat_type="dm", user_id=sender, user_name=label, message_id=token
        )
        return MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            user_id=sender,
            user_name=label,
            source=source,
            message_id=token,
            allow_gateway_control=self.settings.is_owner(sender) and text.startswith("/"),
            reply_expected=True,
        )

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        token = str(event.message_id or "")
        fut = self._turns.get(token)
        if fut is not None and not fut.done():
            fut.set_result("")  # the turn ended without sending anything (NO_REPLY / silence)
        await self._done(token)
        with contextlib.suppress(Exception):
            await super().on_processing_complete(event, outcome)

    async def _done(self, token: str) -> None:
        handle = self._fallbacks.pop(token, None)
        if handle is not None:
            handle.cancel()
        seq = self._pending.pop(token, None)
        event_id = self._event_ids.pop(token, None)
        if seq is None:
            return
        self._open_seqs.discard(seq)
        if self.state is not None and event_id:
            self.state.mark_processed(event_id)
        await self._advance()

    async def _advance(self) -> None:
        """Ack the highest seq below which every event has been processed (a crash mid-turn replays it)."""
        if self.state is None:
            return
        watermark = (min(self._open_seqs) - 1) if self._open_seqs else self._max_seen
        if watermark > self.state.cursor():
            self.state.set_cursor(watermark)
            if self.link is not None:
                with contextlib.suppress(Exception):
                    await self.link.ack(watermark)

    # ---- calls -------------------------------------------------------------------------------------
    async def _on_call(self, call: LiveCall) -> None:
        """Mode C: Hermes speaks every word (PROMPTLI_VOICE=agent)."""
        if self.voice is None or not self.settings.is_allowed(call.remote_number):
            await call.decline()  # Promptli tries the next session, then voicemail
            return
        self._callers[call.contact_key] = normalize_address(call.remote_number)
        await self.voice.on_call(call)

    async def _on_supervise(self, call: LiveCall) -> None:
        """Mode A: Promptli's assistant talks; Hermes answers when it asks."""
        if self.supervisor is None or not self.settings.is_allowed(call.remote_number):
            await call.decline()
            return
        self._callers[call.contact_key] = normalize_address(call.remote_number)
        await self.supervisor.on_supervise(call)

    # TurnHost (bridge.py) ------------------------------------------------------------------------------
    def expect_turn(self, token: str) -> asyncio.Future[str | None]:
        fut = self._turns.get(token)
        if fut is None:
            fut = asyncio.get_running_loop().create_future()
            self._turns[token] = fut
        return fut

    def forget_turn(self, token: str) -> None:
        self._turns.pop(token, None)

    async def inject_turn(self, session_key: str, prompt: str, *, kind: TurnKind, token: str) -> None:
        caller = self._callers.get(session_key, "")
        if kind is TurnKind.POST_CALL:
            route = Route("sms" if address_kind(caller) == "sms" else "none", session_key, caller, recipients=[caller])
        else:
            route = Route("call", session_key, address=caller)
        if self.state is not None:
            self.state.save_route(token, route)
        await self.handle_message(self._event(session_key, prompt, token=token, sender=caller, label=caller))

    # ---- outbound ----------------------------------------------------------------------------------
    async def send(
        self, chat_id: str, content: str, reply_to: str | None = None, metadata: dict[str, Any] | None = None
    ) -> SendResult:
        text = (content or "").strip()
        fut = self._turns.get(reply_to or "")
        if fut is not None:  # a live call turn: the reply is spoken, not sent
            if not fut.done():
                fut.set_result("" if is_no_reply(text) else text)
            return SendResult(success=True, message_id=reply_to)
        if not text or is_no_reply(text) or text.startswith(SYSTEM_PREFIXES):
            return SendResult(success=True, message_id=None)
        route = self.resolve_route(chat_id, reply_to)
        if route is None:
            return SendResult(
                success=False,
                error=f"promptli: {chat_id!r} is not a conversation or a configured address "
                "(PROMPTLI_ALLOWED_USERS / PROMPTLI_HOME_CHANNEL)",
            )
        if route.channel in ("call", "none"):
            return SendResult(success=True, message_id=None)  # a late reply to a finished call turn
        if self.client is None:
            return SendResult(success=False, error="promptli: not connected", retryable=True)
        return await deliver(self.client, self.handle, route, text)

    def resolve_route(self, chat_id: str, reply_to: str | None) -> Route | None:
        if self.state is not None:
            route = self.state.route_for_token(reply_to) or self.state.last_route(chat_id)
            if route is not None:
                return route
        return direct_route(self.settings, chat_id)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        route = self.state.last_route(chat_id) if self.state is not None else None
        return {"name": (route.address if route else "") or chat_id, "type": "dm"}

    def toolsets_for_source(self, source: Any) -> list[str] | None:
        toolsets = self.settings.toolsets
        return list(toolsets) if toolsets is not None else None


def direct_route(settings: Settings, target: str) -> Route | None:
    """A send that is not a reply (cron, `hermes send`): only to addresses the owner configured."""
    address = parse_target(target)
    kind = address_kind(address)
    if not kind or not settings.may_receive(address):
        return None
    return Route(kind, target, address=address, recipients=[address] if kind == "sms" else [])


async def deliver(client: AsyncPromptli, handle: str, route: Route, text: str) -> SendResult:
    me = client.identity(handle)
    try:
        if route.channel == "email":
            if route.message_id:
                sent = await me.reply(route.message_id, text=text)
            else:
                sent = await me.send_email([route.address], EMAIL_SUBJECT, text=text)
            return SendResult(success=True, message_id=sent.id)
        ids: list[str] = []
        for part in split_sms(strip_markdown(text)):
            sent = await me.send_text(route.recipients or [route.address], part)
            ids.append(sent.id)
        return SendResult(success=True, message_id=ids[-1] if ids else None, continuation_message_ids=tuple(ids))
    except PromptliError as e:
        log.warning("[promptli] %s send failed: %s: %s", route.channel, e.code, e.message)
        retryable = e.code in ("rate_limited", "provider_error") or (e.status or 0) >= 500
        return SendResult(success=False, error=f"{e.code}: {e.message}", retryable=retryable)


# ---- registry hooks (called lazily from __init__.py) -----------------------------------------------------
def check_requirements() -> bool:
    return bool(Settings.load().api_key)


def validate_config(config: Any) -> bool:
    return bool(Settings.load(getattr(config, "extra", None)).api_key)


def env_enablement() -> dict[str, Any] | None:
    from gateway.platforms._shared import seed_extra_from_env

    from .settings import API_KEY_ENV, HOME_CHANNEL_ENV, IDENTITY_ENV, secret

    if not secret(API_KEY_ENV):
        return None
    return seed_extra_from_env(((IDENTITY_ENV, "identity", None),), home_env=HOME_CHANNEL_ENV)


async def standalone_send(pconfig: Any, chat_id: str, message: str, **_: Any) -> dict[str, Any]:
    """Out-of-process delivery (cron in a separate process, `hermes send`): owner-configured addresses only."""
    settings = Settings.load(getattr(pconfig, "extra", None))
    route = direct_route(settings, chat_id)
    if route is None:
        return {"error": f"promptli: {chat_id!r} is not PROMPTLI_HOME_CHANNEL or in PROMPTLI_ALLOWED_USERS"}
    if not settings.api_key:
        return {"error": "promptli: PROMPTLI_API_KEY is not set"}
    async with _default_client(settings) as client:
        handle = settings.identity
        if not handle:
            who = await client.whoami()
            handle = who.identity.handle if who.identity else ""
        if not handle:
            return {"error": "promptli: set PROMPTLI_IDENTITY for an organization key"}
        result = await deliver(client, handle, route, message)
    if not result.success:
        return {"error": result.error or "promptli: send failed"}
    return {"success": True, "platform": PLATFORM, "chat_id": chat_id, "message_id": result.message_id}


def parse_target_ref(raw: str) -> tuple[str, str | None] | None:
    address = parse_target(raw)
    return (address, None) if address_kind(address) else None


def validate_target_ref(raw: str) -> bool | str:
    address = parse_target(raw)
    if not address_kind(address):
        return "not an email address or E.164 phone number"
    if not Settings.load().may_receive(address):
        return "only PROMPTLI_HOME_CHANNEL and PROMPTLI_ALLOWED_USERS can receive messages that are not replies"
    return True
