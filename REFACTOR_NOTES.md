# Refactor notes — professional UX & command architecture

Scope of this pass: conversational command handling, removal of the risk-acceptance
feature, sanitization of user-facing output, and hiding of administrative features.
Internal behavior (trade guards, queue, signing) is unchanged.

## 0. Live pool detection removed; autosnipe rebuilt as a user watchlist

The bot no longer scans the chain for newly created tokens or liquidity pools.
That feature was removed completely — no Raydium watching, no live token
candidates, no fanout of newly launched pools to users.

- Deleted: `sniper/pool_monitor.py` (logsSubscribe + signature polling) and
  `sniper/pools.py` (pool-init log heuristics, candidate extraction).
- Deleted: `seen_pools` table and `mark_seen_pool` / `autosnipe_users` DB
  methods. The schema drops the old table on the next boot (`DROP TABLE IF
  EXISTS`).
- Removed: `websockets` dependency, `get_signatures` / `http_to_ws` RPC
  helpers, `POOL_POLL_SECONDS`, `MAX_SNIPES_PER_POOL`, and
  `DEFAULT_AUTOSNIPE_MIN_LIQUIDITY_SOL` env settings, and the per-user
  `min_liquidity` config entry.
- No log line anywhere prints live mints, signatures, or per-user snipe
  activity. `/admin_stats` shows only a count of active watch entries.

Autosnipe is now a per-user watchlist (Maestro-style):

- A user adds a token address they got from an official announcement they
  trust (paste it in chat and tap Autosnipe, or `/autosnipe <mint>`). One
  entry stores the buy amount; the per-user `snipe_impact` price-impact cap
  and all spend guards still apply.
- `sniper/watchlist_monitor.py` polls each listed token every
  `WATCHLIST_POLL_SECONDS` (default 3s). When Jupiter first returns a price
  for the mint — i.e. liquidity exists and the token is live — the safety
  checks run, and if they pass the buy is queued for that user only. The
  entry is marked `triggered`; it never buys twice.
- Safety failures (freeze authority, blocked risk report) or spend-guard
  failures mark the entry `aborted` with the reason and notify the user;
  nothing is signed.
- The master switch (`/autosnipe` menu button, or `/stop`) pauses all
  auto-buys for that user.

Paste detection: a private-chat message that is exactly one Solana address
is verified on-chain (`getAccountInfo`); if it is an SPL token mint the bot
replies with a status panel (LIVE / NOT LIVE, authority warnings) and
buttons — Autosnipe (add to watchlist), Buy, Sell (holdings checked), Scan,
Close. Wallet addresses, signatures, and private keys get no panel.

New tests: `tests/test_watchlist.py` (address extraction, watchlist CRUD and
filtering, monitor trigger/abort/pause/devnet behavior).

### Fix: inline-button errors now reach the user

In production, tapping the autosnipe master toggle while `/stop` was active
raised `UserFacingError` inside `on_snipe_callback`. Callback handlers had no
error wrapper, so the message was lost and the framework logged an unhandled
exception. Fix:

- `callback_guard` in `sniper/handlers/common.py` wraps every inline-button
  handler (`on_snipe_callback`, `on_token_callback`, `on_trade_callback`,
  `on_check_sub`): `UserFacingError` replies with its text, unexpected errors
  reply neutrally and log the detail.
- A global `application.add_error_handler(_on_error)` is registered as a
  last-resort net for anything that slips through (including ConversationHandler
  states such as the wallet import).
- The benign PTB `per_message=False` FAQ note for the import
  ConversationHandler is silenced at startup.

New tests: `tests/test_callbacks.py`.

### Fix: pasted-token routing raised NameError

The follow-up production log showed `_handle_text` crashing with
`NameError: name 'services' is not defined` when a user pasted a token
address: `app.py` called `services(context)` but never imported it, so the
token panel never appeared. Fix:

- `services` added to the `sniper.handlers.common` import in `app.py`.
- The whole package is now scanned with pyflakes for undefined names
  (result: none) — `compileall` cannot catch this class of bug, which is
  why it slipped through.
- Regression tests in `tests/test_handle_text.py`: a pasted mint routes to
  `detect_pasted_token` with the right address and service, plain text gets
  the fallback reply.

## Cosmetic pass (UI only — no logic changes)

A consistent presentation layer across every user-facing message and
keyboard. Runtime behaviour, guards, limits, and callback data formats are
untouched except for two additive UX buttons (both display-only or
reusing existing flows):

- Emoji vocabulary per domain: 🛒 buy, 💸 sell, 📤 withdraw, 🛡️ safety/scan,
  🎯 autosnipe, ⚙️ settings, 👛 wallet, 📊 portfolio, 🧾 orders/trade ids,
  🔑 import, ⛔ stop, ▶️ resume, ✅ success, ⏳ pending, ❌ failure, 🚫 blocked,
  ⚠️ warning, 💡 tips, 🔒 security notes, 🤖 bot/system notices.
- Messages restructured: header line, blank line, body, footer tip. Dynamic
  values (mints, signatures, ids) are untouched.
- `texts.py`: rewritten START_INTRO / HELP; new HELP_SECTIONS
  (autosnipe / safety / security) + HELP_MENU.
- New display-only help topics: `help:` callbacks (`on_help_callback` in
  start.py, registered in app.py) edit the /help message between the guide
  and topic pages. No flow or trade logic involved.
- `keyboards.py`: labelled buttons (✅ Confirm / ❌ Cancel, 🗑 Remove,
  ✖️ Close, 📢 Join, 🔑 I understand — continue); autosnipe panel gains a
  ➕ Add a token button (`snipe:add`) which starts the existing
  autosnipe_add flow; help menu/topic keyboards added.
- Polished strings in flows prompts/errors, trade previews, scan report,
  confirm/cancel/queued replies, wallet + import screens, summary/orders,
  autosnipe list/toggle/add, stop/resume, token panel, spend-limit reasons,
  safety report summary, RPC rejection messages, admin outputs, and all
  background notifications (trade results, autosnipe trigger/blocked notes).
- Deliberately NOT adopted: HTML/Markdown parse_mode (would risk breaking on
  user-supplied text); emoji kept out of log lines and callback data.
- Phrases asserted by tests were preserved verbatim; suite passes 46/46 and
  pyflakes reports no undefined names.
- QA: sample messages rendered with fake services via
  `scripts/review_bot_messages.py` (outside the repo) and eyeballed for
  structure, spacing, and button labels.

## Orders readability + automatic trade-history pruning

1. `/orders` was one dense line per trade (ISO timestamp, mint, sig, error
   all concatenated). It is now numbered per-trade cards:

   `1) ❌ Buy failed · 0.0500 SOL`
   `   🎯 auto-snipe · 🕒 14 Nov 2026, 17:06 UTC`
   `   🪙 <mint>`
   `   ⚠️ <error>`

   - Timestamps are human-readable (`14 Nov 2026, 17:06 UTC`; unparseable
     values fall back to the raw string).
   - Cards show PnL and the full signature only when present; withdraws show
     the destination instead of the WSOL mint.
   - Rendering has a ~3600-char budget: if ten cards would overflow it, the
     oldest are replaced by an "older trade(s) not shown" note, so Telegram
     never hard-truncates mid-card (reply() caps at 3900).
   - Extracted pure helpers `format_trade_row` / `_render_orders`
     (portfolio.py); covered by `tests/test_orders_format.py`.

2. Nothing deleted stored trades before — history grew forever. Added
   automatic pruning, mirrored on the user's suggestion ("keep only the N
   latest"):

   - `Database.prune_trades_sync/prune_trades` (db.py): per user, keeps the
     newest `cfg.trades_keep_per_user` trades and deletes older rows; a
     48-hour grace window is never pruned, so `spent_today` (daily limit)
     and `trades_since` (hourly cap) keep counting recent trades even if a
     user exceeds the keep count within 48 h.
   - Config knob `TRADES_KEEP_PER_USER` (config.py, default 100, clamped
     10–10000), documented in `.env.example`.
   - Hooked into `TradeQueue.reconcile_loop`: runs on startup and every
     10 minutes (constant `TRADES_PRUNE_INTERVAL`), increments the
     `trades_pruned` metric, and is exception-guarded like the rest of the
     loop. /orders itself is unchanged behaviourally — it still reads the
     newest 10.
   - Test `tests/test_prune_trades_keeps_newest_and_grace`
     (tests/test_orders.py): 15 old + 2 mid + 10 recent rows for one user
     and a lone old row for another; asserts 15 removed, grace rows and the
     other user's newest kept, and that a second pass is a no-op.

   With default 100 and a 48 h grace window, spend caps and the hourly
   trade limit are provably unaffected: they only ever look back one day /
   one hour.
- Cosmetic cleanups found by the scan: dead `last` variable in
  `SolanaService.confirm`, unused `parse_positive_decimal` import.

## 1. Conversational command handling (no inline arguments required)

New module: `sniper/handlers/flows.py` — a lightweight per-user state machine.
State lives in `context.user_data["awaiting_input"]` and expires after 15 minutes.
A catch-all private-chat text handler in `sniper/app.py` routes replies into the
active flow; `/cancel` aborts it.

| Command | Flow |
| --- | --- |
| `/buy` | asks token address (mint) → asks SOL amount → preview |
| `/sell` | asks mint → asks amount or percent → preview |
| `/withdraw` | asks destination address → asks SOL amount → preview |
| `/scan` | asks mint → safety report |
| `/config` | shows current settings → asks which setting → asks new value |
| `/autosnipe` | shows the watchlist; `/autosnipe <mint>` starts add-token flow (mint → amount) |
| `/admin_user` (admin only) | asks Telegram id |

Behavior details:

- Inline arguments are still accepted and validated through the exact same
  parsers (`/buy <mint> 0.2` keeps working); an invalid inline argument falls
  back to asking conversationally instead of failing the command.
- Invalid typed input re-prompts without ending the flow.
- Flow input is rate-limited and subscription-gated the same as commands.
- New tests: `tests/test_flows.py`.

## 2. `/accept_risk` removed

- Command, callback, keyboard, and copy removed (`app.py`, `handlers/start.py`,
  `keyboards.py`, `texts.py`).
- `needs_risk` flag removed from the `command` decorator.
- `risk_accepted` column removed from `schema.sql`; `users` queries, `set_risk`,
  and the `autosnipe_users` filter removed from `sniper/db.py`; the
  `risk_accepted` parameter removed from `spend_decision` and all call sites
  (`handlers/trading.py`, `sniper/trade_queue.py`).
- Existing databases keep the old column harmlessly; the schema is created
  without it going forward.

## 3. Output sanitization

Removed from all user-facing text: custodial-wallet and key-custody wording,
"who runs the bot", the operator PC, Turso/database mentions, RPC/provider
errors, `.env`/config-key names, and the `NETWORK: mainnet-beta (REAL FUNDS)`
banner (`network_banner()` deleted; logs use the plain network name).

- `/start` and `/help` rewritten as clean product copy.
- Import flow keeps only the necessary operational instructions (send the key,
  it is deleted, use a dedicated wallet) without describing storage/decryption.
- Quote/swap/RPC failures now return neutral service messages
  ("Trading is temporarily unavailable.", "Could not get a quote right now…");
  technical detail goes to the log only.
- Subscription-gate failures no longer tell users to edit `.env` or bot
  permissions; detail is logged (`GateResult.error_detail`).
- `/orders` drops the internal `src=` label and marks auto-snipes as
  `(auto-snipe)`; cost-basis note reworded without RPC/Turso.

## 4. Administrative features hidden

- Admin section removed from `/help`.
- Non-admins invoking `/admin_*` get the same neutral response as an unknown
  command ("Unknown command. Send /help…"); no admin role or config hints.
- Admin diagnostics (`/admin_stats`, `/admin_gate`) remain available to
  configured ids only.

## 5. Discretion

- Generic handler failures reply "Something went wrong. Please try again in a
  moment." — no log paths, no stack traces in chat.
- Trade-queue notifications reworded without PC/queue-internals wording.
- Secret-protection behavior (delete pasted private keys) unchanged.

## Ops note

The `.env` that shipped inside the uploaded archive contained live credentials
(bot token, Turso token, master key, Helius/Jupiter keys). It is intentionally
excluded from this deliverable; rotate those credentials and keep `.env`
local. `.env.example` documents every variable (one was added since: `TRADES_KEEP_PER_USER` for automatic trade-history pruning).

## Turso connection-loss recovery (incident 2026-11-15 ~03:20 UTC)

After ~20 h of uptime the bot logged endless `watchlist tick failed:
ValueError` and `reconcile failed` with `Hrana: ... 502 Bad Gateway
{"error":"connect to upstream failed"}` followed by `Hrana: ... 404 Not
Found {"error":"stream not found: ..."}` until restart.

Root cause, in two layers:

1. Trigger (Turso-side, transient): the proxy briefly returned 502 — it
   could not reach the database instance. This happens on free-tier
   instance wake/restart or a network blip and is not fixable client-side.
2. Bug (ours, permanent): the libsql client holds a persistent Hrana
   stream (server-side session). Once Turso drops it, every call on the
   same connection object keeps failing with 404 "stream not found"; the
   client never re-establishes the session by itself. Evidence: ~17
   consecutive failures across two independent loops — a transient error
   would have cleared on the next tick. `Database._execute_sync` had no
   retry and the class did not even keep the information needed to rebuild
   its connection, so the database stayed unusable until manual restart
   while every loop kept catching and logging.

Fix (db.py):

- `Database` now stores a `factory` callable (libsql only; sqlite tests are
  untouched and never reconnect). `connect()` builds connections via
  `_connect_libsql(cfg)` and passes the same builder as the factory.
- `_is_connection_error` recognises connection-level failures by message
  markers (hrana / stream not found / connect to upstream / bad gateway /
  connection / closed / reset / timed out / socket / ...). Real SQL errors
  (syntax, missing table) never match.
- `_execute_sync` retries once on a connection error: `_reconnect_locked`
  closes the dead connection and opens a fresh one (safe — all DB calls
  serialize on the single-thread pool), then the statement runs again. A
  failure inside a transaction is not retried as a lone statement (the
  BEGIN died with the old socket); it propagates instead.
- `_transaction_sync` replays the whole transaction once when the error is
  a provably pre-execution failure (`_SAFE_REPLAY_MARKERS`: "connect to
  upstream", "stream not found", "bad gateway" — nothing reached the
  server, so a replay cannot double-apply). Mid-response losses such as
  timeouts are never replayed; they surface as trade failures.
- `_reconnect_locked` failure logs and re-raises the original error, so
  the loops keep their normal exception handling; each new tick retries
  the reconnect, giving self-healing once Turso is reachable again.
- Observability: `turso connection lost (...) — reconnected, retrying`
  warning on each recovered failure and
  `turso connection restored after N failed call(s)` info on success.
- watchlist_monitor logs the exception message now (`%s` of `exc`, not the
  bare class name), so the next incident shows the real reason in-line.
- Tests in `tests/test_db_reconnect.py` use a scripted fake connection:
  recovery by reconnect, no retry on plain SQL errors, reconnect failure
  keeps the original error, whole-transaction replay (fn runs twice, BEGIN
  first on the fresh socket), no replay after timeout, sqlite backend
  untouched, and both real incident messages recognised.

## Telegram API resilience (incident 2026-11-15 17:18 UTC, "dead check button")

A second account clicked the channel-membership check button and nothing
happened. The log shows `on_check_sub` dying at its very first Telegram API
call (`query.answer` at start.py) with `httpx.ConnectTimeout → telegram.error.
TimedOut`; two hours later the fallback "Something went wrong" reply timed out
too and surfaced as an unhandled update error. Meanwhile long polling kept
receiving updates, so the bot looked online.

Root cause, in two layers:

1. Trigger (environment, transient): the host PC could not open a fresh TCP
   connection to api.telegram.org for several seconds at a time (typical
   Wi-Fi roam / VPN / ISP blip). Already-established connections — the
   long-poll — kept working, which is exactly why the bot seemed alive while
   every new outgoing call died at the 5 s default connect timeout.
2. Bug (ours): handlers were one-shot. The cosmetic spinner toast
   (`query.answer`) was the first statement of `on_check_sub`, so a failure
   there aborted the membership check entirely; fallback notices ran inside
   except branches and could raise again; nothing ever retried.

Fix (common.py, start.py, app.py):

- `tg_call(call, ...)` in handlers/common.py retries a Telegram API call up
  to 3 times (1 s, then 2 s backoff) on `TimedOut`/`NetworkError` only.
  `BadRequest` and `RetryAfter` are never retried (deterministic / flood
  control). Every retry logs a warning.
- `reply()` goes through `tg_call`, so every handler and every fallback
  notice gets the retry for free.
- `ensure_subscribed` runs the membership gate through `tg_call` too, and
  `ensure_private` reuses `reply`.
- `on_check_sub` answers the callback through `_best_effort_answer`: the
  spinner toast may fail and the membership check still runs and replies —
  the click can no longer die on cosmetics.
- `command` and `callback_guard` wrappers send their fallback notices via
  `_safe_reply`, which cannot raise — a dead network no longer turns one
  error into a cascade of unhandled-update errors.
- `build_application` raises the connect timeout from 5 s to 15 s
  (`.connect_timeout(15.0)`, `.get_updates_connect_timeout(15.0)`), giving
  slow network paths time to establish a connection. Read timeouts are
  untouched.
- Tests in `tests/test_tg_resilience.py`: reply retries then succeeds, gives
  up after three attempts, never retries BadRequest, and — reproducing the
  incident — `on_check_sub` completes the membership check and delivers the
  welcome message while every `query.answer` call times out; the
  rate-limited branch also answers best-effort.

Residual ops note: the network itself (Wi-Fi/VPN/ISP on the host PC) remains
outside the bot's control; with the retry plus 15 s window, short blips are
absorbed and only longer outages surface, now with clear log lines.

## /config: one panel with tap-to-change buttons (UX redesign, 2026-11-16)

/config used to send TWO consecutive messages: the settings panel, then the
flow's "Which setting would you like to change?" prompt — and the next typed
answer had to name a setting before the user could even state the value.

New behavior (settings.py, keyboards.py, settings_logic.py, app.py):

- /config sends ONE message: settings block + the help list ("tap a button
  below, or type a name") + an inline keyboard with one button per setting
  (`cfg:slippage`, `cfg:priority_fee`, `cfg:max_sol`, `cfg:daily_limit`,
  `cfg:snipe_amount`, `cfg:snipe_impact`, `cfg:impact`) plus ✖️ Cancel.
- Tapping a button starts the existing config flow with the name prefilled,
  so the user lands directly on "Send the new value for X" — the flow engine,
  validation and apply path are unchanged.
- The empty-args /config branch no longer opens a flow, so the panel no
  longer hijacks the user's next free-text message.
- Typing a bare setting name (e.g. "impact") anywhere in free text still
  starts that setting's flow (app.py `_handle_text`), preserving the old
  interaction as a fallback.
- ✖️ Cancel clears any active flow with "Your settings are unchanged" when
  one was actually running.
- The callback handler is `callback_guard`-wrapped and answers the spinner
  best-effort (shared `best_effort_answer` in common.py, now also used by
  start.py), so a network blip on the toast cannot kill the action.

## Token panel: 🔄 Refresh button (live market data, 2026-11-16)

The enriched token panel showed a static snapshot when the CA was pasted.
A 🔄 Refresh button now sits next to ✖️ Close in `token_options_keyboard`
(callback `tok:refresh:<mint>`):

- `_panel_lines(address, info, live, profile)` was extracted from
  `detect_pasted_token` so first paste and refresh render identically —
  same fields, same fail-soft behavior.
- `_refresh_token_panel` re-fetches mint info + DexScreener profile and
  EDITS the panel message in place (`query.edit_message_text`, spinner
  answered, keyboard re-attached). No new message is sent, so the chat
  stays clean and the numbers stay live.
- Failure paths never destroy the panel: RPC down or profile lookup
  failed → soft reply ("🌐 The network did not answer. Tap 🔄 again in a
  moment."); the original panel and its buttons survive. Rapid double-tap
  with unchanged data → Telegram's "Message is not modified" BadRequest is
  swallowed instead of surfacing as an error.
- Tests in `tests/test_wallet_token_panel.py`: keyboard shape, in-place
  edit with fresh numbers ($0.00000123 → $9.50, liquidity → $250.0K),
  failure keeps the panel intact, not-modified tolerated.

## Rich buy/sell panels + limit orders & trailing stops (2026-11-16)

The manual buy/sell flows were two bare prompts. They are now button panels
that keep the entire existing safety pipeline underneath.

### Buy panel (/buy, or 🛒 Buy now on a token panel) — handlers/trade_panel.py
- One panel shows the token profile (name, price, mcap, liquidity via
  DexScreener, fail-soft), wallet SOL balance, and the current selections.
- Buttons: 0.01/0.05/0.1/0.5/1 SOL presets + ✏️ Custom (a short flow), a
  slippage sub-menu (0.5/1/3/5/10/15/30% per-trade override), a gas
  sub-menu (10k–1M lamports per-trade override), 🎯 Limit order, ✖️ Close,
  and ✅ Confirm.
- Confirm hands off to build_buy_preview — spend caps, price-impact
  limits, safety summary, and the final Confirm/Cancel preview all still
  apply. The panel only replaces typing, never the guards.
- Without a mint the panel asks for a token; /buy <mint> <amount> still
  runs the direct two-step flow as before.

### Sell panel (/sell, or 💸 Sell on a token panel)
- /sell with no arguments lists the wallet's LIVE holdings (balances via
  getTokenAccountsByOwner, values via Jupiter prices + DexScreener
  profiles, sorted by USD value, max 8 buttons + 🔄 Refresh). No CA
  typing anymore.
- Tapping a holding opens its panel: 25/50/75/100% presets + ✏️ Custom,
  the same slippage/gas sub-menus, 🎯 Limit sell, 📉 Trailing stop,
  ✅ Confirm (→ build_sell_preview, same guards).

### Per-trade overrides carried to execution
- PendingPreview gained slippage_bps / priority_fee_lamports; insert_trade
  stores them (new trades.priority_fee_lamports column + best-effort
  idempotent ALTER migration in Database._migrate_trades_columns_sync);
  TradeQueue._swap prefers the trade row over settings and passes
  priority_fee_lamports to Jupiter's GET /order as the documented
  prioritizationFeeLamports query parameter. Previews label overrides as
  "(this trade)".

### Limit orders + trailing stops (trade_triggers table, TriggerMonitor)
- schema.sql: trade_triggers (limit_buy / limit_sell / trailing_stop with
  target price, trail distance, peak price, amount/percent, status).
- db.py: add_trade_trigger, open_triggers (joins users+settings, excludes
  paused users), triggers_for, set_trigger_status, update_trigger_peak,
  update_trigger_distance, cancel_trigger, count_triggers_for.
- trigger_monitor.py: evaluate_trigger (pure) + TriggerMonitor polling
  every cfg.trigger_poll_seconds (TRIGGER_POLL_SECONDS, default 15s);
  limit_buy fires at/below target, limit_sell at/above, trailing stop
  tracks the highest price since arming and fires after the configured
  drop. Fires go through TradeQueue.enqueue_trigger_buy/sell — same spend
  guards, idempotency keys (trigger:<id>), and result notifications as
  autosnipe; refusals disarm the trigger with a user notification.
- Panel flows: limit_buy (price + SOL amount), limit_sell (price +
  percent), trailing_stop (drop % + percent); armed via panel buttons,
  managed with /triggers (cancel buttons, trailing-distance edit buttons).
- Manual triggers intentionally skip autosnipe safety checks (like manual
  trades) — the arm/execution messages say so explicitly.

### Tests
- tests/test_trade_panel.py: panel rendering, amount/slip taps, confirm
  hand-off with overrides, holdings detection, limit/trailing flows,
  /triggers callbacks, all callback_data <= 64 bytes.
- tests/test_triggers.py: evaluate_trigger cases, trigger DB roundtrip,
  migration on a pre-column deployment, monitor tick paths (fire, peak
  update, pause, empty, refusal), queue trigger enqueue (no holdings,
  percent-of-holding, overrides).
- Suite: 114 passed.
- Tests in `tests/test_config_panel.py`: single message with the full
  button set, button → value prompt with prefilled name, cancel drops the
  flow, unknown names ignored, typed names still work, keyboard shape.

## Wallet buttons + rich token-detection panel (UX + data, 2026-11-16)

Two user-requested improvements shipped together.

Wallet panel (wallet.py, keyboards.py): /wallet keeps its text but now
attaches an inline keyboard — 📤 Withdraw (opens the withdraw flow),
📊 Summary, 🔄 Refresh (re-reads balance), ⚙️ Settings (opens the /config
panel with its buttons). Callbacks are `wal:<action>`, handled by
`on_wallet_callback` with the usual guards (private chat, rate limit,
subscription gate) and a best-effort spinner answer. /import deliberately
has NO button: it is a ConversationHandler whose state machine only tracks
updates routed through the handler itself, so invoking it from a callback
would silently lose the state.

Token panel (token_panel.py, prices.py): pasting a token address now shows
a market snapshot instead of just mint + status. `PriceService
.token_profile(mint)` queries DexScreener's free token endpoint (same
service already used for liquidity, no API key) and returns the
deepest-liquidity pair as: name, symbol, price, liquidity (+ DEX name),
market cap (fdv fallback), 24h volume, 24h price change. Formatting helpers
`_fmt_usd` ($12.3K / $1.23M) and `_fmt_price` (memecoin precision down to
$0.00000123) keep the panel readable. Everything degrades gracefully: no
market data → "ℹ️ No market data yet — the token may be brand new or
unlisted." plus the original mint/status/warnings lines; a failing lookup
never blocks the panel. Freeze/mint-authority warnings and the buy/sell/
autosnipe/scan buttons are unchanged.

Tests in tests/test_wallet_token_panel.py: wallet keyboard shape, every
wal: action routed (withdraw flow begins; summary/refresh/config re-run
their commands via monkeypatched targets), rich panel contains all market
lines for a profiled token, thin panel still renders without market data,
USD/price formatting, best-pair selection, and defensive field mapping
(missing marketCap/fdv → None; unlisted or HTTP 404 → None).

## UI Restyle (Maestro-inspired, behaviour unchanged)

A full presentation pass over every panel, sub-panel and conversation
output. No logic, callback data, guard order, limits or flow changed —
only strings, button labels and message transport:

- Every user-visible message is sent as Telegram HTML (reply(),
  trade_queue._notify(), all edit_message_text paths). All dynamic values
  (token names/symbols/dex labels, signatures, errors, destinations,
  rugcheck risk text) pass through sniper/ui.esc() before embedding.
- Maestro-style headers: one emoji + bold uppercase title per panel
  (🛒 BUY, 💸 SELL, ⚙️ CONFIG, 💼 WALLET, 📊 PORTFOLIO, 🧾 TRADES,
  🎯 AUTOSNIPE, 📋 ORDER MANAGEMENT, 🛡️ SAFETY, 🔐 SECURITY, 🛠 ADMIN).
- Emoji budget ≈20% of lines: titles, ON/OFF state dots (🟢/🔴),
  status lines and italic ℹ️ footers only; field lines stay plain.
- Banned characters removed everywhere: no ━ rule lines, no em dashes
  (· and | separators instead); old space-aligned columns replaced by
  compact "Label: value" rows.
- Long explanatory paragraphs shortened to one-line footers
  (sniper/ui.info()) used sparingly.
- sniper/ui.py adds esc()/info()/onoff()/yesno() helpers.
- tests: cosmetic assertions synced to the new copy (including three
  assertions that were already stale against the shipped strings);
  fake message doubles accept parse_mode. 217/217 pass.
