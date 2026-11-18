"""``pre_tool_call`` hook for the Promptli MCP tools (and any other tool named place_call / schedule_call).

- **Calling hours (TRAI).** Calls to Indian numbers (+91) are refused outside 9 AM-9 PM IST, both when placed
  now and when scheduled for later.
- **AI disclosure.** A call ``opening`` (the first words the callee hears) must say the caller is an AI.
- **Strangers.** In a Promptli conversation (a session opened by an outside sender) the outbound Promptli
  tools are refused unless the sender is one of the owner's configured addresses: the reply is the agent's
  final answer, sent on the person's own channel, so a stranger cannot make the agent email, text or phone
  someone else.

Everything else passes through to Hermes's normal approval rules (mark the Promptli MCP server
``trust: untrusted`` so each send asks first; `hermes promptli mcp` does that).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta, timezone
from typing import Any

from .settings import PLATFORM, Settings

IST = timezone(timedelta(hours=5, minutes=30), "IST")
CALL_WINDOW = (time(9, 0), time(21, 0))  # TRAI: 9 AM-9 PM IST
_TOOL = re.compile(r"^(?:mcp_+(?P<server>[a-z0-9_-]+?)_+)?(?P<tool>[a-z_]+)$")
CALL_TOOLS = {"place_call", "schedule_call"}
OUTBOUND_TOOLS = {
    "send_email",
    "reply_email",
    "send_text",
    "place_call",
    "schedule_call",
    "schedule_text",
    "cancel_scheduled",
    "add_contact_memory",
}
_AI = re.compile(r"\b(?:A\.?I\.?|artificial intelligence|virtual assistant|automated assistant)(?:\b|$)", re.I)


def _split(tool_name: str) -> tuple[str, str]:
    """(server, tool) for `mcp_<server>_<tool>` / `mcp__<server>__<tool>`; ('', name) for anything else."""
    name = tool_name.strip().lower()
    for tool in sorted(OUTBOUND_TOOLS | {"list_conversations", "get_conversation"}, key=len, reverse=True):
        if name == tool:
            return "", tool
        if name.startswith("mcp_") and name.endswith("_" + tool):
            server = name[len("mcp_") : -len(tool) - 1].strip("_")
            return server, tool
    m = _TOOL.match(name)
    return (m.group("server") or "", m.group("tool")) if m else ("", name)


def _is_indian(number: object) -> bool:
    return re.sub(r"[\s().-]", "", str(number or "")).startswith("+91")


def _in_window(moment: datetime) -> bool:
    local = moment.astimezone(IST).time()
    return CALL_WINDOW[0] <= local < CALL_WINDOW[1]


def _parse_when(raw: object) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else None  # a time without a zone cannot be checked


def _block(reason: str) -> dict[str, str]:
    return {"action": "block", "message": f"Promptli: {reason}"}


def _session(name: str) -> str:
    try:
        from gateway.session_context import get_session_env
    except ImportError:  # CLI-only installs without the gateway package
        return ""
    return str(get_session_env(name, "") or "")


def check(tool_name: str, args: dict[str, Any], *, now: datetime | None = None) -> dict[str, str] | None:
    """The directive for one tool call, or None to let it through. Pure, for tests."""
    _server, tool = _split(tool_name)
    if tool in CALL_TOOLS:
        to = args.get("to")
        if _is_indian(to):
            if tool == "place_call" and not _in_window(now or datetime.now(UTC)):
                return _block(
                    "calls to Indian numbers are only allowed 9 AM-9 PM IST (TRAI). Schedule it for later "
                    "with schedule_call, inside that window."
                )
            if tool == "schedule_call":
                when = _parse_when(args.get("run_at"))
                if when is None:
                    return _block("run_at needs a full ISO 8601 time with a timezone for calls to +91 numbers.")
                if not _in_window(when):
                    return _block("calls to Indian numbers must be scheduled between 9 AM and 9 PM IST (TRAI).")
        opening = str(args.get("opening") or "").strip()
        if opening and not _AI.search(opening):
            return _block(
                'the opening must say you are an AI assistant (for example: "Hi, this is Ada, an AI '
                'assistant calling for Sam"). Rewrite it and try again.'
            )
    return None


def pre_tool_call(tool_name: str = "", args: dict[str, Any] | None = None, **_: Any) -> dict[str, str] | None:
    args = args or {}
    _server, tool = _split(tool_name)
    if tool in OUTBOUND_TOOLS and _session("HERMES_SESSION_PLATFORM").lower() == PLATFORM:
        try:
            owner = Settings.load().is_owner(_session("HERMES_SESSION_USER_ID"))
        except Exception:
            owner = False
        if not owner:
            return _block(
                "outbound email, texts and calls are not available in a conversation someone else started. "
                "Your final answer is sent to them as the reply."
            )
    return check(tool_name, args)
