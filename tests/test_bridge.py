from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from conftest import PACKAGE, SECRETS, PlatformConfig
from fakes import FakeClient, email_event, text_event
from promptli.calls_live import LiveCall
from promptli_gateway.adapter import TextDelta, TurnDone, TurnError, TurnKind
from promptli_gateway.framing import VOICE_RULES

adapter_mod = __import__(f"{PACKAGE}.adapter", fromlist=["PromptliAdapter"])
bridge_mod = __import__(f"{PACKAGE}.bridge", fromlist=["shape_prompt"])
CALLER = "+15550109999"


async def connected(tmp_path: Path, **secrets: str) -> tuple[Any, FakeClient]:
    SECRETS.update({"PROMPTLI_API_KEY": "psk_test_fake", "PROMPTLI_ALLOWED_USERS": CALLER})
    SECRETS.update(secrets)
    fake = FakeClient()
    ad = adapter_mod.PromptliAdapter(PlatformConfig(), client_factory=lambda _s: fake, state_path=tmp_path / "s.db")
    assert await ad.connect()
    return ad, fake


async def collect(gen: Any) -> list[Any]:
    return [c async for c in gen]


async def test_voice_turn_reply_is_spoken_not_sent(tmp_path: Path) -> None:
    ad, fake = await connected(tmp_path)

    async def hermes(ev: Any) -> str:
        return "Hi, this is Ada, an AI assistant. How can I help?"

    ad.handler = hermes
    chunks = await collect(ad.bridge.run_turn("ctc_caller", "hello", kind=TurnKind.VOICE, resume_id=None))
    assert isinstance(chunks[0], TextDelta) and "AI assistant" in chunks[0].text
    assert isinstance(chunks[-1], TurnDone)
    assert fake.sent == []  # spoken on the call, never emailed or texted
    assert ad.handled[0].source.chat_id == "ctc_caller"
    await ad.disconnect()


async def test_turn_that_sends_nothing_finishes_empty(tmp_path: Path) -> None:
    ad, _ = await connected(tmp_path)

    async def silent(ev: Any) -> str:
        return ""

    ad.handler = silent
    chunks = await collect(ad.bridge.run_turn("ctc_caller", "hm", kind=TurnKind.VOICE, resume_id=None))
    assert len(chunks) == 1 and isinstance(chunks[0], TurnDone) and chunks[0].text == ""
    await ad.disconnect()


async def test_barge_in_abandons_the_turn(tmp_path: Path) -> None:
    ad, _ = await connected(tmp_path)
    gate = asyncio.Event()

    async def slow(ev: Any) -> str:
        await gate.wait()
        return "too late"

    ad.handler = slow
    run = asyncio.ensure_future(collect(ad.bridge.run_turn("ctc_caller", "q", kind=TurnKind.VOICE, resume_id=None)))
    await asyncio.sleep(0.01)
    await ad.bridge.interrupt("ctc_caller")
    chunks = await run
    assert isinstance(chunks[-1], TurnError)
    gate.set()
    await asyncio.sleep(0.01)  # the late reply is dropped quietly
    await ad.disconnect()


async def test_supervisor_reviews_are_skipped_and_questions_answered(tmp_path: Path) -> None:
    ad, _ = await connected(tmp_path)

    async def hermes(ev: Any) -> str:
        return "Yes, Sunday 2pm is free."

    ad.handler = hermes
    skipped = await collect(ad.bridge.run_turn("ctc_caller", "review", kind=TurnKind.SUPERVISOR, resume_id=None))
    assert [type(c) for c in skipped] == [TurnDone] and ad.handled == []
    q = "...\nThe assistant asks you: is Sunday free?"
    answered = await collect(ad.bridge.run_turn("ctc_caller", q, kind=TurnKind.SUPERVISOR, resume_id=None))
    assert answered[-1].text == "Yes, Sunday 2pm is free."
    await ad.disconnect()


async def test_post_call_follow_up_is_texted_to_the_caller(tmp_path: Path) -> None:
    ad, fake = await connected(tmp_path)
    ad._callers["ctc_caller"] = CALLER

    async def hermes(ev: Any) -> str:
        return "As promised: the address is 1 Main St."

    ad.handler = hermes
    await collect(ad.bridge.run_turn("ctc_caller", "call ended", kind=TurnKind.POST_CALL, resume_id=None))
    await asyncio.sleep(0.01)
    assert fake.sent[0].kind == "text" and fake.sent[0].args["to"] == [CALLER]
    await ad.disconnect()


def test_voice_prompts_carry_the_ai_disclosure() -> None:
    shaped = bridge_mod.shape_prompt(VOICE_RULES + "\nIncoming call from Jane.", TurnKind.VOICE)
    assert "you are an AI assistant" in shaped


async def test_live_call_end_to_end(tmp_path: Path) -> None:
    ad, fake = await connected(tmp_path, PROMPTLI_VOICE="agent")

    async def hermes(ev: Any) -> str:
        return "Hi, this is Ada, an AI assistant."

    ad.handler = hermes
    call = LiveCall(
        fake.link,
        {
            "type": "call.incoming",
            "call_id": "call_1",
            "direction": "inbound",
            "from": CALLER,
            "to": "+15550100000",
            "contact_key": "ctc_caller",
        },
    )
    task = asyncio.ensure_future(ad._on_call(call))
    await asyncio.sleep(0.01)
    call._push({"type": "call.start", "call_id": "call_1", "turn_id": "t0", "greet": True})
    await asyncio.sleep(0.05)
    call._push({"type": "call.stop", "call_id": "call_1", "reason": "hangup"})
    await asyncio.wait_for(task, 2)
    kinds = [f["type"] for f in fake.link.frames]  # type: ignore[union-attr]
    assert kinds[0] == "call.accept"
    spoken = "".join(
        f.get("delta", "") + f.get("text", "")
        for f in fake.link.frames  # type: ignore[union-attr]
        if f["type"] in ("call.text", "call.say")
    )
    assert "AI assistant" in spoken
    assert "you are an AI assistant" in ad.handled[0].text  # the disclosure rule reached Hermes
    await ad.disconnect()


async def test_callers_who_are_not_allowed_are_declined(tmp_path: Path) -> None:
    ad, fake = await connected(tmp_path, PROMPTLI_VOICE="agent")
    call = LiveCall(
        fake.link,
        {
            "type": "call.incoming",
            "call_id": "c2",
            "from": "+15557770000",
            "direction": "inbound",
            "contact_key": "ctc_x",
        },
    )
    await ad._on_call(call)
    assert fake.link.frames[0]["type"] == "call.decline"  # type: ignore[union-attr]
    assert ad.handled == []
    await ad.disconnect()


async def test_email_then_text_from_one_person_keep_their_own_channels(tmp_path: Path) -> None:
    """One person by email then text: each event keeps its own channel (promptli-gateway >= 0.2.2)."""
    ad, fake = await connected(tmp_path, PROMPTLI_ALLOWED_USERS=f"jane@example.com,{CALLER}")

    async def hermes(ev: Any) -> str:
        return "ok"

    ad.handler = hermes
    await ad.on_link_event(email_event(1, contact_key="ctc_jane"))
    await ad.on_link_event(text_event(2, sender=CALLER, contact_key="ctc_jane"))
    await asyncio.sleep(0.01)
    assert '<external_content channel="email">' in ad.handled[0].text
    assert '<external_content channel="sms">' in ad.handled[1].text
    assert [s.kind for s in fake.sent] == ["reply", "text"]
    await ad.disconnect()
