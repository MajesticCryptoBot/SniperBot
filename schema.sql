-- Turso / libSQL schema for the Solana trading bot.
-- Safe to re-run: every statement uses IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS users (
    telegram_id TEXT PRIMARY KEY,
    username TEXT,
    chat_id TEXT,
    wallet_address TEXT,
    enc_payload TEXT,
    trading_paused INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    telegram_id TEXT PRIMARY KEY,
    slippage_bps INTEGER NOT NULL DEFAULT 500,
    priority_fee_lamports INTEGER NOT NULL DEFAULT 100000,
    max_sol_per_trade REAL NOT NULL DEFAULT 0.1,
    daily_spend_limit_sol REAL NOT NULL DEFAULT 0.5,
    autosnipe_enabled INTEGER NOT NULL DEFAULT 0,
    autosnipe_amount_sol REAL NOT NULL DEFAULT 0.05,
    autosnipe_max_price_impact_pct REAL NOT NULL DEFAULT 30,
    max_price_impact_pct REAL NOT NULL DEFAULT 50,
    auto_buy_enabled INTEGER NOT NULL DEFAULT 0,
    auto_buy_amount_sol REAL NOT NULL DEFAULT 0.05,
    tp_sl_enabled INTEGER NOT NULL DEFAULT 0,
    take_profit_pct REAL NOT NULL DEFAULT 50,
    stop_loss_pct REAL NOT NULL DEFAULT 25,
    FOREIGN KEY (telegram_id) REFERENCES users (telegram_id)
);

CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    telegram_id TEXT NOT NULL,
    chat_id TEXT,
    side TEXT NOT NULL,
    token_mint TEXT NOT NULL,
    destination TEXT,
    amount_in_raw TEXT,
    amount_out_raw TEXT,
    amount_sol REAL,
    slippage_bps INTEGER,
    price_impact_pct REAL,
    signature TEXT,
    status TEXT NOT NULL,
    error TEXT,
    source TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    fee_lamports INTEGER,
    sol_usd REAL,
    realized_pnl_sol REAL,
    request_id TEXT,
    claim_token TEXT,
    position_applied INTEGER NOT NULL DEFAULT 0,
    priority_fee_lamports INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_user_created ON trades (telegram_id, created_at);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades (status);

-- Messages that could not be delivered while the network (or Telegram) was
-- unreachable. Flushed in order once the connection is back, so no trade or
-- trigger outcome is silently lost during an outage.
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    telegram_id TEXT NOT NULL,
    token_mint TEXT NOT NULL,
    amount_raw TEXT NOT NULL DEFAULT '0',
    cost_basis_sol REAL NOT NULL DEFAULT 0,
    realized_pnl_sol REAL NOT NULL DEFAULT 0,
    armed INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (telegram_id, token_mint)
);

CREATE TABLE IF NOT EXISTS position_ledger (
    trade_id TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);


-- Removed feature: live pool detection. Old deployments drop the table.
DROP TABLE IF EXISTS seen_pools;

CREATE TABLE IF NOT EXISTS snipe_watch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id TEXT NOT NULL,
    token_mint TEXT NOT NULL,
    amount_sol REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    trigger_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (telegram_id) REFERENCES users (telegram_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_snipe_watch_user_mint ON snipe_watch (telegram_id, token_mint);
CREATE INDEX IF NOT EXISTS idx_snipe_watch_status ON snipe_watch (status);

CREATE TABLE IF NOT EXISTS trade_triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('limit_buy', 'limit_sell', 'trailing_stop')),
    token_mint TEXT NOT NULL,
    target_price_usd REAL,
    trail_distance_pct REAL,
    peak_price_usd REAL,
    amount_sol REAL,
    sell_percent REAL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'triggered', 'cancelled', 'failed')),
    trigger_error TEXT,
    trade_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (telegram_id) REFERENCES users (telegram_id)
);

CREATE INDEX IF NOT EXISTS idx_trade_triggers_status ON trade_triggers (status);

CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fee_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT NOT NULL,
    side TEXT NOT NULL,
    token_mint TEXT NOT NULL,
    fee_mint TEXT,
    fee_bps INTEGER,
    fee_amount_raw TEXT,
    fee_value_sol REAL,
    volume_sol REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fee_ledger_trade ON fee_ledger (trade_id);
CREATE INDEX IF NOT EXISTS idx_fee_ledger_fee_mint ON fee_ledger (fee_mint);
