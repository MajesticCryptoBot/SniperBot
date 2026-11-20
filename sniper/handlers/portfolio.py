from __future__ import annotations

from sniper.accounting import portfolio_totals
from sniper.amounts import lamports_to_sol
from sniper.errors import UserFacingError
from sniper.handlers.common import command, reply, services
from sniper.solana_service import WSOL
from sniper.timeutil import parse_utc
from sniper.ui import card, esc, info, mono

_SIDE_TITLE = {"buy": "BUY", "sell": "SELL", "withdraw": "WITHDRAW"}
_STATUS_TITLE = {
    "confirmed": "CONFIRMED",
    "failed": "FAILED",
    "submitted": "SUBMITTED",
    "pending": "PENDING",
    "processing": "PROCESSING",
}
_ORDERS_BUDGET = 3600


def _fmt_time(value: str) -> str:
    try:
        return parse_utc(value).strftime("%d %b %Y · %H:%M UTC")
    except Exception:
        return value


def format_trade_row(index: int, row: dict) -> str:
    status = str(row.get("status") or "unknown")
    side = _SIDE_TITLE.get(row.get("side"), str(row.get("side")).upper())
    amount = row.get("amount_sol")
    amount_text = f"{float(amount):.4f} SOL" if amount is not None else "n/a"
    lines = [f"{index:02d}  {side}  |  {status.upper()}  |  {amount_text}"]
    tags = []
    if row.get("source") == "autosnipe":
        tags.append("AUTOSNIPE")
    tags.append(_fmt_time(row.get("created_at") or ""))
    lines.append("     " + " · ".join(tags))
    if row.get("destination"):
        lines.append(f"     Destination: {esc(row['destination'])}")
    if row.get("token_mint") and row["side"] != "withdraw":
        lines.append(f"     Token: {esc(row['token_mint'])}")
    pnl = row.get("realized_pnl_sol")
    if pnl is not None:
        lines.append(f"     📈 P&L: {float(pnl):+.4f} SOL")
    if row.get("signature"):
        lines.append(f"     Signature: {esc(row['signature'])}")
    if row.get("error"):
        lines.append(f"     Details: {esc(row['error'])}")
    return "\n".join(lines)


@command("summary", needs_wallet=True)
async def summary(update, context) -> None:
    svc = services(context)
    user = await svc.db.get_user(str(update.effective_user.id))
    try:
        lamports = await svc.rpc.get_balance_lamports(user["wallet_address"])
        accounts = await svc.rpc.get_token_accounts(user["wallet_address"])
    except UserFacingError as exc:
        raise UserFacingError(f"Unable to retrieve wallet balances. {esc(exc)}") from exc
    mints = [item["mint"] for item in accounts]
    prices = await svc.prices.usd_prices(mints)
    sol_usd = float(prices.get(WSOL) or 0)
    holdings = []
    sol_balance = lamports_to_sol(lamports)
    lines = ["<b>PORTFOLIO</b>", info("Balances from the wallet. Values are indicative."), ""]
    lines.append(card(["Wallet", mono(user["wallet_address"])]))
    lines.append("")
    if sol_usd <= 0:
        lines.append(f"SOL: {sol_balance:.6f}")
    else:
        lines.append(f"SOL: {sol_balance:.6f} (${sol_balance * sol_usd:,.2f})")
    positions = {row["token_mint"]: row for row in await svc.db.list_positions(user["telegram_id"])}
    if not accounts:
        lines += ["", "<b>HOLDINGS</b>", "None."]
    else:
        lines += ["", "<b>HOLDINGS</b>"]
    displayed = 0
    omitted = 0
    for item in accounts[:25]:
        price = prices.get(item["mint"])
        usd = (item["ui_amount"] * price) if price is not None else None
        if price is not None:
            holdings.append((item["ui_amount"], price))
        cost = positions.get(item["mint"])
        cost_text = ""
        if cost and sol_usd > 0 and usd is not None:
            cost_usd = float(cost["cost_basis_sol"]) * sol_usd
            pnl_usd = usd - cost_usd
            cost_text = f"\nCost ${cost_usd:,.2f} · PnL ${pnl_usd:+,.2f}"
        elif cost:
            cost_text = f"\nCost {float(cost['cost_basis_sol']):.4f} SOL"
        value = f"${usd:,.2f}" if usd is not None else "n/a"
        block = card([
            f"{item['mint'][:8]}…{item['mint'][-4:]}",
            f"Balance {item['ui_amount']:.6f} · Value {esc(value)}{cost_text}",
        ])
        if sum(len(line) + 1 for line in lines) + len(block) <= 3100:
            lines.append(block)
            displayed += 1
        else:
            omitted += 1
    omitted += max(0, len(accounts) - 25)
    if omitted:
        lines.append(f"+{omitted} more not shown")
    totals = portfolio_totals(sol_balance, sol_usd, holdings)
    lines += ["", "<b>TOTAL VALUE</b>"]
    if sol_usd <= 0:
        lines.append("USD value is unavailable. Balances are shown above.")
    else:
        lines += [
            f"Total ${totals['total_usd']:,.2f}",
            f"SOL ${totals['sol_value_usd']:,.2f} · Tokens ${totals['tokens_usd']:,.2f}",
            "",
            info("Indicative only. Depends on price feeds and liquidity."),
        ]
    await reply(update, "\n".join(lines))


def _render_orders(rows: list[dict]) -> str:
    lines = [f"🧾 <b>TRADE HISTORY</b> · LATEST {len(rows)}", ""]
    used = len(lines[0]) + len(lines[1]) + 2
    shown = 0
    for index, row in enumerate(rows, start=1):
        block = format_trade_row(index, row)
        if used + len(block) > _ORDERS_BUDGET and shown:
            lines.append(f"+{len(rows) - shown} older records omitted (newest first).")
            break
        lines += [block, ""]
        used += len(block) + 2
        shown += 1
    lines.append(info("Signatures can be checked on any Solana explorer."))
    return "\n".join(lines)


@command("orders", needs_wallet=True)
async def orders(update, context) -> None:
    svc = services(context)
    rows = await svc.db.recent_trades(str(update.effective_user.id), limit=10)
    if not rows:
        await reply(update, "🧾 <b>TRADE HISTORY</b>\n\nNo trades or withdrawals yet.")
        return
    await reply(update, _render_orders(rows))
