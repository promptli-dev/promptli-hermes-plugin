"""HermesTurnBridge: lets promptli-gateway's call drivers run their turns through Hermes's own gateway.

promptli-gateway's ``CallTurnDriver`` (Hermes speaks every word, Mode C) and ``SupervisorDriver`` (Promptli's
assistant talks and asks Hermes, Mode A) drive any ``RuntimeAdapter``. This bridge is that runtime adapter: a
turn becomes a ``MessageEvent`` in the caller's own Hermes session (the same one their emails and texts use),
and the reply Hermes sends back through ``PromptliAdapter.send`` resolves the turn instead of going out as a
message. Hermes owns the session and its history; nothing is resumed by id here.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Protocol

from promptli_gateway.adapter import (
    AdapterCapabilities,
    GatewayContext,
    TextDelta,
    TurnChunk,
    TurnDone,
    TurnError,
    TurnKind,
)
from promptli_gateway.framing import VOICE_RULES
from promptli_gateway.tools import ToolSpec

from .prompts import CALL_DISCLOSURE, POST_CALL_NOTE, SUPERVISOR_NOTE

log = logging.getLogger("promptli.hermes.bridge")

# Promptli plays fillers while the agent thinks and a graceful line at 45 s; supervisor answers are not
# latency-critical but a stale answer is useless.
TURN_TIMEOUT_S = {TurnKind.VOICE: 45.0, TurnKind.SUPERVISOR: 60.0}
SUPERVISOR_QUESTION = "The assistant asks you:"


class TurnHost(Protocol):
    """What the bridge needs from the platform adapter."""

    def expect_turn(self, token: str) -> asyncio.Future[str | None]: ...
    def forget_turn(self, token: str) -> None: ...
    async def inject_turn(self, session_key: str, prompt: str, *, kind: TurnKind, token: str) -> None: ...


def shape_prompt(prompt: str, kind: TurnKind) -> str:
    """Add what Hermes needs on top of promptli-gateway's framing for a Hermes session."""
    if kind is TurnKind.VOICE and VOICE_RULES in prompt:
        return prompt.replace(VOICE_RULES, f"{VOICE_RULES}\n- {CALL_DISCLOSURE}", 1)
    if kind is TurnKind.SUPERVISOR:
        return f"{SUPERVISOR_NOTE}\n\n{prompt}"
    if kind is TurnKind.POST_CALL:
        return f"{prompt}\n\n{POST_CALL_NOTE}"
    return prompt


class HermesTurnBridge:
    name = "hermes"
    capabilities = AdapterCapabilities(
        streams_text=False, native_tools=True, resumable_sessions=True, interruptible=True, voice=True
    )

    def __init__(self, host: TurnHost) -> None:
        self.host = host
        self.ctx: GatewayContext | None = None
        self._running: dict[str, str] = {}  # session key -> token of its live call turn

    async def start(self, ctx: GatewayContext, tools: list[ToolSpec]) -> None:
        self.ctx = ctx

    async def run_turn(
        self, session_key: str, prompt: str, *, kind: TurnKind, resume_id: str | None
    ) -> AsyncIterator[TurnChunk]:
        if kind is TurnKind.SUPERVISOR and SUPERVISOR_QUESTION not in prompt:
            # Start-of-call and periodic reviews only steer through call_add_fact / call_guidance, which
            # Hermes sessions do not have: skip them rather than spend a turn that can do nothing.
            yield TurnDone(text="NO_REPLY")
            return
        token = f"{kind.value}-{uuid.uuid4().hex[:16]}"
        if kind is TurnKind.POST_CALL:
            # The follow-up goes out as a text through the normal reply path; the call is already over.
            try:
                await self.host.inject_turn(session_key, shape_prompt(prompt, kind), kind=kind, token=token)
            except Exception as e:
                log.warning("post-call turn for %s not started: %s", session_key, e)
            yield TurnDone(text="")
            return
        fut = self.host.expect_turn(token)
        self._running[session_key] = token
        try:
            try:
                await self.host.inject_turn(session_key, shape_prompt(prompt, kind), kind=kind, token=token)
            except Exception as e:
                yield TurnError(f"hermes: could not start the turn: {e}", retryable=True)
                return
            try:
                text = await asyncio.wait_for(asyncio.shield(fut), TURN_TIMEOUT_S.get(kind, 60.0))
            except TimeoutError:
                yield TurnError("hermes: no reply in time", retryable=True)
                return
            if text is None:
                yield TurnError("hermes: turn interrupted", retryable=False)
                return
            if text:
                yield TextDelta(text)
            yield TurnDone(text=text)
        finally:
            if self._running.get(session_key) == token:
                self._running.pop(session_key, None)
            self.host.forget_turn(token)

    async def interrupt(self, session_key: str) -> None:
        """Barge-in: stop waiting for this turn. Hermes interrupts the run itself when the next utterance
        arrives in the same session (busy mode), and any late reply to this turn is dropped."""
        token = self._running.pop(session_key, None)
        if token:
            fut = self.host.expect_turn(token)
            if not fut.done():
                fut.set_result(None)

    async def reset_session(self, session_key: str) -> None:
        return None

    async def close(self) -> None:
        for session_key in list(self._running):
            with contextlib.suppress(Exception):
                await self.interrupt(session_key)
