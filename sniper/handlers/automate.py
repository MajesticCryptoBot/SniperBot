""" /automate — pattern scan, trailing dip, trailing stop.

The command only creates a row in automate_jobs. The monitor in
sniper/automate_monitor.py does the work. Existing buy, sell, autosnipe,
TP/SL, and limit-order commands are not called from here.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from sniper.errors import UserFacingError
from sniper.handlers import flows
from sniper.handlers.common import best_effort_answer, callback_guard, command, ensure_private, reply, services
from sniper.ui import card, esc, info, lead, sol

logger = logging.getLogger(__name__)

MAX_ACTIVE = 5

PROMPT = (
    "<b>AUTOMATE</b>\n"
    + lead("Patterns, then a trailing dip, then a trailing stop. One mint.")
    + "\n\nSend the token mint. P15 covers a bull push. P18 covers a bear rebound. "
    "A valid score arms the trailing dip. The buy happens only when price rebounds into that level. "
    "After the fill, the trailing stop locks profit through RUN → MOONSHOT.\n\n"
    + info("Buy size is your autosnipe amount. /cancel leaves everything as it is.")
)


def _status_line(row: dict) -> str:
    status = str(row.get("status") or "")
    pattern = row.get("pattern")
    tag = f"P{int(pattern)} " if pattern else ""
    score = row.get("pattern_score")
    score_txt = f" {int(score)}" if score is not None else ""
    extra = ""
    if status == "dip" and row.get("dip_level"):
        extra = f" · level ${float(row['dip_level']):.6g}"
    if status == "managing":
        phase = int(row.get("profit_phase") or 0) + 1
        extra = f" · phase {phase}/6"
    short = f"{row['token_mint'][:6]}…{row['token_mint'][-4:]}"
    return f"#{row['id']} {esc(short)} · {tag}{esc(status)}{score_txt}{extra} · {sol(row['amount_sol'])}"


def _list_keyboard(rows: list[dict]) -> InlineKeyboardMarkup | None:
    active = [row for row in rows if row["status"] in {"scanning", "dip", "buying", "managing", "selling"}]
    if not active:
        return None
    buttons = [
        [InlineKeyboardButton(f"Stop #{row['id']}", callback_data=f"am:stop:{row['id']}")]
        for row in active[:8]
    ]
    return InlineKeyboardMarkup(buttons)


async def _render_list(update, svc, user_id: str) -> None:
    rows = await svc.db.automate_jobs_for(user_id)
    if not rows:
        await reply(
            update,
            "<b>AUTOMATE</b>\n" + lead("No jobs yet.") + "\n\n" + info("Send /automate and a mint to start one."),
        )
        return
    await reply(
        update,
        "<b>AUTOMATE</b>\n"
        + lead("Scanning and open trails. Stopping a job does not sell.")
        + "\n\n"
        + card([_status_line(row) for row in rows[:10]])
        + "\n\n"
        + info("/automate_off stops every open job. A managing job stops trailing; the tokens stay."),
        _list_keyboard(rows),
    )


@command("automate", needs_wallet=True)
async def automate(update, context) -> None:
    svc = services(context)
    user_id = str(update.effective_user.id)
    user = await svc.db.get_user(user_id)
    if user and user.get("trading_paused"):
        raise UserFacingError("Trading is paused. Use /resume, then /automate.")
    if not svc.cfg.is_mainnet:
        raise UserFacingError("Automate is unavailable on the current network configuration.")
    await flows.begin(update, context, "automate")


@command("automate_list", needs_wallet=True)
async def automate_list(update, context) -> None:
    await _render_list(update, services(context), str(update.effective_user.id))


@command("automate_off", needs_wallet=True)
async def automate_off(update, context) -> None:
    svc = services(context)
    user_id = str(update.effective_user.id)
    stopped = await svc.db.cancel_automate_for_user(user_id)
    if not stopped:
        await reply(update, "<b>AUTOMATE</b>\n" + info("Nothing was running."))
        return
    await reply(
        update,
        "<b>AUTOMATE STOPPED</b>\n"
        + card([f"Jobs released: {stopped}", "No sell was sent."])
        + "\n\n"
        + info("Open positions are unchanged. /sell if you want out."),
    )


async def _arm(update, context, data: dict) -> None:
    svc = services(context)
    user_id = str(update.effective_user.id)
    mint = data["mint"]
    user = await svc.db.get_user(user_id)
    if not user or user.get("trading_paused"):
        raise UserFacingError("Trading is paused. Use /resume, then /automate.")
    if not svc.cfg.is_mainnet:
        raise UserFacingError("Automate is unavailable on the current network configuration.")
    existing = await svc.db.active_automate_for(user_id, mint)
    if existing:
        raise UserFacingError(f"This mint is already on automate (#{existing['id']}, {existing['status']}).")
    if await svc.db.count_active_automate(user_id) >= MAX_ACTIVE:
        raise UserFacingError(f"Automate is full ({MAX_ACTIVE}). /automate_off releases a job first.")
    settings = await svc.db.get_settings(user_id)
    amount = float(settings.get("autosnipe_amount_sol") or 0)
    if amount <= 0:
        raise UserFacingError("Set a snipe amount in /config before /automate. That size is the buy.")
    if amount > float(settings["max_sol_per_trade"]):
        raise UserFacingError("Snipe amount is above the per-trade maximum. Lower it in /config.")
    if svc.safety is not None:
        report = await svc.safety.evaluate(mint)
        if not report.ok_for_autosnipe:
            raise UserFacingError("Safety check blocked this mint. " + "; ".join(report.blockers)[:300])
    job_id = await svc.db.add_automate_job(user_id, mint, amount)
    await reply(
        update,
        "<b>AUTOMATE ARMED</b>\n"
        + lead("Scanning P15 and P18. Nothing is bought until a trailing dip rebounds.")
        + "\n\n"
        + card(
            [
                f"Job #{job_id}",
                f"Mint: {esc(mint)}",
                f"Buy size: {sol(amount)}",
                "State: scanning",
            ]
        )
        + "\n\n"
        + info("/automate_list shows the live state. /automate_off stops it without selling."),
    )


@callback_guard
async def on_automate_callback(update, context) -> None:
    query = update.callback_query
    await best_effort_answer(query, "")
    if not await ensure_private(update):
        return
    data = query.data or ""
    if not data.startswith("am:stop:"):
        return
    try:
        job_id = int(data.removeprefix("am:stop:"))
    except ValueError:
        return
    svc = services(context)
    user_id = str(update.effective_user.id)
    stopped = await svc.db.cancel_automate_job(job_id, user_id)
    if not stopped:
        await reply(update, "<b>AUTOMATE</b>\n" + info("That job is already finished."))
        return
    await reply(
        update,
        f"<b>AUTOMATE</b>\nJob #{job_id} stopped. No sell was sent.",
    )


def _register() -> None:
    flows.register(
        "automate",
        [flows.Step("mint", PROMPT, flows.parse_mint)],
        _arm,
    )


_register()
