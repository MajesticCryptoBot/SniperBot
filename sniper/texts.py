from __future__ import annotations

from sniper.ui import info

# Telegram is the product. Copy stays short, status-led, and free of decoration.

START_INTRO = """<b>SOLANA SNIPER</b>
<i>Private terminal. The bot acts only on tokens you submit.</i>

<b>TRADING</b>
Market buy and sell, with a live quote before anything is signed.
Limit orders and trailing stops from the same panels.

<b>AUTOMATION</b>
Watchlist entries, optional buy on paste, and optional take-profit or stop-loss.

<b>ACCOUNT</b>
Wallet, portfolio, history, withdrawals, and spend limits.

""" + info("Join both channels below, then verify access.")

HELP = """<b>COMMANDS</b>
<i>Open a command, then answer the prompt. Arguments are optional.</i>

<b>TRADING</b>
/buy · buy panel
/sell · holdings, then a sell panel
/scan · token safety report
/triggers · limit orders and trailing stops
/withdraw · SOL withdrawal

<b>PORTFOLIO</b>
/wallet · address, balance, controls
/summary · holdings and value
/orders · trades and withdrawals

<b>AUTOMATION</b>
/autosnipe · watchlist and switches
/config · limits and execution

<b>ACCOUNT</b>
/stop · pause new trading
/resume · restore trading
/import · replace the wallet
/cancel · leave the current prompt

<b>QUICK OPEN</b>
Paste a mint, or a supported token or pair link, to open its panel.

""" + info("Choose a topic below for a short guide.")

HELP_SECTIONS = {
    "autosnipe": """<b>AUTOSNIPE</b>
<i>Watches only tokens you add. It does not scan the market.</i>

<b>STEPS</b>
1. Add a mint you trust.
2. Set the SOL amount.
3. Leave Autosnipe on.
4. A live market runs the safety checks.
5. A buy is queued only if every check and limit passes.

<b>OTHER SWITCHES</b>
Auto-buy on paste scans a live token you paste, then buys it with no confirmation.
A flagged token is not bought.
Take-profit / stop-loss sells a position when your level is reached.

""" + info("Auto-buy skips confirmation. A flagged scan blocks the buy. Review limits in /config. /stop pauses new activity."),
    "safety": """<b>TOKEN SAFETY</b>
<i>A point-in-time check. Not a guarantee.</i>

The report can include freeze and mint authority, liquidity, honeypot flags, and external risk notes.

Autosnipe will not buy when a critical check fails.
Auto-buy on paste uses the same scan and will not buy a flagged token.
A manual trade still shows the report. The decision stays yours.

Missing data does not mean a token is safe.

""" + info("Use /scan and send a mint."),
    "security": """<b>WALLET SECURITY</b>
<i>Treat the connected wallet as a trading wallet only.</i>

Keys are encrypted at rest and are never shown in chat.
Import messages are deleted immediately when Telegram allows it.
Trade records include signatures you can verify yourself.
/stop blocks new requests. A transaction already sent may still settle.

Never share a recovery phrase or private key. It is requested only inside /import.""",
}

HELP_MENU = "<b>HELP</b>\n" + info("Choose a topic.")

IMPORT_WARNING = """<b>WALLET IMPORT</b>
<i>This replaces the wallet connected to this account.</i>

Use a dedicated trading wallet. Fund only what you are prepared to trade.

After you continue, send the private key in the next message. It is deleted at once. If Telegram refuses, delete it yourself.

""" + info("The prompt expires on its own. /cancel leaves the current wallet unchanged.")
