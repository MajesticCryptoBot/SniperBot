from __future__ import annotations

import logging

from sniper.amounts import AmountError
from sniper.errors import UserFacingError
from sniper.handlers import flows
from sniper.handlers.common import (
    best_effort_answer,
    callback_guard,
    command,
    ensure_private,
    ensure_subscribed,
    reply,
    services,
)
from sniper.keyboards import autosnipe_keyboard, config_keyboard
from sniper.settings_logic import SETTING_NAMES, validate_setting
from sniper.ui import card, dot, esc, info, lead, onoff, sol
from sniper.watchlist_monitor import LIVE_TOKEN_REFUSAL, watchlist_add_rejection

logger = logging.getLogger(__name__)


def _format_settings(svc, settings) -> str:
    return "\n".join(
        [
            "⚙️ <b>TRADING CONFIGURATION</b>",
            "",
            "<b>EXECUTION</b>",
            card(
                [
                    f"Slippage: {int(settings['slippage_bps']) / 100:.2f}%",
                    f"Priority fee: {int(settings['priority_fee_lamports']):,} lamports",
                    f"Max per trade: {sol(settings['max_sol_per_trade'])}",
                    f"Daily limit: {sol(settings['daily_spend_limit_sol'])}",
                    f"Impact cap: {float(settings['max_price_impact_pct']):.2f}%",
                ]
            ),
            "",
            "<b>AUTOMATION</b>",
            card(
                [
                    f"Autosnipe: {dot(settings['autosnipe_enabled'])} {onoff(settings['autosnipe_enabled'])}",
                    f"Auto-buy: {dot(settings.get('auto_buy_enabled'))} {onoff(settings.get('auto_buy_enabled'))}",
                    f"TP/SL: {dot(settings.get('tp_sl_enabled'))} {onoff(settings.get('tp_sl_enabled'))}",
                ]
            ),
            "",
            info("Tap a setting below to change it."),
        ]
    )


@command("config", needs_wallet=True)
async def config_command(update, context) -> None:
    svc = services(context)
    user_id = str(update.effective_user.id)
    settings = await svc.db.get_settings(user_id)
    args = list(context.args or [])
    if not args:
        await reply(update, _format_settings(svc, settings), config_keyboard())
        return
    if len(args) == 1:
        await flows.begin(update, context, "config", prefill={"name": args[0]})
        return
    await flows.begin(update, context, "config", prefill={"name": args[0], "value": args[1]})


SETTING_LABELS = {
    "slippage": "Slippage",
    "priority_fee": "Priority fee",
    "max_sol": "Max per trade",
    "daily_limit": "Daily limit",
    "snipe_amount": "Snipe amount",
    "snipe_impact": "Snipe impact",
    "impact": "Impact cap",
    "auto_buy_amount": "Auto-buy size",
    "take_profit": "Take-profit",
    "stop_loss": "Stop-loss",
}


def _setting_value_text(column: str, value) -> str:
    """The stored value, formatted the way the panel prints it."""
    if column == "slippage_bps":
        return f"{int(value) / 100:.2f}%"
    if column == "priority_fee_lamports":
        return f"{int(value):,} lamports"
    if column in {"max_sol_per_trade", "daily_spend_limit_sol", "autosnipe_amount_sol", "auto_buy_amount_sol"}:
        return sol(value)
    if column == "take_profit_pct":
        return f"+{float(value):.2f}%"
    if column in {"stop_loss_pct", "autosnipe_max_price_impact_pct", "max_price_impact_pct"}:
        return f"{float(value):.2f}%"
    return esc(value)


async def apply_config(update, context, svc, name: str, raw_value: str) -> None:
    user_id = str(update.effective_user.id)
    settings = await svc.db.get_settings(user_id)
    try:
        column, value = validate_setting(name, raw_value, absolute_max_sol=svc.cfg.absolute_max_sol)
    except AmountError as exc:
        raise UserFacingError(str(exc)) from exc
    if column == "autosnipe_amount_sol" and float(value) > float(settings["max_sol_per_trade"]):
        raise UserFacingError("Snipe amount is above the per-trade maximum. Raise max per trade, or lower the amount.")
    await svc.db.update_setting(user_id, column, value)
    settings = await svc.db.get_settings(user_id)
    label = SETTING_LABELS.get(name.strip().lower(), name)
    await reply(
        update,
        f"✅ <b>{esc(label)}</b> updated to <b>{_setting_value_text(column, value)}</b>\n\n"
        + _format_settings(svc, settings),
        config_keyboard(),
    )


@callback_guard
async def on_config_callback(update, context) -> None:
    query = update.callback_query
    await best_effort_answer(query, "")
    if not await ensure_private(update):
        return
    data = query.data or ""
    if not data.startswith("cfg:"):
        return
    name = data[len("cfg:"):].strip().lower()
    svc = services(context)
    user = update.effective_user
    if user and not svc.limiter.allow(f"cmd:{user.id}", svc.cfg.commands_per_30s, 30):
        await reply(update, "<b>PLEASE WAIT</b>\nToo many requests. Try again in a moment.")
        return
    if not await ensure_subscribed(update, context):
        return
    if name == "cancel":
        flow = flows.cancel(context)
        await reply(update, "<b>CANCELLED</b>\nInput flow cancelled." if flow else "No active input flow.")
        return
    if name not in SETTING_NAMES:
        return
    await flows.begin(update, context, "config", prefill={"name": name})


# ---------------------------------------------------------------------------
# Autosnipe
# ---------------------------------------------------------------------------

AUTOSNIPE_MINT_PROMPT = (
    "🎯 <b>ADD TO AUTOSNIPE</b>\n\nSend the token mint address.\n\n"
    + info("A buy fires only after a live market appears and the checks pass. Already-tradeable tokens are refused.")
)
AUTOSNIPE_AMOUNT_PROMPT = "<b>ENTRY AMOUNT</b>\n\nSOL for this watchlist entry. Example: 0.25."



@command("autosnipe", needs_wallet=True)
async def autosnipe(update, context) -> None:
    svc = services(context)
    user_id = str(update.effective_user.id)
    args = list(context.args or [])
    if args:
        try:
            mint = flows.parse_mint(args[0])
        except flows.StepError:
            mint = ""  # invalid format: let the flow ask for it conversationally
        if mint:
            rejection = await watchlist_add_rejection(svc.prices, mint)
            if rejection:
                await reply(update, rejection)
                return
        await flows.begin(update, context, "autosnipe_add", prefill={"mint": args[0]})
        return
    settings = await svc.db.get_settings(user_id)
    entries = await svc.db.watchlist_for(user_id)
    await reply(
        update,
        _autosnipe_overview(settings, entries),
        autosnipe_keyboard(
            entries,
            bool(settings["autosnipe_enabled"]),
            bool(settings["auto_buy_enabled"]),
            bool(settings["tp_sl_enabled"]),
        ),
    )


def _autosnipe_overview(settings, entries: list[dict]) -> str:
    lines = ["🎯 <b>AUTOSNIPE</b>", lead("Watchlist only. No market scan."), "", "<b>WATCHLIST</b>"]
    status_label = {"active": "waiting", "triggered": "queued", "aborted": "blocked"}
    rows = []
    if entries:
        for index, entry in enumerate(entries, start=1):
            status = status_label.get(entry["status"], str(entry["status"]))
            line = f"{index:02d}  {entry['token_mint'][:8]}… · {sol(entry['amount_sol'])} · {status}"
            if entry["status"] == "aborted" and entry.get("trigger_error"):
                line += f"\n{esc(entry['trigger_error'][:100])}"
            rows.append(line)
    else:
        rows.append("Empty. Add a mint below.")
    lines.append(card(rows))
    switches = [
        f"🎯 Autosnipe: {dot(settings['autosnipe_enabled'])} {onoff(settings['autosnipe_enabled'])}",
        f"Impact cap: {float(settings['autosnipe_max_price_impact_pct']):.2f}%",
        f"{dot(settings.get('auto_buy_enabled'))} Auto-buy on paste: {onoff(settings.get('auto_buy_enabled'))}",
    ]
    if settings.get("auto_buy_enabled"):
        switches.append(f"Amount: {sol(settings.get('auto_buy_amount_sol') or 0)}")
    switches.append(f"{dot(settings.get('tp_sl_enabled'))} TP/SL auto-sell: {onoff(settings.get('tp_sl_enabled'))}")
    if settings.get("tp_sl_enabled"):
        switches.append(f"TP +{float(settings.get('take_profit_pct') or 0):.1f}% · SL {float(settings.get('stop_loss_pct') or 0):.1f}%")
    lines += ["", "<b>SWITCHES</b>", card(switches), "", info("/stop pauses new trading.")]
    return "\n".join(lines)


async def _parse_watch_mint(text, update, context) -> str:
    """Autosnipe mint parser: format first, then the already-live gate.

    Runs at the mint step, before the amount is asked, so a live token is
    refused immediately instead of after the whole flow. A live token is
    never addable, so that refusal also closes the dialog — the next paste
    reaches the normal chat flow (auto-buy / token panel), as the refusal
    itself suggests. An unverifiable market is a transient problem: the
    dialog stays open so the user can simply send the mint again.
    """
    mint = flows.parse_mint(text, update, context)
    svc = services(context)
    rejection = await watchlist_add_rejection(svc.prices, mint)
    if rejection == LIVE_TOKEN_REFUSAL:
        raise flows.StepAbortError(rejection)
    if rejection:
        raise flows.StepError(rejection)
    return mint


async def add_to_watchlist(update, context, svc, mint: str, amount_sol: float) -> None:
    user_id = str(update.effective_user.id)
    user = await svc.db.get_user(user_id)
    if user["trading_paused"]:
        raise UserFacingError("Trading is paused. Use /resume before enabling automation.")
    if not svc.cfg.is_mainnet:
        logger.warning("watchlist add refused: network is %s", svc.cfg.network)
        raise UserFacingError("🎯 Autosnipe is unavailable on the current network configuration.")
    settings = await svc.db.get_settings(user_id)
    if amount_sol > float(settings["max_sol_per_trade"]):
        raise UserFacingError("Amount is above your per-trade maximum.")
    count = await svc.db.count_watch_for(user_id)
    if count >= svc.cfg.max_watch_per_user:
        raise UserFacingError(f"Watchlist is full ({svc.cfg.max_watch_per_user}). Remove an entry first.")
    # The authoritative gate: autosnipe is for tokens with no market yet.
    rejection = await watchlist_add_rejection(svc.prices, mint)
    if rejection:
        raise UserFacingError(rejection)
    watch_id, action = await svc.db.add_watch_entry(user_id, mint, amount_sol)
    verb = {
        "added": "<b>WATCHLIST ENTRY ADDED</b>",
        "updated": "<b>WATCHLIST ENTRY UPDATED</b>",
        "reactivated": "<b>WATCHLIST ENTRY REACTIVATED</b>",
    }[action]
    switch_note = ""
    if int(settings["autosnipe_enabled"]) == 0:
        await svc.db.update_setting(user_id, "autosnipe_enabled", 1)
        switch_note = "\n🎯 Autosnipe is now on."
    await reply(
        update,
        "\n".join(
            [
                verb,
                "",
                f"Mint: {esc(mint)}",
                f"Amount: {sol(amount_sol)}",
                f"Reference: {watch_id}",
                switch_note,
            ]
        ).strip(),
    )


async def toggle_autosnipe(update, context, svc) -> None:
    user_id = str(update.effective_user.id)
    user = await svc.db.get_user(user_id)
    settings = await svc.db.get_settings(user_id)
    if settings["autosnipe_enabled"]:
        await svc.db.update_setting(user_id, "autosnipe_enabled", 0)
        await reply(update, "🔴 🎯 <b>AUTOSNIPE OFF</b>\nWatchlist entries will not buy.")
        return
    if user["trading_paused"]:
        raise UserFacingError("Trading is paused for this wallet. Use /resume before enabling automation.")
    if not svc.cfg.is_mainnet:
        logger.warning("autosnipe enable refused: network is %s", svc.cfg.network)
        raise UserFacingError("🎯 Autosnipe is unavailable on the current network configuration.")
    await svc.db.update_setting(user_id, "autosnipe_enabled", 1)
    count = await svc.db.count_watch_for(user_id)
    note = "The watchlist is empty. Add a token before expecting an entry." if count == 0 else f"Watching {count} {'entry' if count == 1 else 'entries'}."
    await reply(update, f"🟢 🎯 <b>AUTOSNIPE ON</b>\n\n{esc(note)}")


@callback_guard
async def on_snipe_callback(update, context) -> None:
    query = update.callback_query
    await query.answer()
    if not await ensure_private(update):
        return
    data = query.data or ""
    if not data.startswith("snipe:"):
        return
    parts = data.split(":")
    svc = services(context)
    user = update.effective_user
    if user and not svc.limiter.allow(f"cmd:{user.id}", svc.cfg.commands_per_30s, 30):
        await reply(update, "<b>PLEASE WAIT</b>\nTry again in a moment.")
        return
    if not await ensure_subscribed(update, context):
        return
    user_id = str(user.id)
    if parts[1] == "toggle":
        await toggle_autosnipe(update, context, svc)
        return
    if parts[1] == "autobuy":
        await toggle_auto_buy(update, context, svc)
        return
    if parts[1] == "tpsl":
        await toggle_tp_sl(update, context, svc)
        return
    if parts[1] == "add":
        await flows.begin(update, context, "autosnipe_add")
        return
    if parts[1] == "remove" and len(parts) == 3 and parts[2].isdigit():
        watch_id = int(parts[2])
        entries = await svc.db.watchlist_for(user_id)
        status = next((e["status"] for e in entries if e["id"] == watch_id), None)
        removed = await svc.db.remove_watch_entry(watch_id, user_id)
        note = (
            "\nAn already-queued buy is not cancelled by removing the entry."
            if status == "triggered"
            else ""
        )
        await reply(
            update,
            ("<b>REMOVED</b>\nWatchlist entry removed." + note) if removed else "That entry is no longer active.",
        )
        return


async def toggle_auto_buy(update, context, svc) -> None:
    user_id = str(update.effective_user.id)
    user = await svc.db.get_user(user_id)
    settings = await svc.db.get_settings(user_id)
    if settings.get("auto_buy_enabled"):
        await svc.db.update_setting(user_id, "auto_buy_enabled", 0)
        await reply(update, "🔴 <b>AUTO-BUY OFF</b>\nA pasted token opens its panel.")
        return
    if user["trading_paused"]:
        raise UserFacingError("Trading is paused for this wallet. Use /resume before enabling automation.")
    if not svc.cfg.is_mainnet:
        logger.warning("auto-buy enable refused: network is %s", svc.cfg.network)
        raise UserFacingError("Auto-buy is unavailable on the current network configuration.")
    await svc.db.update_setting(user_id, "auto_buy_enabled", 1)
    await reply(
        update,
        "🟢 <b>AUTO-BUY ON</b>\n\n"
        f"Amount: {sol(settings.get('auto_buy_amount_sol') or 0)}\n\n"
        + info("A pasted token with a live market is scanned, then bought. A flagged token is not bought."),
    )


async def toggle_tp_sl(update, context, svc) -> None:
    user_id = str(update.effective_user.id)
    user = await svc.db.get_user(user_id)
    settings = await svc.db.get_settings(user_id)
    if settings.get("tp_sl_enabled"):
        await svc.db.update_setting(user_id, "tp_sl_enabled", 0)
        await reply(update, "🔴 <b>TP/SL OFF</b>\nAutomatic exits are paused.")
        return
    if user["trading_paused"]:
        raise UserFacingError("Trading is paused for this wallet. Use /resume before enabling automation.")
    if not svc.cfg.is_mainnet:
        logger.warning("tp/sl enable refused: network is %s", svc.cfg.network)
        raise UserFacingError("TP/SL auto-sell is unavailable on the current network configuration.")
    await svc.db.update_setting(user_id, "tp_sl_enabled", 1)
    await reply(
        update,
        "🟢 <b>TP/SL ON</b>\n\n"
        f"Take-profit: +{float(settings.get('take_profit_pct') or 0):.1f}%\n"
        f"Stop-loss: {float(settings.get('stop_loss_pct') or 0):.1f}%\n\n"
        + info("The full eligible position is sold when a level is reached."),
    )


@command("stop", needs_wallet=True)
async def stop(update, context) -> None:
    svc = services(context)
    await svc.db.set_paused(str(update.effective_user.id), True)
    await reply(
        update,
        "<b>TRADING PAUSED</b>\n\n"
        "New buys, sells, withdrawals, and automation are blocked.\n\n"
        + info("A transaction already submitted may still settle."),
    )


@command("resume", needs_wallet=True)
async def resume(update, context) -> None:
    svc = services(context)
    await svc.db.set_paused(str(update.effective_user.id), False)
    await reply(
        update,
        "<b>TRADING RESUMED</b>\n\n"
        + info("🎯 Autosnipe and auto-buy keep their previous state. Review /autosnipe."),
    )


def _register_flows() -> None:
    flows.register(
        "config",
        [
            flows.Step(
                "name",
                lambda data: f"<b>CONFIGURATION</b>\n\nWhich setting?\n\n{', '.join(SETTING_NAMES)}",
                flows.parse_setting_name,
            ),
            flows.Step(
                "value",
                lambda data: f"<b>UPDATE SETTING</b>\n\nSend the new value for “{esc(data['name'])}”.\n\n{info('Send /cancel to keep the current value.')}",
                None,
            ),
        ],
        lambda update, context, data: apply_config(update, context, services(context), data["name"], str(data["value"])),
    )
    flows.register(
        "autosnipe_add",
        [
            flows.Step("mint", AUTOSNIPE_MINT_PROMPT, _parse_watch_mint),
            flows.Step("amount", AUTOSNIPE_AMOUNT_PROMPT, flows.parse_sol_amount),
        ],
        lambda update, context, data: add_to_watchlist(update, context, services(context), data["mint"], float(data["amount"])),
    )


_register_flows()
