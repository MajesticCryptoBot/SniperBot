"""Main-menu navigation.

Every menu button reuses the existing command handler, so rate limits,
membership checks and wallet checks behave exactly as they do for the
typed command. Nothing here touches trading logic.
"""

from __future__ import annotations

from sniper.handlers.common import best_effort_answer, callback_guard, ensure_private, reply
from sniper.keyboards import main_menu_keyboard
from sniper.texts import MENU


def _targets() -> dict:
    # Imported lazily: these modules import keyboards, which import nothing from here.
    from sniper.handlers.portfolio import orders, summary
    from sniper.handlers.settings import autosnipe, config_command
    from sniper.handlers.start import help_command
    from sniper.handlers.trade_panel import buy_entry, sell_entry, triggers_command
    from sniper.handlers.wallet import wallet

    return {
        "buy": buy_entry,
        "sell": sell_entry,
        "wallet": wallet,
        "portfolio": summary,
        "autosnipe": autosnipe,
        "triggers": triggers_command,
        "orders": orders,
        "config": config_command,
        "help": help_command,
    }


@callback_guard
async def on_menu_callback(update, context) -> None:
    query = update.callback_query
    await best_effort_answer(query, "")
    if not await ensure_private(update):
        return
    action = (query.data or "").removeprefix("menu:")
    if action == "home":
        await reply(update, MENU, main_menu_keyboard())
        return
    target = _targets().get(action)
    if target is not None:
        await target(update, context)
