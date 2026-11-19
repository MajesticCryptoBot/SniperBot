from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def join_keyboard(cfg) -> InlineKeyboardMarkup:
    rows = []
    for channel in cfg.channels():
        rows.append([InlineKeyboardButton(f"Open {channel}", url=cfg.channel_link(channel))])
    rows.append([InlineKeyboardButton("✅ I've joined · Verify access", callback_data="check_sub")])
    return InlineKeyboardMarkup(rows)


def trade_keyboard(preview_id: str, high_value: bool, label: str) -> InlineKeyboardMarkup:
    confirm = f"✅ Confirm high-value · {label}" if high_value else f"✅ Confirm · {label}"
    if len(confirm) > 60:
        confirm = "✅ Confirm high-value trade" if high_value else "✅ Confirm trade"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(confirm, callback_data=f"ok:{preview_id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"no:{preview_id}")],
        ]
    )


def import_go_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Continue to import", callback_data="import_go")]])


def import_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Confirm replacement", callback_data="import_yes")],
            [InlineKeyboardButton("❌ Cancel", callback_data="import_no")],
        ]
    )


def token_options_keyboard(mint: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🛒 Buy", callback_data=f"tok:buy:{mint}"),
                InlineKeyboardButton("💸 Sell holding", callback_data=f"tok:sell:{mint}"),
            ],
            [
                InlineKeyboardButton("🎯 Add to Autosnipe", callback_data=f"tok:auto:{mint}"),
                InlineKeyboardButton("🛡️ Safety report", callback_data=f"tok:scan:{mint}"),
            ],
            [
                InlineKeyboardButton("🔄 Refresh", callback_data=f"tok:refresh:{mint}"),
                InlineKeyboardButton("✖️ Close", callback_data="tok:close"),
            ],
        ]
    )


def autosnipe_keyboard(entries: list[dict], enabled: bool, auto_buy_on: bool = False, tp_sl_on: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        f"🎯 Autosnipe: {'ON ✅' if enabled else 'OFF ❌'}",
        callback_data="snipe:toggle",
    )]]
    rows.append([InlineKeyboardButton(
        f"🤖 Auto-buy on paste: {'ON ✅' if auto_buy_on else 'OFF ❌'}",
        callback_data="snipe:autobuy",
    )])
    rows.append([InlineKeyboardButton(
        f"💰 TP/SL auto-sell: {'ON ✅' if tp_sl_on else 'OFF ❌'}",
        callback_data="snipe:tpsl",
    )])
    for entry in entries[:10]:
        if entry["status"] == "active":
            rows.append([InlineKeyboardButton(
                f"Remove · {str(entry['token_mint'])[:10]}…",
                callback_data=f"snipe:remove:{entry['id']}",
            )])
    rows.append([InlineKeyboardButton("➕ Add token to watchlist", callback_data="snipe:add")])
    return InlineKeyboardMarkup(rows)


def config_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎚 Slippage", callback_data="cfg:slippage"),
                InlineKeyboardButton("⛽ Priority fee", callback_data="cfg:priority_fee"),
            ],
            [
                InlineKeyboardButton("🛒 Max per trade", callback_data="cfg:max_sol"),
                InlineKeyboardButton("📅 Daily limit", callback_data="cfg:daily_limit"),
            ],
            [
                InlineKeyboardButton("🎯 Snipe amount", callback_data="cfg:snipe_amount"),
                InlineKeyboardButton("🎯 Snipe impact cap", callback_data="cfg:snipe_impact"),
            ],
            [
                InlineKeyboardButton("📉 Impact cap", callback_data="cfg:impact"),
                InlineKeyboardButton("🤖 Auto-buy amount", callback_data="cfg:auto_buy_amount"),
            ],
            [
                InlineKeyboardButton("📈 Take-profit", callback_data="cfg:take_profit"),
                InlineKeyboardButton("🛑 Stop-loss", callback_data="cfg:stop_loss"),
            ],
            [InlineKeyboardButton("✖️ Close", callback_data="cfg:cancel")],
        ]
    )


def wallet_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💰 Withdraw SOL", callback_data="wal:withdraw"),
                InlineKeyboardButton("📊 Portfolio", callback_data="wal:summary"),
            ],
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="wal:refresh"),
                InlineKeyboardButton("⚙️ Trading settings", callback_data="wal:config"),
            ],
        ]
    )


def help_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎯 Autosnipe guide", callback_data="help:autosnipe"),
                InlineKeyboardButton("🛡️ Token safety", callback_data="help:safety"),
            ],
            [InlineKeyboardButton("🔐 Wallet security", callback_data="help:security")],
        ]
    )


def help_topic_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎯 Autosnipe guide", callback_data="help:autosnipe"),
                InlineKeyboardButton("🛡️ Token safety", callback_data="help:safety"),
            ],
            [
                InlineKeyboardButton("🔐 Wallet security", callback_data="help:security"),
                InlineKeyboardButton("📋 Command reference", callback_data="help:main"),
            ],
        ]
    )
