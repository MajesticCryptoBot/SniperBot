"""Rich buy/sell panels with buttons: preset amounts, per-trade slippage
and gas, limit orders, and trailing stops.

The panels keep their state in context.user_data (PANEL_KEY) and edit the
panel message in place on every tap. Confirm always hands the trade to the
existing preview pipeline in trading.py, so spend caps, price-impact
limits, and the final Confirm/Cancel preview all still apply — the panel
only replaces the typing, never the safety checks.

Limit orders and trailing stops are stored in the trade_triggers table and
evaluated by the background TriggerMonitor (trigger_monitor.py); they
execute through the same trade queue as every other trade.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from sniper.amounts import AmountError, parse_positive_decimal, raw_to_ui
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
    tg_call,
)
from sniper.handlers.trading import build_buy_preview, build_sell_preview
from sniper.solana_service import WSOL
from sniper.ui import card, esc, info

logger = logging.getLogger(__name__)

PANEL_KEY = "trade_panel"
BUY_PRESETS = (0.01, 0.05, 0.1, 0.5, 1.0)
SELL_PCTS = (25, 50, 75, 100)
SLIP_PRESETS = (100, 300, 500, 1000, 1500, 3000)  # bps: 1% … 30%
FEE_PRESETS = (10_000, 50_000, 100_000, 500_000, 1_000_000)  # lamports
TRAIL_PRESETS = (10, 20, 30, 50)
MAX_HOLDINGS_BUTTONS = 8
PASTE_FLOWS = {"panel_buy_mint", "panel_sell_mint"}
PASTE_BUY_PROMPT = (
    "<b>BUY</b>\n\n"
    "Send the token mint you want to buy, or a supported token or pair link."
)
PASTE_SELL_PROMPT = (
    "<b>SELL</b>\n\n"
    "Send the token mint to sell. The wallet balance is checked before the panel opens."
)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_usd_text(value) -> str | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value != value or value <= 0:
        return None
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    if value >= 1:
        return f"${value:,.2f}"
    return f"${value:.8f}".rstrip("0")


def _fmt_amount(ui: float) -> str:
    if ui >= 1_000_000:
        return f"{ui / 1_000_000:.2f}M"
    if ui >= 1_000:
        return f"{ui:,.0f}"
    if ui >= 1:
        return f"{ui:,.2f}"
    return f"{ui:.6f}".rstrip("0").rstrip(".") or "0"


def _fee_label(lamports: int) -> str:
    if lamports >= 1_000_000:
        return f"{lamports / 1_000_000:g}M"
    return f"{lamports / 1000:g}k"


def _slip_line(state, settings) -> str:
    override = state.get("slip_bps")
    slip = int(override) if override else int(settings["slippage_bps"])
    origin = " (this order)" if override else " (account default)"
    return f"Slippage: {slip / 100:.2f}%{origin}"


def _fee_line(state, settings) -> str:
    override = state.get("fee_lamports")
    if override:
        return f"Priority fee: {int(override)} lamports (this trade)"
    return f"Priority fee: {int(settings['priority_fee_lamports'])} lamports (dynamic)"


# ---------------------------------------------------------------------------
# Panel state (context.user_data)
# ---------------------------------------------------------------------------

def _panel(context) -> dict | None:
    return context.user_data.get(PANEL_KEY)


def _set_panel(context, **fields) -> dict:
    state = context.user_data.setdefault(PANEL_KEY, {})
    state.update(fields)
    return state


def _close_panel(context) -> None:
    context.user_data.pop(PANEL_KEY, None)


def _cancel_paste_flow(context) -> None:
    """A closed panel must not keep swallowing replies meant for something else."""
    if flows.active_flow(context) in PASTE_FLOWS:
        flows.cancel(context)


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def _preset_rows(values, make, per_row: int) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for value in values:
        label, data = make(value)
        row.append(InlineKeyboardButton(label, callback_data=data))
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def _buy_keyboard(state, settings) -> InlineKeyboardMarkup:
    mint = state.get("mint") or ""
    if not mint:  # defensive — panels open with a mint; the ask happens before
        return InlineKeyboardMarkup([[InlineKeyboardButton("Close", callback_data="tp:close")]])
    amount = state.get("amount")
    mode = state.get("mode") or "main"
    if mode == "slip":
        rows = _preset_rows(SLIP_PRESETS, lambda v: (f"{v / 100:g}%", f"tp:slip:{v}:{mint}"), per_row=3)
        rows.append([InlineKeyboardButton("Back", callback_data=f"tp:menu:{mint}")])
    elif mode == "fee":
        rows = _preset_rows(FEE_PRESETS, lambda v: (f"{_fee_label(v)}", f"tp:fee:{v}:{mint}"), per_row=3)
        rows.append([InlineKeyboardButton("Back", callback_data=f"tp:menu:{mint}")])
    else:
        rows = _preset_rows(BUY_PRESETS, lambda v: (f"{v:g} SOL", f"tp:amt:{v}:{mint}"), per_row=3)
        rows.append([InlineKeyboardButton("Custom", callback_data=f"tp:custom:{mint}")])
        rows.append([
            InlineKeyboardButton(f"Slip {(state.get('slip_bps') or int(settings['slippage_bps'])) / 100:g}%", callback_data=f"tp:slipmenu:{mint}"),
            InlineKeyboardButton(f"Fee {_fee_label(int(state['fee_lamports'])) if state.get('fee_lamports') else 'dyn'}", callback_data=f"tp:feemenu:{mint}"),
        ])
        rows.append([
            InlineKeyboardButton("Limit", callback_data=f"tp:limit:{mint}"),
            InlineKeyboardButton("Close", callback_data=f"tp:close:{mint}"),
        ])
        confirm = "Confirm buy" + (f" · {float(amount):g} SOL" if amount else "")
        rows.append([InlineKeyboardButton(confirm, callback_data=f"tp:go:{mint}")])
    return InlineKeyboardMarkup(rows)


def _sell_keyboard(state, settings) -> InlineKeyboardMarkup:
    mint = state.get("mint") or ""
    mode = state.get("mode") or "main"
    if mode == "slip":
        rows = _preset_rows(SLIP_PRESETS, lambda v: (f"{v / 100:g}%", f"tp:slip:{v}:{mint}"), per_row=3)
        rows.append([InlineKeyboardButton("Back", callback_data=f"tp:menu:{mint}")])
    elif mode == "fee":
        rows = _preset_rows(FEE_PRESETS, lambda v: (f"{_fee_label(v)}", f"tp:fee:{v}:{mint}"), per_row=3)
        rows.append([InlineKeyboardButton("Back", callback_data=f"tp:menu:{mint}")])
    else:
        rows = _preset_rows(SELL_PCTS, lambda v: (f"{v}%", f"tp:pct:{v}:{mint}"), per_row=4)
        rows.append([
            InlineKeyboardButton("Custom", callback_data=f"tp:custom:{mint}"),
            InlineKeyboardButton("Limit", callback_data=f"tp:limit:{mint}"),
            InlineKeyboardButton("Trail", callback_data=f"tp:trail:{mint}"),
        ])
        rows.append([
            InlineKeyboardButton(f"Slip {(state.get('slip_bps') or int(settings['slippage_bps'])) / 100:g}%", callback_data=f"tp:slipmenu:{mint}"),
            InlineKeyboardButton(f"Fee {_fee_label(int(state['fee_lamports'])) if state.get('fee_lamports') else 'dyn'}", callback_data=f"tp:feemenu:{mint}"),
        ])
        pct = state.get("pct")
        amount = state.get("amount")
        confirm = "Confirm sell"
        if pct:
            confirm += f" · {int(pct)}%"
        elif amount:
            confirm += f" · {amount}"
        rows.append([
            InlineKeyboardButton(confirm, callback_data=f"tp:go:{mint}"),
            InlineKeyboardButton("Holdings", callback_data="tp:holdings"),
            InlineKeyboardButton("Close", callback_data=f"tp:close:{mint}"),
        ])
    return InlineKeyboardMarkup(rows)


def _holdings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Refresh", callback_data="tp:refreshsell"),
                InlineKeyboardButton("Paste mint", callback_data="tp:paste"),
                InlineKeyboardButton("Close", callback_data="tp:close"),
            ],
        ]
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

async def _edit(query, text: str, markup) -> None:
    try:
        await tg_call(query.edit_message_text, text[:3900], reply_markup=markup, disable_web_page_preview=True, parse_mode="HTML")
    except BadRequest as exc:
        # Rapid double-tap with unchanged content — Telegram rejects the edit.
        if "not modified" not in str(exc).lower():
            raise


async def _wallet_balance_sol(svc, user_id: str) -> float | None:
    try:
        user = await svc.db.get_user(user_id)
        return float(await svc.rpc.get_balance_lamports(user["wallet_address"])) / 1_000_000_000
    except Exception:
        logger.warning("could not read the balance for a trade panel")
        return None


def _fmt_price(value) -> str | None:
    """Price formatting that survives memecoin precision ($0.00000123)."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value != value or value <= 0:
        return None
    if value >= 1:
        return f"${value:,.2f}"
    if value >= 0.01:
        return f"${value:.4f}"
    return f"${value:.10f}".rstrip("0")


async def _safe_profile(svc, mint: str) -> dict | None:
    try:
        return await svc.prices.token_profile(mint)
    except Exception:
        return None


def _buy_text(state, settings, balance_sol, profile, mint) -> str:
    lines = [
        "🛒 <b>BUY</b>",
        info("Nothing is signed until you confirm the live quote."),
        "",
    ]
    asset = []
    if profile and (profile.get("name") or profile.get("symbol")):
        title = f"{esc(profile.get('name') or 'Unknown')} ({esc(profile.get('symbol')) if profile.get('symbol') else 'n/a'})"
        asset.append(title)
        metrics = []
        price = _fmt_usd_text(profile.get("price_usd"))
        cap = _fmt_usd_text(profile.get("market_cap_usd"))
        liq = _fmt_usd_text(profile.get("liquidity_usd"))
        if price:
            metrics.append(f"Price {price}")
        if cap:
            metrics.append(f"MCap {cap}")
        if liq:
            metrics.append(f"Liq {liq}")
        if metrics:
            asset.append(" · ".join(metrics))
    asset += ["Mint", esc(mint)]
    asset.append(f"Balance: {balance_sol:.6f} SOL" if balance_sol is not None else "Balance: unavailable")
    lines.append(card(asset))
    lines += ["", "<b>ORDER</b>"]
    amount = state.get("amount")
    lines.append(
        card(
            [
                f"Spend: {float(amount):.6f} SOL" if amount else "Spend: select below",
                _slip_line(state, settings),
                _fee_line(state, settings),
            ]
        )
    )
    mode = state.get("mode") or "main"
    if mode == "slip":
        foot = "Slippage applies to this order only."
    elif mode == "fee":
        foot = "Priority fee applies to this order only."
    else:
        foot = "Select an amount. Confirm requests a live quote. A limit order is available."
    lines += ["", info(foot)]
    return "\n".join(lines)


async def _buy_panel_content(svc, user_id: str, state: dict) -> tuple[str, InlineKeyboardMarkup]:
    settings = await svc.db.get_settings(user_id)
    balance_sol = await _wallet_balance_sol(svc, user_id)
    mint = state.get("mint")
    profile = await _safe_profile(svc, mint) if mint else None
    return _buy_text(state, settings, balance_sol, profile, mint or "not selected"), _buy_keyboard(state, settings)


async def open_buy_panel(update, context, svc, mint: str | None = None, *, query=None, preset: dict | None = None) -> None:
    user_id = str(update.effective_user.id)
    state = _set_panel(context, side="buy", mint=mint, mode="main", amount=None, pct=None, slip_bps=None, fee_lamports=None)
    if preset:
        state.update(preset)
    text, keyboard = await _buy_panel_content(svc, user_id, state)
    if query is not None:
        await _edit(query, text, keyboard)
    else:
        await reply(update, text, keyboard)


def _sell_token_text(state, settings, mint, holding, price, profile) -> str:
    title = "<b>SELL TERMINAL</b>"
    if profile and (profile.get("name") or profile.get("symbol")):
        title += f"  |  {esc(profile.get('name') or 'Unknown')} ({esc(profile.get('symbol')) if profile.get('symbol') else 'n/a'})"
    position = ["Mint", esc(mint), ""]
    if holding:
        ui = raw_to_ui(holding["raw"], holding["decimals"])
        value = ui * price if price else None
        position += ["Holding", f"{_fmt_amount(ui)} tokens"]
        if value is not None:
            position.append(f"Value: ${value:,.2f}")
    else:
        position += ["Holding", "No balance found"]
    if price:
        price_text = _fmt_usd_text(price)
        if price_text:
            position.append(f"Price: {price_text}")
    pct = state.get("pct")
    amount = state.get("amount")
    if pct:
        size = f"Sell size: {int(pct)}% of holding"
    elif amount:
        size = f"Sell size: {amount}"
    else:
        size = "Sell size: select below"
    return "\n".join(
        [
            title,
            info("Nothing is signed until you confirm the live quote."),
            "",
            card(position),
            "",
            "<b>ORDER</b>",
            card([size, _slip_line(state, settings), _fee_line(state, settings)]),
            "",
            info("Keep SOL for network fees. A trailing stop is available."),
        ]
    )


async def _sell_panel_content(svc, user_id: str, state: dict) -> tuple[str, InlineKeyboardMarkup]:
    settings = await svc.db.get_settings(user_id)
    mint = state.get("mint")
    user = await svc.db.get_user(user_id)
    holding = None
    price = None
    try:
        accounts = await svc.rpc.get_token_accounts(user["wallet_address"])
        match = [item for item in accounts if item["mint"] == mint]
        if match:
            holding = {"raw": sum(item["amount_raw"] for item in match), "decimals": match[0]["decimals"]}
        prices = await svc.prices.usd_prices([mint])
        price = prices.get(mint) or None
    except Exception:
        logger.warning("sell panel could not read live data")
    profile = await _safe_profile(svc, mint)
    return _sell_token_text(state, settings, mint, holding, price, profile), _sell_keyboard(state, settings)


async def open_sell_token_panel(update, context, svc, mint: str, *, query=None, preset: dict | None = None) -> None:
    user_id = str(update.effective_user.id)
    state = _set_panel(context, side="sell", mint=mint, mode="main", amount=None, pct=None, slip_bps=None, fee_lamports=None)
    if preset:
        state.update(preset)
    text, keyboard = await _sell_panel_content(svc, user_id, state)
    if query is not None:
        await _edit(query, text, keyboard)
    else:
        await reply(update, text, keyboard)


def _holdings_text(entries, total_value, hidden) -> str:
    lines = ["📈 <b>SELL · WALLET HOLDINGS</b>", info("Select a holding to open its sell panel."), ""]
    rows = []
    for index, entry in enumerate(entries, start=1):
        line = f"{index:02d}  {esc(entry['label'])}"
        if entry.get("price_text"):
            line += f"  ·  {entry['price_text']}"
        rows.append(line)
    if total_value is not None:
        rows.append(f"Displayed value: ${total_value:,.2f}")
    lines.append(card(rows))
    if hidden:
        lines.append(f"Other holdings: {hidden}. Use /sell with the mint to open one.")
    lines += ["", info("Refresh re-reads balances. Paste a mint to open an asset that is not listed.")]
    return "\n".join(lines)


async def open_sell_holdings(update, context, svc, *, query=None) -> None:
    user_id = str(update.effective_user.id)
    user = await svc.db.get_user(user_id)
    try:
        accounts = await svc.rpc.get_token_accounts(user["wallet_address"])
    except Exception:
        await reply(update, "<b>NETWORK</b>\nThe network did not respond. Try again shortly.")
        return
    holdings: dict[str, dict] = {}
    for item in accounts:
        slot = holdings.setdefault(item["mint"], {"raw": 0, "decimals": item["decimals"]})
        slot["raw"] += item["amount_raw"]
    holdings = {mint: slot for mint, slot in holdings.items() if slot["raw"] > 0}
    if not holdings:
        text = (
            "📈 <b>SELL · WALLET HOLDINGS</b>\n\n"
            "No SPL token balances in this wallet.\n\n"
            "Use /buy, or send a token mint to check one."
        )
        _close_panel(context)
        if query is not None:
            await _edit(query, text, None)
        else:
            await reply(update, text)
        flows.arm(context, "panel_sell_mint")
        return
    _set_panel(context, side="holdings")
    mints = list(holdings)
    prices: dict[str, float] = {}
    profiles: dict[str, dict] = {}
    try:
        prices = await svc.prices.usd_prices(mints)
    except Exception:
        prices = {}
    try:
        profiles = await svc.prices.token_profiles(mints)
    except Exception:
        profiles = {}
    ranked = []
    for mint, slot in holdings.items():
        ui = raw_to_ui(slot["raw"], slot["decimals"])
        price = prices.get(mint) or 0
        profile = profiles.get(mint) or {}
        symbol = profile.get("symbol") or f"{mint[:4]}…{mint[-4:]}"
        value = ui * price if price else None
        label = f"{symbol} · {_fmt_amount(ui)}"
        if value:
            label += f" (~${value:,.2f})"
        ranked.append({"mint": mint, "label": label, "value": value or 0.0, "price_text": _fmt_price(price)})
    ranked.sort(key=lambda item: item["value"], reverse=True)
    shown = ranked[:MAX_HOLDINGS_BUTTONS]
    hidden = max(0, len(ranked) - len(shown))
    total = sum(item["value"] for item in shown) if any(item["value"] for item in ranked) else None
    rows = []
    for entry in shown:
        rows.append([InlineKeyboardButton(entry["label"][:60], callback_data=f"tp:sell:{entry['mint']}")])
    rows.append(_holdings_keyboard().inline_keyboard[0])
    text = _holdings_text(shown, total, hidden)
    if query is not None:
        await _edit(query, text, InlineKeyboardMarkup(rows))
    else:
        await reply(update, text, InlineKeyboardMarkup(rows))


# ---------------------------------------------------------------------------
# Entry commands
# ---------------------------------------------------------------------------

@command("buy", needs_wallet=True)
async def buy_entry(update, context) -> None:
    args = list(context.args or [])
    if len(args) >= 2:
        await flows.begin(update, context, "buy", prefill={"mint": args[0], "amount": args[1]})
        return
    svc = services(context)
    mint = None
    if args:
        try:
            mint = flows.parse_mint(args[0])
        except flows.StepError as exc:
            raise UserFacingError(str(exc)) from None
    if mint:
        await open_buy_panel(update, context, svc, mint)
    else:
        # Ask for the token first — the panel then opens as a fresh message
        # right below the address the user sends.
        await flows.begin(update, context, "panel_buy_mint")


@command("sell", needs_wallet=True)
async def sell_entry(update, context) -> None:
    args = list(context.args or [])
    if len(args) >= 2:
        await flows.begin(update, context, "sell", prefill={"mint": args[0], "amount": args[1]})
        return
    svc = services(context)
    if args:
        try:
            mint = flows.parse_mint(args[0])
        except flows.StepError as exc:
            raise UserFacingError(str(exc)) from None
        await open_sell_token_panel(update, context, svc, mint)
        return
    await open_sell_holdings(update, context, svc)


# ---------------------------------------------------------------------------
# Panel callbacks (tp:…)
# ---------------------------------------------------------------------------

@callback_guard
async def on_trade_panel_callback(update, context) -> None:
    query = update.callback_query
    await best_effort_answer(query, "")
    if not await ensure_private(update):
        return
    parts = (query.data or "").split(":")
    if len(parts) < 2 or parts[0] != "tp":
        return
    action = parts[1]
    svc = services(context)
    user = update.effective_user
    if user and not svc.limiter.allow(f"cmd:{user.id}", svc.cfg.commands_per_30s, 30):
        await reply(update, "<b>PLEASE WAIT</b>\nToo many requests. Try again in a moment.")
        return
    if not await ensure_subscribed(update, context):
        return
    if action == "close":
        _cancel_paste_flow(context)
        _close_panel(context)
        await _edit(query, "<b>CLOSED</b>\nSend /buy or /sell to reopen.", None)
        return
    if action in {"holdings", "refreshsell"}:
        await open_sell_holdings(update, context, svc, query=query)
        return
    if action == "sell" and len(parts) >= 3 and parts[2]:
        await open_sell_token_panel(update, context, svc, parts[2], query=query)
        return
    if action == "paste":
        side = (_panel(context) or {}).get("side")
        flow = "panel_sell_mint" if side in {"sell", "holdings"} else "panel_buy_mint"
        await flows.begin(update, context, flow)
        return
    state = _panel(context)
    if not state:
        await reply(update, "<b>PANEL EXPIRED</b>\nSend /buy or /sell to open a new one.")
        return
    if action in {"amt", "pct", "slip", "fee"}:
        value_raw = parts[2] if len(parts) > 2 else ""
        mint = parts[3] if len(parts) > 3 else ""
        try:
            value = float(value_raw)
        except ValueError:
            return
        if mint:
            state["mint"] = mint
        if action == "amt":
            state["amount"] = value
            state["mode"] = "main"
        elif action == "pct":
            state["pct"] = int(value)
            state["amount"] = None
        elif action == "slip":
            state["slip_bps"] = int(value)
            state["mode"] = "main"
        else:
            state["fee_lamports"] = int(value)
            state["mode"] = "main"
    elif action == "menu":
        state["mode"] = "main"
    elif action == "slipmenu":
        state["mode"] = "slip"
    elif action == "feemenu":
        state["mode"] = "fee"
    elif action == "custom":
        if state.get("side") == "sell":
            await flows.begin(update, context, "panel_sell_amount")
        else:
            await flows.begin(update, context, "panel_buy_amount")
        return
    elif action == "limit":
        if state.get("side") == "sell":
            await flows.begin(update, context, "limit_sell")
        else:
            await flows.begin(update, context, "limit_buy")
        return
    elif action == "trail":
        await flows.begin(update, context, "trailing_stop")
        return
    elif action == "go":
        await _confirm_panel(update, context, query, svc, state)
        return
    else:
        return
    if state.get("side") == "sell":
        await _refresh_sell_panel(update, context, query, svc, state)
    else:
        await _refresh_buy_panel(update, context, query, svc, state)


async def _refresh_buy_panel(update, context, query, svc, state) -> None:
    user_id = str(update.effective_user.id)
    settings = await svc.db.get_settings(user_id)
    balance_sol = await _wallet_balance_sol(svc, user_id)
    mint = state.get("mint")
    profile = await _safe_profile(svc, mint) if mint else None
    text = _buy_text(state, settings, balance_sol, profile, mint or "not selected")
    await _edit(query, text, _buy_keyboard(state, settings))


async def _refresh_sell_panel(update, context, query, svc, state) -> None:
    user_id = str(update.effective_user.id)
    settings = await svc.db.get_settings(user_id)
    mint = state.get("mint")
    user = await svc.db.get_user(user_id)
    holding = None
    price = None
    try:
        accounts = await svc.rpc.get_token_accounts(user["wallet_address"])
        match = [item for item in accounts if item["mint"] == mint]
        if match:
            holding = {"raw": sum(item["amount_raw"] for item in match), "decimals": match[0]["decimals"]}
        prices = await svc.prices.usd_prices([mint])
        price = prices.get(mint) or None
    except Exception:
        logger.warning("sell panel refresh could not read live data")
    profile = await _safe_profile(svc, mint)
    text = _sell_token_text(state, settings, mint, holding, price, profile)
    await _edit(query, text, _sell_keyboard(state, settings))


async def _confirm_panel(update, context, query, svc, state) -> None:
    mint = state.get("mint")
    if not mint:
        await reply(update, "<b>NO ASSET</b>\nSend a mint, or open /sell and pick a holding.")
        return
    if state.get("side") == "buy":
        amount = state.get("amount")
        if not amount or float(amount) <= 0:
            await reply(update, "<b>NO AMOUNT</b>\nSelect a spend amount first.")
            return
        _close_panel(context)
        _cancel_paste_flow(context)
        await build_buy_preview(
            update,
            context,
            svc,
            mint,
            float(amount),
            slippage_bps=state.get("slip_bps"),
            priority_fee_lamports=state.get("fee_lamports"),
        )
        await _edit(query, "<b>QUOTE READY</b>\nBuy preview sent below. This panel is closed.", None)
        return
    pct = state.get("pct")
    amount = state.get("amount")
    if pct:
        spec = f"{int(pct)}%"
    elif amount:
        spec = str(amount)
    else:
        await reply(update, "<b>NO SIZE</b>\nSelect the sell size first.")
        return
    _close_panel(context)
    _cancel_paste_flow(context)
    await build_sell_preview(
        update,
        context,
        svc,
        mint,
        spec,
        slippage_bps=state.get("slip_bps"),
        priority_fee_lamports=state.get("fee_lamports"),
    )
    await _edit(query, "<b>QUOTE READY</b>\nSell preview sent below. This panel is closed.", None)


# ---------------------------------------------------------------------------
# /triggers — armed limit orders and trailing stops
# ---------------------------------------------------------------------------

def _trigger_line(row) -> str:
    mint = row["token_mint"]
    short = f"{mint[:6]}…{mint[-4:]}"
    kind = row["kind"]
    if kind == "limit_buy":
        return f"LIMIT BUY · {float(row['amount_sol']):g} SOL · at or below ${float(row['target_price_usd']):g} · {short}"
    if kind == "limit_sell":
        return f"LIMIT SELL · {float(row['sell_percent']):g}% · at or above ${float(row['target_price_usd']):g} · {short}"
    peak = float(row["peak_price_usd"] or 0)
    peak_text = f"${peak:g}" if peak else "initialising"
    return (f"TRAILING STOP · sell {float(row['sell_percent']):g}% · "
            f"{float(row['trail_distance_pct']):g}% pullback · peak {peak_text} · {short}")


def _triggers_text(rows) -> str:
    open_rows = [row for row in rows if row["status"] == "open"]
    lines = ["🕓 <b>ORDER MANAGEMENT</b>", info("Armed orders are watched automatically."), "", "<b>ARMED</b>"]
    if open_rows:
        armed = [f"#{row['id']}  {_trigger_line(row)}" for row in open_rows]
        lines.append(card(armed))
    else:
        lines.append(card(["No active orders.", "Limit buy is in /buy. Limit sell and trailing stop are in /sell."]))
    closed = [row for row in rows if row["status"] != "open"][:5]
    if closed:
        lines += ["", "<b>RECENT</b>"]
        labels = {"triggered": "TRIGGERED", "cancelled": "CANCELLED"}
        recent = []
        for row in closed:
            status = labels.get(row["status"], f"FAILED: {esc((row.get('trigger_error') or '')[:70])}")
            recent.append(f"#{row['id']}  {_trigger_line(row)}  ·  {status}")
        lines.append(card(recent))
    lines += ["", info("Filled orders appear in /orders.")]
    return "\n".join(lines)


def _triggers_keyboard(open_rows) -> InlineKeyboardMarkup:
    rows = []
    for row in open_rows[:10]:
        rid = row["id"]
        if row["kind"] == "trailing_stop":
            rows.append([
                InlineKeyboardButton(f"Cancel #{rid}", callback_data=f"tt:cancel:{rid}"),
                InlineKeyboardButton("Distance", callback_data=f"tt:distmenu:{rid}"),
            ])
        else:
            rows.append([InlineKeyboardButton(f"Cancel #{rid}", callback_data=f"tt:cancel:{rid}")])
    if not rows:
        rows.append([InlineKeyboardButton("Close", callback_data="tt:close")])
    return InlineKeyboardMarkup(rows)


@command("triggers", needs_wallet=True)
async def triggers_command(update, context) -> None:
    svc = services(context)
    rows = await svc.db.triggers_for(str(update.effective_user.id))
    await reply(update, _triggers_text(rows), _triggers_keyboard([row for row in rows if row["status"] == "open"]))


@callback_guard
async def on_trigger_callback(update, context) -> None:
    query = update.callback_query
    await best_effort_answer(query, "")
    if not await ensure_private(update):
        return
    parts = (query.data or "").split(":")
    if len(parts) < 2 or parts[0] != "tt":
        return
    action = parts[1]
    svc = services(context)
    user = update.effective_user
    if user and not svc.limiter.allow(f"cmd:{user.id}", svc.cfg.commands_per_30s, 30):
        await reply(update, "<b>PLEASE WAIT</b>\nTry again in a moment.")
        return
    if not await ensure_subscribed(update, context):
        return
    user_id = str(user.id)
    if action == "close":
        await query.edit_message_reply_markup(None)
        return
    if action == "cancel" and len(parts) == 3 and parts[2].isdigit():
        cancelled = await svc.db.cancel_trigger(int(parts[2]), user_id)
        await reply(update, "🕓 <b>ORDER CANCELLED</b>\nIt will not be submitted." if cancelled else "That order is no longer active.")
        return
    if action == "distmenu" and len(parts) == 3 and parts[2].isdigit():
        rid = int(parts[2])
        row = [InlineKeyboardButton(f"{p:g}%", callback_data=f"tt:dist:{rid}:{p}") for p in TRAIL_PRESETS]
        await reply(
            update,
            f"<b>TRAILING DISTANCE</b>\n\nOrder #{rid}\n\n" + info("Fires when price falls this far below the tracked high."),
            InlineKeyboardMarkup([row]),
        )
        return
    if action == "dist" and len(parts) == 4 and parts[2].isdigit():
        try:
            pct = float(parts[3])
        except ValueError:
            return
        if not 1 <= pct <= 90:
            await reply(update, "Trailing distance must be between 1% and 90%.")
            return
        updated = await svc.db.update_trigger_distance(int(parts[2]), user_id, pct)
        await reply(update, f"<b>DISTANCE UPDATED</b>\n\nPullback: {pct:g}%" if updated else "That order is no longer active.")
        return


# ---------------------------------------------------------------------------
# Panel input flows
# ---------------------------------------------------------------------------

def _parse_usd_price(text, update=None, context=None) -> float:
    value = (text or "").strip().lstrip("$").strip()
    try:
        price = float(parse_positive_decimal(value))
    except AmountError:
        raise flows.StepError("Enter a USD price. Example: 0.00042 or $0.42.") from None
    if not 0 < price < 1_000_000_000:
        raise flows.StepError("Price is outside the accepted range.")
    return price


def _parse_trail_pct(text, update=None, context=None) -> float:
    value = (text or "").strip().rstrip("%").strip()
    try:
        pct = float(parse_positive_decimal(value))
    except AmountError:
        raise flows.StepError("Enter a percentage. Example: 20 or 20%.") from None
    if not 1 <= pct <= 90:
        raise flows.StepError("Trailing distance must be between 1% and 90%.")
    return pct


def _parse_sell_percent(text, update=None, context=None) -> float:
    value = (text or "").strip().rstrip("%").strip()
    try:
        pct = float(parse_positive_decimal(value))
    except AmountError:
        raise flows.StepError("Enter a percentage of the holding. Example: 50%.") from None
    if not 0 < pct <= 100:
        raise flows.StepError("Percentage must be between 0 and 100.")
    return pct


PANEL_BUY_AMOUNT_PROMPT = "<b>BUY AMOUNT</b>\n\nEnter the SOL amount to spend. Example: 0.25."
PANEL_SELL_AMOUNT_PROMPT = "<b>SELL SIZE</b>\n\nEnter a percentage (example: 30%) or a token amount (example: 1.5)."
LIMIT_BUY_PRICE_PROMPT = (
    "<b>LIMIT BUY PRICE</b>\n\nEnter the USD target. Example: 0.00042.\n\n"
    + info("Fires when the price is at or below it.")
)
LIMIT_BUY_AMOUNT_PROMPT = "<b>LIMIT BUY SIZE</b>\n\nSOL to spend when the target is reached."
LIMIT_SELL_PRICE_PROMPT = (
    "<b>LIMIT SELL PRICE</b>\n\nEnter the USD target. Example: 0.001.\n\n"
    + info("Fires when the price is at or above it.")
)
LIMIT_SELL_PCT_PROMPT = "<b>LIMIT SELL SIZE</b>\n\nPercentage of the holding to sell. Example: 50%."
TRAIL_DISTANCE_PROMPT = (
    "<b>TRAILING STOP</b>\n\nPullback that sells. Example: 20 means 20% below the tracked high.\n\n"
    + info("The tracked high rises with price and does not fall before the order fires.")
)
TRAIL_PCT_PROMPT = "<b>TRAILING STOP SIZE</b>\n\nPercentage of the holding to sell. Example: 100%."


async def _require_panel_mint(update, context) -> str | None:
    state = _panel(context)
    mint = (state or {}).get("mint")
    if not mint:
        await reply(update, "<b>PANEL CLOSED</b>\nReopen /buy or /sell.")
        return None
    return mint


def _armed_reply(headline: str, detail: list[str], trigger_id: int) -> str:
    return "\n".join(
        [
            headline,
            "",
            *detail,
            f"Reference #{trigger_id}",
            "",
            info("Checked about every 15 seconds. Manual orders do not use the Autosnipe safety gate."),
        ]
    )


async def _panel_buy_amount_done(update, context, data) -> None:
    state = _panel(context)
    if not state or state.get("side") != "buy":
        await reply(update, "<b>PANEL CLOSED</b>\nSend /buy to reopen.")
        return
    await open_buy_panel(update, context, services(context), state.get("mint"), preset={"amount": float(data["amount"])})


async def _panel_sell_amount_done(update, context, data) -> None:
    state = _panel(context)
    if not state or state.get("side") != "sell":
        await reply(update, "<b>PANEL CLOSED</b>\nSend /sell to reopen.")
        return
    await open_sell_token_panel(update, context, services(context), state["mint"], preset={"amount": str(data["amount"])})


async def _panel_buy_mint_done(update, context, data) -> None:
    """A replied token address opens the buy panel right below it."""
    svc = services(context)
    mint = data["mint"]
    if mint == WSOL:
        flows.arm(context, "panel_buy_mint")
        await reply(update, "<b>NOT A TOKEN</b>\nThat is wrapped SOL, not a tradable token. Send the token mint to purchase.")
        return
    try:
        await svc.rpc.get_mint_info(mint)
    except UserFacingError as exc:
        flows.arm(context, "panel_buy_mint")
        reason = esc(str(exc)) if str(exc).startswith("The network") else "<b>NOT A TOKEN MINT</b>\nValid Solana address, but not a token mint."
        await reply(update, f"{reason}\n\n{PASTE_BUY_PROMPT}")
        return
    except Exception:
        logger.warning("mint check failed on a pasted buy address")
        flows.arm(context, "panel_buy_mint")
        await reply(update, f"<b>NETWORK</b>\nThe network did not respond. Please try again.\n\n{PASTE_BUY_PROMPT}")
        return
    await open_buy_panel(update, context, svc, mint)


async def _panel_sell_mint_done(update, context, data) -> None:
    """A replied token address opens its sell panel right below it — only when it is held."""
    svc = services(context)
    user_id = str(update.effective_user.id)
    mint = data["mint"]
    held = None
    try:
        user = await svc.db.get_user(user_id)
        accounts = await svc.rpc.get_token_accounts(user["wallet_address"])
        held = sum(int(item["amount_raw"]) for item in accounts if item["mint"] == mint)
    except Exception:
        logger.warning("sell paste flow could not read the holdings")
    if held == 0:
        flows.arm(context, "panel_sell_mint")
        await reply(update, "<b>NO BALANCE</b>\nNo balance for this token in your wallet. Send another mint, or buy it with /buy.")
        return
    await open_sell_token_panel(update, context, svc, mint)


async def _limit_buy_done(update, context, data) -> None:
    svc = services(context)
    user_id = str(update.effective_user.id)
    mint = await _require_panel_mint(update, context)
    if not mint:
        return
    count = await svc.db.count_triggers_for(user_id)
    if count >= svc.cfg.max_watch_per_user:
        raise UserFacingError(f"Order limit reached ({svc.cfg.max_watch_per_user}). Cancel one in /triggers first.")
    settings = await svc.db.get_settings(user_id)
    amount = float(data["amount"])
    if amount > float(settings["max_sol_per_trade"]):
        raise UserFacingError("Above your per-trade maximum. Lower the amount, or change it in /config.")
    price = float(data["price"])
    trigger_id = await svc.db.add_trade_trigger(
        telegram_id=user_id, kind="limit_buy", mint=mint, target_price_usd=price, amount_sol=amount
    )
    _close_panel(context)
    await reply(
        update,
        _armed_reply(
            "<b>LIMIT BUY ACTIVATED</b>",
            [f"Mint: {esc(mint)}", f"Trigger: price ≤ ${price:g}", f"Spend: {amount:.4f} SOL"],
            trigger_id,
        ),
    )


async def _limit_sell_done(update, context, data) -> None:
    svc = services(context)
    user_id = str(update.effective_user.id)
    mint = await _require_panel_mint(update, context)
    if not mint:
        return
    count = await svc.db.count_triggers_for(user_id)
    if count >= svc.cfg.max_watch_per_user:
        raise UserFacingError(f"Order limit reached ({svc.cfg.max_watch_per_user} active). Cancel one in /triggers first.")
    price = float(data["price"])
    percent = float(data["percent"])
    trigger_id = await svc.db.add_trade_trigger(
        telegram_id=user_id, kind="limit_sell", mint=mint, target_price_usd=price, sell_percent=percent
    )
    _close_panel(context)
    await reply(
        update,
        _armed_reply(
            "<b>LIMIT SELL ACTIVATED</b>",
            [f"Mint: {esc(mint)}", f"Trigger: price ≥ ${price:g}", f"Sell size: {percent:g}% of holding"],
            trigger_id,
        ),
    )


async def _trailing_stop_done(update, context, data) -> None:
    svc = services(context)
    user_id = str(update.effective_user.id)
    mint = await _require_panel_mint(update, context)
    if not mint:
        return
    count = await svc.db.count_triggers_for(user_id)
    if count >= svc.cfg.max_watch_per_user:
        raise UserFacingError(f"Order limit reached ({svc.cfg.max_watch_per_user} active). Cancel one in /triggers first.")
    distance = float(data["distance"])
    percent = float(data["percent"])
    trigger_id = await svc.db.add_trade_trigger(
        telegram_id=user_id, kind="trailing_stop", mint=mint, trail_distance_pct=distance, sell_percent=percent
    )
    _close_panel(context)
    await reply(
        update,
        _armed_reply(
            "<b>TRAILING STOP ACTIVATED</b>",
            [
                f"Mint: {esc(mint)}",
                f"Sell size: {percent:g}% on a {distance:g}% pullback from the tracked high",
            ],
            trigger_id,
        ),
    )


def _register_flows() -> None:
    flows.register(
        "panel_buy_amount",
        [flows.Step("amount", PANEL_BUY_AMOUNT_PROMPT, flows.parse_sol_amount)],
        _panel_buy_amount_done,
    )
    flows.register("panel_buy_mint", [flows.Step("mint", PASTE_BUY_PROMPT, flows.parse_mint)], _panel_buy_mint_done)
    flows.register("panel_sell_mint", [flows.Step("mint", PASTE_SELL_PROMPT, flows.parse_mint)], _panel_sell_mint_done)
    flows.register(
        "panel_sell_amount",
        [flows.Step("amount", PANEL_SELL_AMOUNT_PROMPT, flows.parse_token_or_percent)],
        _panel_sell_amount_done,
    )
    flows.register(
        "limit_buy",
        [
            flows.Step("price", LIMIT_BUY_PRICE_PROMPT, _parse_usd_price),
            flows.Step("amount", LIMIT_BUY_AMOUNT_PROMPT, flows.parse_sol_amount),
        ],
        _limit_buy_done,
    )
    flows.register(
        "limit_sell",
        [
            flows.Step("price", LIMIT_SELL_PRICE_PROMPT, _parse_usd_price),
            flows.Step("percent", LIMIT_SELL_PCT_PROMPT, _parse_sell_percent),
        ],
        _limit_sell_done,
    )
    flows.register(
        "trailing_stop",
        [
            flows.Step("distance", TRAIL_DISTANCE_PROMPT, _parse_trail_pct),
            flows.Step("percent", TRAIL_PCT_PROMPT, _parse_sell_percent),
        ],
        _trailing_stop_done,
    )


_register_flows()
