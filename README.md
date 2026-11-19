# Solana Telegram sniper bot

Plain Python process for a personal PC. No Docker, no webhook, no public URL.

```text
python bot.py
```

Stop it with Ctrl+C in that terminal. Telegram updates arrive by long polling. Users, encrypted wallets, settings, and trades live in a hosted Turso database. Swaps go out to Jupiter. The bot never scans the chain for new tokens or pools: autosnipe only ever buys tokens a user added to their own watchlist from an announcement they trust.

This is custodial. The PC that runs the bot holds `MASTER_ENCRYPTION_KEY`. Anyone with that key and the Turso database can spend every wallet the bot created or imported. Use a separate wallet and only SOL you can afford to lose. New tokens are often rugs. Autosnipe is not an edge — it only speeds up a buy decision you already made about a token you know.

The draft that was used as a starting point contained a live bot token. Revoke it in @BotFather (`/revoke`) and never paste the new token into a chat. This repository does not contain that token.

## What runs where

| Piece | Where |
| --- | --- |
| `python bot.py` | Your PC (Windows 10/11 or Linux) |
| Users, settings, encrypted keys, trades, positions | Turso (hosted libSQL) |
| Membership checks | Telegram Bot API `getChatMember` |
| Quotes, signing handoff, landing | Jupiter Swap API v2 (`/order`, `/execute`) |
| Balances, mint authorities, sending withdraws, confirming signatures | Solana RPC (Helius or any JSON-RPC URL) |
| Autosnipe watchlist | Jupiter price API poll of the user's own token list |
| Optional token risk report | RugCheck |
| Optional price fallback | Birdeye |
| Logs | `logs/bot.log` on the PC (secrets redacted) |

Helius webhooks are not used. A home PC behind NAT cannot receive them. The bot only makes outbound connections.

## Realistic capacity

This is one Python process on one PC, not a hosted trading platform.

- Default `MAX_CONCURRENT_SWAPS=3`. Do not raise it past a handful. Each swap is a quote, a sign, an HTTP execute, and a confirm.
- The in-memory queue holds `TRADE_QUEUE_MAX` jobs (default 200). When it is full, new confirms are rejected. Nothing extra is signed.
- Autosnipe is a per-user watchlist: each user keeps up to `MAX_WATCH_PER_USER` (default 10) tokens, and the list is polled every `WATCHLIST_POLL_SECONDS` (default 3s) with one Jupiter price request per unique mint.
- Each user is limited to `MAX_TRADES_PER_USER_PER_HOUR` (default 20) and their own daily SOL cap.
- Public RPC endpoints rate-limit heavily. Use a paid RPC if autosnipe matters.
- A laptop can serve a private group. It cannot run market-wide monitoring, and it does not try to.
- If the PC sleeps, the process stops. Queued trades that never got a signature are failed on the next start after 90 seconds. Trades that already have a signature are confirmed or reported, not sent twice.

## Channel gate

Before any command except the explanation on `/start`, the user must be a member of both channels in `.env`:

```text
REQUIRED_CHANNEL_1=@yourchannel1
REQUIRED_CHANNEL_2=@yourchannel2
```

Use `@username` or a numeric id such as `-1001234567890`. Optional `REQUIRED_CHANNEL_1_LINK` / `REQUIRED_CHANNEL_2_LINK` override the buttons.

The bot account must be an administrator in both channels. Otherwise Telegram refuses `getChatMember` and every command stays locked. Membership is cached for `SUBSCRIPTION_CACHE_SECONDS` (default 60). "Not joined" is cached too. A failed admin check is not cached.

`/start` explains the bot, shows the two join buttons, then checks membership. If both channels are joined it creates an encrypted wallet (or loads the existing one). Other commands re-check first.

## Wallets

`/start` generates a `solders` keypair. The 64-byte secret is envelope-encrypted:

1. A random 32-byte data key encrypts the secret (AES-GCM).
2. `MASTER_ENCRYPTION_KEY` wraps that data key (AES-GCM).
3. The JSON envelope is stored in Turso `users.enc_payload`.

The master key is only read from `.env`. It is not generated at startup, so a reboot does not make wallets unreadable. Losing the key makes every wallet undecryptable. Rotating it without re-encrypting does the same.

`/import` accepts a Phantom base58 export, a 32-byte seed, or a JSON byte array, then tries to delete the Telegram message. Telegram is not a safe place for a seed. Prefer a fresh wallet.

The private key is never sent back in Telegram. On the PC:

```text
python tools/decrypt_wallet.py <telegram_id>
```

## Trading

Commands are conversational: no command requires inline arguments. `/buy`, `/sell`, `/withdraw`, `/scan`, `/config`, `/autosnipe`, and `/admin_user` each ask for their inputs one message at a time (existing inline arguments are still accepted and validated the same way). `/cancel` aborts the current step. Inputs expire after 15 minutes of inactivity.

Manual flow: `/buy` fetches a Jupiter order after asking for the mint and the SOL amount, shows price impact and a safety report, and waits for a confirm button. High-value trades (`HIGH_VALUE_SOL`, default 1 SOL, or a sell of 50%+) use a stronger confirm. The preview expires after 120 seconds. Confirm enqueues one row with a unique idempotency key.

The worker then:

1. Claims the row so a second worker cannot send it.
2. Re-checks pause, per-trade cap, daily SOL cap, and hourly cap.
3. Requests a fresh Jupiter `/order` (taker = the user's wallet).
4. Rejects the quote if price impact is above the user's limit. Nothing is signed.
5. Partially signs with `solders` (other signature slots, such as a market maker, are left intact).
6. POSTs `signedTransaction` to Jupiter `/execute`.
7. Confirms the signature on RPC. Cost basis is written once, inside a single Turso transaction.

`/withdraw` builds a SOL transfer locally (destination and amount are asked in chat), signs it, and sends it through RPC. It does not go through Jupiter.

If `SOLANA_NETWORK` is not `mainnet-beta`, Jupiter swaps and autosnipe execution are refused. Balances and devnet withdraws still work.

Token detection: the bot finds a token wherever the user shares it — a bare CA, a CA inside a message ("check this 89Voaks… 🚀"), or a CA inside a link (pump.fun, DexScreener, Solscan, Birdeye, Jup, Rugcheck…), including photo captions. Every 43-44 character base58 address in the message is validated on-chain in reading order; the first SPL token mint gets a status panel (LIVE or NOT LIVE, authority warnings) and buttons — Autosnipe, Buy, Sell (holdings are checked first), Scan, Close. Pool/pair links that carry a pair address instead of the mint (DexScreener's `…/solana/<pair>`, Photon's `…/lp/<pair>`) are resolved to the base token through DexScreener's search API. Wallet addresses, transaction signatures, and private keys get no panel; a pasted private key is still deleted by the secret guard. The `/buy` and `/sell` steps accept the same formats when the bot asks for an address.

Autosnipe is a per-user watchlist (`/autosnipe`). It never scans the market: the only tokens it can buy are ones the user added themselves, from a source they trust (a project's official announcement, for example). Add a token by pasting its address and tapping Autosnipe, or with `/autosnipe <mint>` (then the buy amount). The entry waits; the moment the token goes live — Jupiter returns a first price for it — the bot runs the safety checks and, if they pass, queues the buy for that user with their amount and their `snipe_impact` price-impact cap. Filters:

- Freeze authority always blocks; mint authority blocks only when `AUTOSNIPE_REQUIRE_MINT_REVOKED=true` (default `false`).
- RugCheck blocks when the report is missing or high-risk if `AUTOSNIPE_REQUIRE_RUGCHECK=true` (default).
- Daily spend, hourly cap, user `/stop`, and admin global pause.

A blocked or refused snipe is marked aborted with the reason and the user is notified; nothing is signed. A triggered entry never buys twice. `/autosnipe` shows the list with statuses (waiting / bought / cancelled), the master on/off button, the two automation switches below, and remove buttons.

**Auto-buy on paste** (Maestro-style, separate toggle on the `/autosnipe` panel, default off): with the switch ON, every detected token that already has a live market is bought instantly with `auto_buy_amount` SOL (settable in `/config`) — no panel, no confirmation. Like manual trades it skips the autosnipe safety checks (the token came from the user's own message), but tokens with an active freeze authority are skipped and open the panel with a warning instead. Spend guards still apply (max SOL per trade, daily limit, hourly cap, `/stop`). Every paste is a deliberate act: pasting the same token twice buys twice. `/stop` switches this off along with autosnipe.

**TP/SL auto-sell** (Maestro-style, separate toggle on the `/autosnipe` panel, default off): every position bought through the bot carries the averaged cost basis. When the switch is ON, a background monitor checks open positions each poll cycle and auto-sells the whole holding when price crosses `take_profit` percent above the entry or `stop_loss` percent below it (settable in `/config`, defaults +50% / −25%). PnL is measured in SOL terms (entry SOL per token from the cost basis vs current USD price over the live SOL price), so a moving SOL price does not distort the levels. Stop-loss wins when both levels cross in one cycle. Levels re-arm on every new buy against the new averaged entry; a fired level is disarmed until then. Fires go through the same trade queue as manual trades — spend caps, per-user serialization, idempotency keys and result notifications all apply. `/stop` pauses the monitor (positions stay armed; protection resumes with `/resume`), and imported holdings bought outside the bot are left alone — no entry price is known for them.

`/scan` runs the same safety report (mint asked in chat) and does not trade. Manual buys are allowed even when autosnipe would block; the warnings are shown first.

`/stop` pauses that user and switches auto-buy off. `/resume` allows manual trades again and leaves auto-buy off. `/admin_pause` pauses everyone. Admin commands require the Telegram user id in `ADMIN_TELEGRAM_IDS` and are hidden from the user-facing command list; non-admins get the same response as for an unknown command.

## Layout

```text
bot.py                  entrypoint
migrate.py              apply schema.sql to Turso
schema.sql
requirements.txt
.env.example
sniper/                 config, Turso, crypto, Jupiter, RPC, queue, watchlist monitor, handlers
tests/                  pytest, no network
tools/                  master key, decrypt wallet, JSON export
scripts/linux/          setup.sh, backup.sh, restore.sh
scripts/windows/        setup.ps1, backup.ps1, restore.ps1
```

## How to run on my PC

1. Install Python 3.11 or newer from [python.org](https://www.python.org/downloads/) and tick "Add python.exe to PATH" on Windows.
2. Revoke any bot token that was ever pasted into a chat. Create a new one with @BotFather.
3. Create a Turso database and a token:

   ```text
   turso db create solana-sniper
   turso db show solana-sniper --url
   turso db tokens create solana-sniper
   ```

4. Create the two Telegram channels. Add the bot as an administrator in both.
5. Get a Jupiter API key from [developers.jup.ag](https://developers.jup.ag/portal). Keyless calls are rate-limited and the current Swap API expects `x-api-key`.
6. Get a Solana mainnet RPC URL. A Helius key is optional; the public mainnet RPC will throttle trading at any real volume.
7. From this directory:

   Windows:

   ```text
   powershell -ExecutionPolicy Bypass -File scripts\windows\setup.ps1
   .\.venv\Scripts\python.exe tools\generate_master_key.py
   notepad .env
   .\.venv\Scripts\python.exe migrate.py
   .\.venv\Scripts\python.exe bot.py
   ```

   Linux:

   ```text
   bash scripts/linux/setup.sh
   source .venv/bin/activate
   python tools/generate_master_key.py
   # paste that value into MASTER_ENCRYPTION_KEY in .env, and fill the rest
   python migrate.py
   python bot.py
   ```

8. In Telegram, open the bot in a private chat, send `/start`, join both channels, tap the check button.
9. Fund the printed wallet with a small amount of SOL. `/wallet` shows the address and balance.
10. Stop the bot with Ctrl+C. Start it again with the same command. Wallets stay decryptable only if `.env` still has the same master key.

Tests, offline:

```text
pip install -r requirements.txt
pytest -q
```

## Backup and restore

Encrypted secrets are inside the dump. Store dumps next to a copy of the master key, offline.

```text
python tools/export_backup.py
```

That writes `backups/turso-export-*.json` using only `.env` (no Turso CLI).

With the Turso CLI (`turso auth login` on the PC):

```text
bash scripts/linux/backup.sh
bash scripts/linux/restore.sh backups/<dump>.sql
```

Windows: `scripts\windows\backup.ps1` and `scripts\windows\restore.ps1 -Dump backups\<dump>.sql`.

Restore into an empty database (`turso db create ...`), then point `.env` at the new URL and token. `MASTER_ENCRYPTION_KEY` must be the key that encrypted the wallets. After restore, run `python migrate.py` again. It only adds missing tables.

## Required accounts and keys

| Item | Required for | Env |
| --- | --- | --- |
| Telegram bot token | Starting the bot | `TELEGRAM_BOT_TOKEN` |
| Two channel ids | Subscription gate | `REQUIRED_CHANNEL_1`, `REQUIRED_CHANNEL_2` |
| Turso URL + token | Every user and trade | `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` |
| Master encryption key | Create, import, and spend wallets | `MASTER_ENCRYPTION_KEY` |
| Solana RPC URL | Balances, safety, withdraws, confirms | `SOLANA_RPC_URLS` |
| Jupiter API key | Mainnet buy/sell, price checks for the autosnipe watchlist | `JUPITER_API_KEY` |
| Helius API key | Optional. Only substituted into `{HELIUS_API_KEY}` in the RPC URL | `HELIUS_API_KEY` |
| Birdeye API key | Optional price fallback | `BIRDEYE_API_KEY` |
| RugCheck | Optional HTTP, no key. Autosnipe fails closed if it is down | `RUGCHECK_API_BASE` |

## Security notes

- `.env` is gitignored. Do not commit it.
- Logs run through a redaction filter for the token, Turso token, API keys, and the master key. Still do not print `decrypt_wallet.py` output into Telegram.
- Commands work only in private chats.
- A pasted private key outside `/import` is deleted when Telegram allows it and is not stored.
- Idempotency keys stop a double tap or a repeated watchlist trigger from sending two swaps.
- There is no way to pull a transaction back after it has a signature. `/stop` only blocks new ones.
