"""Per-user conversational input flows.

Commands never require inline arguments. A command opens a short state
machine that asks for each input one message at a time; the user's next
free-text reply is routed back into the active flow. State lives in
``context.user_data`` and expires after FLOW_TTL seconds.

Modules register their flows at import time via :func:`register`.
"""

from __future__ import annotations

import logging
import time

from solders.pubkey import Pubkey

from sniper.amounts import AmountError, parse_positive_decimal
from sniper.ca_detect import extract_mints
from sniper.errors import UserFacingError
from sniper.handlers.common import ensure_subscribed, looks_like_secret, reply, services
from sniper.ui import esc

logger = logging.getLogger(__name__)

STATE_KEY = "awaiting_input"
FLOW_TTL = 900.0  # seconds before an unfinished conversation expires


class StepError(Exception):
    """A re-promptable validation problem; the flow stays open."""


class Step:
    def __init__(self, key: str, prompt, parse=None):
        self.key = key
        self.prompt = prompt  # str or callable(data) -> str
        self.parse = parse  # (text, update, context) -> value | dict

    def prompt_text(self, data: dict) -> str:
        return self.prompt(data) if callable(self.prompt) else self.prompt


FLOWS: dict[str, dict] = {}


def register(flow: str, steps: list[Step], execute) -> None:
    FLOWS[flow] = {"steps": steps, "execute": execute}


def active_flow(context) -> str | None:
    state = context.user_data.get(STATE_KEY)
    return None if not state else state.get("flow")


def cancel(context) -> str | None:
    """Drop any active flow. Returns the flow name that was active."""
    state = context.user_data.pop(STATE_KEY, None)
    return None if not state else state.get("flow")


def arm(context, flow: str) -> None:
    """Arm a flow without sending its first prompt.

    Used when the calling panel already asked the question — the user's
    next free-text reply is captured exactly like a flow begun with begin().
    """
    context.user_data[STATE_KEY] = {"flow": flow, "data": {}, "ts": time.monotonic()}


def _step_for(spec: dict, data: dict) -> Step | None:
    for step in spec["steps"]:
        if step.key not in data:
            return step
    return None


async def begin(update, context, flow: str, *, prefill: dict | None = None) -> None:
    """Start (or restart) a flow. Prefilled keys are validated like typed input.

    An invalid prefilled value falls back to asking for that step
    conversationally instead of failing the command.
    """
    spec = FLOWS.get(flow)
    if spec is None:
        raise UserFacingError("Unknown action. Send /help for the commands.")
    data: dict = {}
    notes: list[str] = []
    for key, raw in (prefill or {}).items():
        step = next((item for item in spec["steps"] if item.key == key), None)
        if step is None or step.parse is None:
            continue
        try:
            value = step.parse(str(raw), update, context)
            if isinstance(value, dict):
                data.update(value)
            else:
                data[key] = value
        except StepError as exc:
            notes.append(str(exc))
    context.user_data[STATE_KEY] = {"flow": flow, "data": data, "ts": time.monotonic()}
    step = _step_for(spec, data)
    if step is None:
        await _execute(update, context)
        return
    await reply(update, "\n".join([*notes, step.prompt_text(data)]).strip())


async def handle(update, context, text: str) -> bool:
    """Feed a free-text reply into the active flow. Returns True if consumed."""
    state = context.user_data.get(STATE_KEY)
    if not state:
        return False
    flow = state.get("flow")
    spec = FLOWS.get(flow)
    if spec is None:
        context.user_data.pop(STATE_KEY, None)
        return False
    if time.monotonic() - state.get("ts", 0) > FLOW_TTL:
        context.user_data.pop(STATE_KEY, None)
        await reply(update, "<b>EXPIRED</b>\nThis input has expired. Send the command again.")
        return True
    user = update.effective_user
    svc = services(context)
    if user and not svc.limiter.allow(f"cmd:{user.id}", svc.cfg.commands_per_30s, 30):
        await reply(update, "<b>PLEASE WAIT</b>\nToo many requests. Try again in a moment.")
        return True
    if not await ensure_subscribed(update, context):
        return True
    step = _step_for(spec, state["data"])
    if step is None:
        context.user_data.pop(STATE_KEY, None)
        return False
    try:
        value = step.parse(text, update, context) if step.parse else text
    except StepError as exc:
        state["ts"] = time.monotonic()
        await reply(update, esc(str(exc)))
        return True
    if isinstance(value, dict):
        state["data"].update(value)
    else:
        state["data"][step.key] = value
    state["ts"] = time.monotonic()
    step = _step_for(spec, state["data"])
    if step is not None:
        await reply(update, step.prompt_text(state["data"]))
        return True
    await _execute(update, context)
    return True


async def _execute(update, context) -> None:
    state = context.user_data.pop(STATE_KEY, None)
    if not state:
        return
    spec = FLOWS[state["flow"]]
    try:
        await spec["execute"](update, context, state["data"])
    except UserFacingError as exc:
        await reply(update, esc(str(exc)))


# ---------------------------------------------------------------------------
# Shared input parsers
# ---------------------------------------------------------------------------


def parse_mint(text, update=None, context=None) -> str:
    value = (text or "").strip()
    if not looks_like_secret(value):
        # The reply may be more than a bare CA: a sentence containing the
        # address, or a link to it (pump.fun, DexScreener, Solscan…).
        mints = extract_mints(value)
        if mints:
            value = mints[0]
    try:
        return str(Pubkey.from_string(value))
    except Exception:
        raise StepError(
            "Not a valid Solana token mint. Send the mint, or a supported link that contains it."
        ) from None


def parse_destination(text, update=None, context=None) -> str:
    value = (text or "").strip()
    try:
        return str(Pubkey.from_string(value))
    except Exception:
        raise StepError(
            "Not a valid Solana address. Send the recipient wallet address."
        ) from None


def parse_sol_amount(text, update=None, context=None) -> float:
    value = (text or "").strip().lower()
    if value.endswith("sol"):
        value = value[:-3].strip()
    try:
        amount = float(parse_positive_decimal(value))
    except AmountError:
        raise StepError("Enter a SOL amount. Example: 0.5.") from None
    return amount


def parse_token_or_percent(text, update=None, context=None) -> str:
    value = (text or "").strip().replace(" %", "%")
    if value.endswith("%"):
        inner = value[:-1].strip()
        try:
            percent = float(parse_positive_decimal(inner))
        except AmountError:
            raise StepError("Enter a percentage. Example: 50%.") from None
        if percent <= 0 or percent > 100:
            raise StepError("Percentage must be between 0 and 100. Example: 50%.")
        return value
    try:
        float(parse_positive_decimal(value))
    except AmountError:
        raise StepError(
            "Enter a token amount (example: 1.5) or a percentage (example: 50%)."
        ) from None
    return value


def parse_onoff(text, update=None, context=None) -> bool:
    value = (text or "").strip().lower()
    if value in {"on", "enable", "enabled", "start"}:
        return True
    if value in {"off", "disable", "disabled", "stop"}:
        return False
    raise StepError("Enter ON or OFF.")


def parse_telegram_id(text, update=None, context=None) -> str:
    value = (text or "").strip()
    if not value.isdigit():
        raise StepError("Enter a numeric Telegram user ID.")
    return value


def parse_setting_name(text, update=None, context=None) -> dict:
    from sniper.settings_logic import SETTING_NAMES

    parts = (text or "").strip().split()
    if not parts:
        raise StepError(f"Which setting? {', '.join(SETTING_NAMES)}")
    name = parts[0].lower()
    if name not in SETTING_NAMES:
        raise StepError(
            f"Unknown setting “{esc(parts[0])}”. Options: {', '.join(SETTING_NAMES)}"
        )
    if len(parts) == 1:
        return {"name": name}
    return {"name": name, "value": " ".join(parts[1:])}
