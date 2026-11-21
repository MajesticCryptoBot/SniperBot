from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from solders.pubkey import Pubkey

from sniper.accounting import spend_decision
from sniper.amounts import AmountError, lamports_to_sol, parse_sell_spec, raw_to_ui, sol_to_lamports
from sniper.errors import UserFacingError
from sniper.handlers import flows
from sniper.handlers.common import callback_guard, command, ensure_subscribed, reply, services
from sniper.keyboards import trade_keyboard
from sniper.solana_service import WSOL
from sniper.timeutil import start_of_utc_day
from sniper.trade_queue import PendingPreview
from sniper.ui import card, esc, info, sol

logger = logging.getLogger(__name__)
PREVIEW_TTL = 120


def _mint(value: str) -> str:
    try:
        return str(Pubkey.from_string(value.strip()))
    except Exception as exc:
        raise UserFacingError("The token mint is not a valid Solana address.") from exc


def _pop_expired(queue) -> None:
    now = time.monotonic()
    stale = [key for key, item in queue.previews.items() if now - item.created_monotonic > PREVIEW_TTL]
    for key in stale:
        queue.previews.pop(key, None)


async def _require_mainnet(svc) -> None:
    if not svc.cfg.is_mainnet:
        logger.warning("trading disabled: network is %s", svc.cfg.network)
        raise UserFacingError("Trading is unavailable on the current network configuration.")


@command("buy", needs_wallet=True)
async def buy(update, context) -> None:
    args = list(context.args or [])
    prefill = {}
    if len(args) >= 1:
        prefill["mint"] = args[0]
    if len(args) >= 2:
        prefill["amount"] = args[1]
    await flows.begin(update, context, "buy", prefill=prefill or None)


@command("sell", needs_wallet=True)
async def sell(update, context) -> None:
    args = list(context.args or [])
    prefill = {}
    if len(args) >= 1:
        prefill["mint"] = args[0]
    if len(args) >= 2:
        prefill["amount"] = args[1]
    await flows.begin(update, context, "sell", prefill=prefill or None)


@command("withdraw", needs_wallet=True)
async def withdraw(update, context) -> None:
    args = list(context.args or [])
    prefill = {}
    if len(args) >= 1:
        prefill["destination"] = args[0]
    if len(args) >= 2:
        prefill["amount"] = args[1]
    await flows.begin(update, context, "withdraw", prefill=prefill or None)


@command("scan")
async def scan(update, context) -> None:
    args = list(context.args or [])
    prefill = {"mint": args[0]} if args else None
    await flows.begin(update, context, "scan", prefill=prefill)


async def build_buy_preview(update, context, svc, mint: str, amount_sol: float, *, slippage_bps: int | None = None, priority_fee_lamports: int | None = None) -> None:
    await _require_mainnet(svc)
    user_id = str(update.effective_user.id)
    user = await svc.db.get_user(user_id)
    settings = await svc.db.get_settings(user_id)
    await _check_spend(svc, user, settings, amount_sol)
    lamports = sol_to_lamports(amount_sol)
    effective_slip = int(slippage_bps) if slippage_bps else int(settings["slippage_bps"])
    order = await svc.jupiter.order(
        input_mint=WSOL,
        output_mint=mint,
        amount=lamports,
        taker=user["wallet_address"],
        slippage_bps=effective_slip,
        priority_fee_lamports=priority_fee_lamports,
    )
    decimals = 0
    try:
        info = await svc.rpc.get_mint_info(mint)
        decimals = int(info["decimals"])
    except UserFacingError as exc:
        raise UserFacingError(f"Unable to retrieve token information for the buy quote: {esc(exc)}") from exc
    safety = await svc.safety.evaluate(mint, min_liquidity_sol=0)
    preview_id = uuid.uuid4().hex
    label = f"{sol(amount_sol)}"
    _store_preview(
        svc,
        PendingPreview(
            preview_id=preview_id,
            telegram_id=user_id,
            chat_id=update.effective_chat.id,
            side="buy",
            mint=mint,
            amount_sol=amount_sol,
            amount_in_raw=lamports,
            destination=None,
            label=label,
            created_monotonic=time.monotonic(),
            slippage_bps=slippage_bps,
            priority_fee_lamports=priority_fee_lamports,
        ),
    )
    high = amount_sol >= svc.cfg.high_value_sol
    slip_origin = " (account default)" if not slippage_bps else ""
    lines = [
        "<b>BUY ORDER REVIEW</b>",
        info("Nothing is signed yet."),
        "",
        card(
            [
                f"Spend: {sol(amount_sol)}",
                f"Mint: {esc(mint)}",
                f"Est. output: {raw_to_ui(order.out_amount, decimals):.6f} tokens",
                f"Price impact: {order.price_impact_pct:.2f}%",
                f"Slippage: {effective_slip / 100:.2f}%{slip_origin}",
                f"Priority fee: {priority_fee_lamports:,} lamports (this order)" if priority_fee_lamports else "Priority fee: Jupiter dynamic",
                f"Route: {esc(order.route)}",
            ]
        ),
        "",
        "<b>RISK</b>",
        safety.summary(),
    ]
    if high:
        lines.append("⚠️ HIGH VALUE. Confirm the amount and the asset.")
    lines.append(info("A fresh quote is taken at confirmation. Output may differ."))
    await reply(update, "\n".join(lines), trade_keyboard(preview_id, high, label))


async def build_sell_preview(update, context, svc, mint: str, spec_text: str, *, slippage_bps: int | None = None, priority_fee_lamports: int | None = None) -> None:
    await _require_mainnet(svc)
    user_id = str(update.effective_user.id)
    user = await svc.db.get_user(user_id)
    settings = await svc.db.get_settings(user_id)
    accounts = await svc.rpc.get_token_accounts(user["wallet_address"])
    match = [item for item in accounts if item["mint"] == mint]
    if not match:
        raise UserFacingError("No balance for this mint in the connected wallet.")
    balance_raw = sum(item["amount_raw"] for item in match)
    decimals = match[0]["decimals"]
    try:
        raw, label = parse_sell_spec(spec_text, balance_raw, decimals)
    except AmountError as exc:
        raise UserFacingError(str(exc)) from exc
    effective_slip = int(slippage_bps) if slippage_bps else int(settings["slippage_bps"])
    fee_on = svc.cfg.fee_active
    order = await svc.jupiter.order(
        input_mint=mint,
        output_mint=WSOL,
        amount=raw,
        taker=user["wallet_address"],
        slippage_bps=effective_slip,
        priority_fee_lamports=priority_fee_lamports,
        referral_account=svc.cfg.jupiter_referral_account if fee_on else None,
        referral_fee_bps=svc.cfg.bot_fee_bps if fee_on else None,
    )
    out_sol = order.out_amount / 1_000_000_000
    preview_id = uuid.uuid4().hex
    _store_preview(
        svc,
        PendingPreview(
            preview_id=preview_id,
            telegram_id=user_id,
            chat_id=update.effective_chat.id,
            side="sell",
            mint=mint,
            amount_sol=out_sol,
            amount_in_raw=raw,
            destination=None,
            label=label,
            created_monotonic=time.monotonic(),
            slippage_bps=slippage_bps,
            priority_fee_lamports=priority_fee_lamports,
        ),
    )
    high = out_sol >= svc.cfg.high_value_sol or spec_text.strip().endswith("%") and float(spec_text.strip()[:-1] or 0) >= 50
    slip_origin = " (account default)" if not slippage_bps else ""
    lines = [
        "<b>SELL ORDER REVIEW</b>",
        info("Nothing is signed yet."),
        "",
        card(
            [
                f"Mint: {esc(mint)}",
                f"Sell quantity: {esc(label)}",
                f"Wallet balance: {raw_to_ui(raw, decimals):.6f} / {raw_to_ui(balance_raw, decimals):.6f}",
                f"Est. proceeds: {sol(out_sol)}",
                *([f"Bot fee: {svc.cfg.bot_fee_bps / 100:.2f}% (included in proceeds)"] if fee_on else []),
                f"Price impact: {order.price_impact_pct:.2f}%",
                f"Slippage: {effective_slip / 100:.2f}%{slip_origin}",
                f"Priority fee: {priority_fee_lamports:,} lamports (this order)" if priority_fee_lamports else "Priority fee: Jupiter dynamic",
                f"Route: {esc(order.route)}",
            ]
        ),
        "",
        "<b>RISK</b>",
    ]
    if high:
        lines.append("⚠️ HIGH VALUE. Confirm the quantity and the proceeds.")
    lines.append(info("A fresh quote is taken at confirmation. Keep SOL for network fees."))
    await reply(update, "\n".join(lines), trade_keyboard(preview_id, high, label))


async def build_withdraw_preview(update, context, svc, destination: str, amount_sol: float) -> None:
    user_id = str(update.effective_user.id)
    user = await svc.db.get_user(user_id)
    settings = await svc.db.get_settings(user_id)
    if destination == user["wallet_address"]:
        raise UserFacingError("Destination is this wallet. No withdrawal was created.")
    await _check_spend(svc, user, settings, amount_sol)
    preview_id = uuid.uuid4().hex
    label = f"{sol(amount_sol)}"
    _store_preview(
        svc,
        PendingPreview(
            preview_id=preview_id,
            telegram_id=user_id,
            chat_id=update.effective_chat.id,
            side="withdraw",
            mint=WSOL,
            amount_sol=amount_sol,
            amount_in_raw=sol_to_lamports(amount_sol),
            destination=destination,
            label=label,
            created_monotonic=time.monotonic(),
        ),
    )
    balance = lamports_to_sol(await svc.rpc.get_balance_lamports(user["wallet_address"]))
    high = amount_sol >= svc.cfg.high_value_sol
    text = "\n".join(
        [
            "<b>WITHDRAWAL REVIEW</b>",
            info("Nothing is signed yet."),
            "",
            card(
                [
                    f"From: {esc(user['wallet_address'])}",
                    f"To: {esc(destination)}",
                    f"Amount: {sol(amount_sol)}",
                    f"Balance: {sol(balance)}",
                ]
            ),
            "",
            info("Check the destination. Network fees are extra."),
        ]
    )
    await reply(update, text, trade_keyboard(preview_id, high, label))


async def run_scan(update, svc, mint: str) -> None:
    report = await svc.safety.evaluate(mint, min_liquidity_sol=0)
    await reply(
        update,
        "\n".join(
            [
                "🛡️ <b>SAFETY REPORT</b>",
                "",
                card([f"Mint: {esc(mint)}", "", report.summary()]),
                "",
                info("No trade was created by this assessment."),
            ]
        ),
    )


@callback_guard
async def on_trade_callback(update, context) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if ":" not in data:
        return
    action, preview_id = data.split(":", 1)
    svc = services(context)
    _pop_expired(svc.queue)
    preview = svc.queue.previews.get(preview_id)
    if preview is None or preview.telegram_id != str(update.effective_user.id):
        await reply(update, "<b>QUOTE EXPIRED</b>\nThis confirmation is no longer valid. Open the command again.")
        return
    if action == "no":
        svc.queue.previews.pop(preview_id, None)
        await reply(update, "<b>ORDER CANCELLED</b>\nNothing was signed or submitted.")
        return
    if action != "ok":
        return
    if not await ensure_subscribed(update, context):
        return
    user = await svc.db.get_user(preview.telegram_id)
    settings = await svc.db.get_settings(preview.telegram_id)
    if preview.side in {"buy", "withdraw"}:
        await _check_spend(svc, user, settings, float(preview.amount_sol or 0))
    if preview.side in {"buy", "sell"}:
        await _require_mainnet(svc)
    svc.queue.previews.pop(preview_id, None)
    trade_id = await svc.queue.enqueue_preview(preview, source="manual")
    logger.info("queued %s trade=%s user=%s", preview.side, trade_id, preview.telegram_id)
    await reply(
        update,
        "\n".join(
            [
                "🕓 <b>ORDER QUEUED</b>",
                "",
                card(
                    [
                        f"Action: {preview.side.upper()}",
                        f"Size: {esc(preview.label)}",
                        f"Reference: {trade_id}",
                    ]
                ),
            ]
        )
        + "\n\n"
        + info("The worker is processing it. A result follows."),
    )


def _store_preview(svc, preview: PendingPreview) -> None:
    _pop_expired(svc.queue)
    svc.queue.previews[preview.preview_id] = preview


async def _check_spend(svc, user, settings, amount_sol: float) -> None:
    hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ok, reason = spend_decision(
        amount_sol=amount_sol,
        max_per_trade=float(settings["max_sol_per_trade"]),
        spent_today=await svc.db.spent_today(user["telegram_id"], start_of_utc_day()),
        daily_limit=float(settings["daily_spend_limit_sol"]),
        trades_last_hour=await svc.db.trades_since(user["telegram_id"], hour_ago),
        max_trades_per_hour=svc.cfg.max_trades_per_hour,
        user_paused=bool(user["trading_paused"]),
        global_paused=(await svc.db.get_state("global_pause")) == "1",
        absolute_max_sol=svc.cfg.absolute_max_sol,
    )
    if not ok:
        raise UserFacingError(reason)


# ---------------------------------------------------------------------------
# Conversational flow registration
# ---------------------------------------------------------------------------

BUY_MINT_PROMPT = "<b>BUY</b>\n\nSend the token mint to purchase."
BUY_AMOUNT_PROMPT = "<b>BUY AMOUNT</b>\n\nEnter the SOL amount to spend."
SELL_MINT_PROMPT = "<b>SELL</b>\n\nSend the token mint to sell."
SELL_AMOUNT_PROMPT = "<b>SELL SIZE</b>\n\nEnter a token amount (example: 1.5) or a percentage of the holding (example: 50%)."
WITHDRAW_ADDRESS_PROMPT = "<b>WITHDRAWAL</b>\n\nSend the Solana address that should receive the SOL."
WITHDRAW_AMOUNT_PROMPT = "<b>WITHDRAWAL AMOUNT</b>\n\nEnter the SOL amount to send."
SCAN_MINT_PROMPT = "🛡️ <b>SAFETY REPORT</b>\n\nSend the token mint to assess."


def _register_flows() -> None:
    flows.register(
        "buy",
        [
            flows.Step("mint", BUY_MINT_PROMPT, flows.parse_mint),
            flows.Step("amount", BUY_AMOUNT_PROMPT, flows.parse_sol_amount),
        ],
        lambda update, context, data: build_buy_preview(update, context, services(context), data["mint"], float(data["amount"])),
    )
    flows.register(
        "sell",
        [
            flows.Step("mint", SELL_MINT_PROMPT, flows.parse_mint),
            flows.Step("amount", SELL_AMOUNT_PROMPT, flows.parse_token_or_percent),
        ],
        lambda update, context, data: build_sell_preview(update, context, services(context), data["mint"], str(data["amount"])),
    )
    flows.register(
        "withdraw",
        [
            flows.Step("destination", WITHDRAW_ADDRESS_PROMPT, flows.parse_destination),
            flows.Step("amount", WITHDRAW_AMOUNT_PROMPT, flows.parse_sol_amount),
        ],
        lambda update, context, data: build_withdraw_preview(update, context, services(context), data["destination"], float(data["amount"])),
    )
    flows.register(
        "scan",
        [flows.Step("mint", SCAN_MINT_PROMPT, flows.parse_mint)],
        lambda update, context, data: run_scan(update, services(context), data["mint"]),
    )


_register_flows()
