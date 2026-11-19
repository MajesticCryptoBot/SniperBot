from __future__ import annotations

from sniper.ui import info

# Telegram chat is the product interface: copy is concise, status-led and
# Maestro-inspired. Emoji stay on headers, key status lines and footers.

START_INTRO = """🎯 <b>SOLANA SNIPER</b>

Professional trading terminal for Solana.

<b>TRADING</b>
• Market buy and sell with a live quote
• Token panels with real-time market data
• Limit orders and trailing stops

<b>AUTOMATION</b>
• Autosnipe for tokens on your watchlist
• Optional auto-buy on paste
• Optional TP/SL auto-sell

<b>PORTFOLIO</b>
• Balances, holdings and realised P&L
• Full trade history
• SOL withdrawals

<b>RISK CONTROLS</b>
• Token safety checks
• Per-trade and daily spend limits
• Slippage and price-impact limits

⚠️ The bot does not scan the market. It only acts on tokens you submit or add yourself.

""" + info("Send /help for commands and operating details.")

HELP = """📋 <b>COMMANDS</b>

<b>TRADING</b>
/buy · open the buy flow
/sell · choose a holding or enter a mint
/scan · run a token safety report
/triggers · manage limit orders and trailing stops
/withdraw · prepare a SOL withdrawal

<b>PORTFOLIO</b>
/wallet · address, balance and controls
/summary · holdings with USD valuations
/orders · trade and withdrawal history

<b>AUTOMATION</b>
/autosnipe · watchlist and automation switches
/config · trading parameters

<b>ACCOUNT</b>
/stop · pause new trading and withdrawals
/resume · restore trading
/import · replace the connected wallet
/cancel · cancel the active input flow

⚡ <b>QUICK ACCESS</b>
Paste a token mint or a supported token/pair link to open its panel.

Select a topic below for details."""

HELP_SECTIONS = {
    "autosnipe": """🎯 <b>AUTOSNIPE</b>

Autosnipe watches only the tokens you add to your watchlist. It does not scan the market.

<b>HOW IT WORKS</b>
1. Add a token mint you trust.
2. Set the SOL amount per entry.
3. Enable Autosnipe.
4. When a live market appears, safety checks run.
5. A buy is queued only if all checks and limits pass.

<b>RELATED SWITCHES</b>
• Auto-buy on paste · buys a live token you paste, without confirmation
• TP/SL auto-sell · auto-exits positions at your levels

⚠️ Auto-buy bypasses manual confirmation and the Autosnipe safety gate. Enable it only if you accept that.

""" + info("Review limits with /config · pause everything with /stop."),
    "safety": """🛡️ <b>TOKEN SAFETY</b>

A safety report may include:
• Freeze and mint authority
• Liquidity and pool data
• Honeypot indicators
• External risk flags

🚫 Autosnipe is blocked when a critical check fails. Manual trades show the report; the decision stays yours.

⚠️ A report is a point-in-time assessment, not a guarantee. Missing data does not mean a token is safe.

""" + info("Use /scan with a token mint to request a report."),
    "security": """🔐 <b>WALLET SECURITY</b>

• Private keys are encrypted at rest and never displayed in chat.
• Submitted import messages are deleted immediately where Telegram permits.
• Use a dedicated wallet funded only for trading.
• Trade records include signatures for independent verification.
• /stop blocks new requests; transactions already sent may still settle.

🚨 Never share your recovery phrase or private key with anyone. The bot team will never ask for it outside the wallet-import flow.""",
}

HELP_MENU = "📚 <b>HELP</b>\nSelect a topic to continue."

IMPORT_WARNING = """🔐 <b>WALLET IMPORT</b>

You are about to replace the wallet connected to this bot.

⚠️ Use a dedicated trading wallet and confirm you accept the risks.

After you continue, send the private key in your next message. The bot deletes that message immediately; if Telegram refuses, delete it yourself at once.

""" + info("The import flow expires automatically · send /cancel to abort.")
