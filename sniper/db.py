"""Turso (libSQL) access. Tests use sqlite3 with the same SQL.

All calls are serialized on one thread. Async methods hop to that thread so the
event loop stays free without sharing the connection across threads.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any

from sniper.accounting import apply_buy, apply_sell
from sniper.config import Config
from sniper.paths import SCHEMA_PATH
from sniper.timeutil import utcnow

logger = logging.getLogger(__name__)

# Turso surfaces a dropped Hrana session as a plain ValueError from the same
# connection object, and the libsql client never re-establishes it on its own
# (seen in production as 502 "connect to upstream failed" followed by endless
# 404 "stream not found"). These markers identify connection-level failures.
_CONNECTION_ERROR_MARKERS = (
    "hrana", "stream not found", "connect to upstream", "bad gateway",
    "connection", "closed", "reset", "timed out", "timeout",
    "broken pipe", "socket", "eof",
)
# Errors that provably happened before any statement reached the server, so a
# whole-transaction replay cannot double-apply anything.
_SAFE_REPLAY_MARKERS = ("connect to upstream", "stream not found", "bad gateway")
# A 502 from Turso's gateway often outlives a single immediate retry (the new
# socket fails the same way). These waits sit between the extra attempts.
# They only apply to safe, pre-execution failures — a dropped connection that
# might already have applied a write is still retried exactly once.
_SAFE_RETRY_DELAYS_S = (1.5, 4.0)


def _is_connection_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _CONNECTION_ERROR_MARKERS)


def _is_safe_replay(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _SAFE_REPLAY_MARKERS)


def split_sql(text: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        buf.append(line)
        if stripped.endswith(";"):
            statement = "\n".join(buf).strip().rstrip(";").strip()
            if statement:
                parts.append(statement)
            buf = []
    tail = "\n".join(buf).strip().rstrip(";").strip()
    if tail:
        parts.append(tail)
    return parts


def _row_dict(cursor, row) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, sqlite3.Row):
        return {key: row[key] for key in row.keys()}
    if hasattr(row, "keys"):
        try:
            return {key: row[key] for key in row.keys()}
        except Exception:
            pass
    description = cursor.description or []
    columns = [item[0] for item in description]
    if columns and not isinstance(row, dict):
        return dict(zip(columns, row))
    if isinstance(row, dict):
        return row
    return {"value": row}


class Database:
    def __init__(self, conn, backend: str, factory=None):
        self._conn = conn
        self.backend = backend
        self._factory = factory  # rebuilds a libsql connection after it dies
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="turso")
        self._in_txn = False
        self._failed_calls = 0

    @classmethod
    def connect(cls, cfg: Config) -> "Database":
        try:
            first_conn = cls._connect_libsql(cfg)
        except ImportError as exc:
            raise RuntimeError(
                "The libsql package is not installed. Run: pip install -r requirements.txt"
            ) from exc
        return cls(
            first_conn,
            "libsql",
            factory=lambda: cls._connect_libsql(cfg),
        )

    @staticmethod
    def _connect_libsql(cfg: Config):
        import libsql
        try:
            conn = libsql.connect(database=cfg.turso_url, auth_token=cfg.turso_token, _check_same_thread=False)
        except TypeError:
            conn = libsql.connect(database=cfg.turso_url, auth_token=cfg.turso_token)
        return conn

    @classmethod
    def connect_sqlite(cls, path: str = ":memory:") -> "Database":
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return cls(conn, "sqlite")

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=False)
        try:
            self._conn.close()
        except Exception:
            pass

    def _reconnect_locked(self) -> None:
        """Replace a dead libsql connection. The single DB thread makes this safe."""
        try:
            self._conn.close()
        except Exception:
            pass
        self._conn = self._factory()

    def _recover(self, exc: Exception, safe_only: bool = False) -> bool:
        """Try to rebuild the connection after a lost Turso session. With
        safe_only, only errors that provably happened before any statement
        reached the server are replayable."""
        if self._factory is None or not _is_connection_error(exc):
            return False
        if safe_only and not any(marker in str(exc).lower() for marker in _SAFE_REPLAY_MARKERS):
            return False
        self._failed_calls += 1
        try:
            self._reconnect_locked()
        except Exception as reconnect_exc:
            logger.warning("turso reconnect failed: %s (original error: %s)", reconnect_exc, exc)
            return False
        logger.warning("turso connection lost (%s) — reconnected, retrying", exc)
        return True

    def _log_restored(self) -> None:
        if self._failed_calls:
            logger.info("turso connection restored after %s failed call(s)", self._failed_calls)
            self._failed_calls = 0

    def _execute_sync(self, sql: str, params: tuple, fetch: bool) -> list[dict]:
        try:
            result = self._execute_once(sql, params, fetch)
        except Exception as exc:
            # Inside a transaction we must not retry a lone statement — the
            # BEGIN went down with the dead connection. Let the error reach
            # _transaction_sync, which replays the whole thing on a new socket.
            if self._in_txn or not self._recover(exc):
                raise
            result = self._retry_after_reconnect(lambda: self._execute_once(sql, params, fetch), exc)
        else:
            return result
        self._log_restored()
        return result

    def _retry_after_reconnect(self, attempt, first_exc: Exception):
        """Run `attempt` again after `_recover` already rebuilt the socket.

        The first retry is immediate, matching the previous behaviour. Safe
        gateway failures get a few more tries: Turso answers 502 for a short
        stretch, and the socket we just opened fails the same way.
        """
        delays = list(_SAFE_RETRY_DELAYS_S) if _is_safe_replay(first_exc) else []
        while True:
            try:
                return attempt()
            except Exception as exc:
                if not delays or not _is_safe_replay(exc) or not self._recover(exc, safe_only=self._in_txn):
                    raise exc from None
                time.sleep(delays.pop(0))

    def _execute_once(self, sql: str, params: tuple, fetch: bool) -> list[dict]:
        cursor = self._conn.execute(sql, params)
        if not self._in_txn:
            self._conn.commit()
        if not fetch:
            return []
        rows = cursor.fetchall()
        return [_row_dict(cursor, row) for row in rows]

    def _transaction_sync(self, fn):
        """Run fn as one commit. A crash or exception rolls every statement back."""
        if self._in_txn:
            return fn()
        self._in_txn = True
        try:
            try:
                result = self._transaction_once(fn)
            except Exception as exc:
                # A dropped Turso session killed the transaction; if the error
                # is provably pre-execution, nothing was applied — replay the
                # whole transaction on a fresh connection. A lingering 502 gets
                # the same extra backoff as a lone statement.
                if not self._recover(exc, safe_only=True):
                    raise
                result = self._retry_after_reconnect(lambda: self._transaction_once(fn), exc)
            self._log_restored()
            return result
        finally:
            self._in_txn = False

    def _transaction_once(self, fn):
        self._conn.execute("BEGIN")
        try:
            result = fn()
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass  # the connection may already be dead — nothing to roll back
            raise
        else:
            self._conn.commit()
            return result

    def execute_sync(self, sql: str, params: tuple = ()) -> None:
        self._execute_sync(sql, tuple(params), False)

    def fetchall_sync(self, sql: str, params: tuple = ()) -> list[dict]:
        return self._execute_sync(sql, tuple(params), True)

    def fetchone_sync(self, sql: str, params: tuple = ()) -> dict | None:
        rows = self.fetchall_sync(sql, params)
        return rows[0] if rows else None

    async def execute(self, sql: str, params: tuple = ()) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._pool, partial(self._execute_sync, sql, tuple(params), False))

    async def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, partial(self._execute_sync, sql, tuple(params), True))

    async def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        rows = await self.fetchall(sql, params)
        return rows[0] if rows else None

    def apply_schema(self, path=SCHEMA_PATH) -> int:
        statements = split_sql(PathText.read(path))
        for statement in statements:
            self.execute_sync(statement)
        self._migrate_trades_columns_sync()
        self._migrate_added_columns_sync()
        return len(statements)

    def _table_columns_sync(self, table: str) -> set[str]:
        """Column names of an existing table, read from the sqlite_master DDL.

        Deliberately a statement that always succeeds: on Turso a *failing*
        statement comes back wrapped in Hrana stream text whose markers ("hrana",
        "stream") make the reconnect logic believe the connection died — exactly
        the noise a duplicate-column ALTER produced on every migrate run.
        Parsing the DDL avoids ever running that ALTER."""
        row = self.fetchone_sync(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
        if not row or not row.get("sql"):
            return set()
        text = row["sql"]
        start = text.find("(")
        if start < 0:
            return set()
        depth = 0
        columns: list[str] = []
        current: list[str] = []
        for char in text[start + 1:]:
            if char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    break  # closes the column list
                depth -= 1
            elif char == "," and depth == 0:
                columns.append("".join(current))
                current = []
                continue
            current.append(char)
        if current:
            columns.append("".join(current))
        names = set()
        for column in columns:
            parts = column.split()
            if not parts:
                continue
            token = parts[0].lower().strip('`"[]')
            # A definition line can start with a table constraint, not a column.
            if token and token not in {"primary", "unique", "check", "foreign", "constraint"}:
                names.add(token)
        return names

    def _migrate_trades_columns_sync(self) -> None:
        """trades.priority_fee_lamports and trades.attempts arrived after the
        first deployments. CREATE TABLE IF NOT EXISTS never adds columns to an
        existing table, so add the missing ones explicitly. Existing columns
        are probed first so a duplicate-column ALTER is never attempted."""
        existing = self._table_columns_sync("trades")
        for column, ddl in (
            ("priority_fee_lamports", "ALTER TABLE trades ADD COLUMN priority_fee_lamports INTEGER"),
            ("attempts", "ALTER TABLE trades ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"),
        ):
            if column in existing:
                continue
            try:
                self.execute_sync(ddl)
                logger.info("added trades.%s column", column)
            except Exception as exc:
                text = str(exc).lower()
                if "duplicate column" not in text and "already exists" not in text:
                    logger.warning("trades column migration check: %s", exc.__class__.__name__)

    def _migrate_added_columns_sync(self) -> None:
        """Columns that arrived after the first deployments: settings gained
        the auto-buy and take-profit/stop-loss switches, positions gained the
        TP/SL armed flag. Probed first so a duplicate-column ALTER is never
        attempted (see _table_columns_sync for why)."""
        for table, column, ddl in (
            ("settings", "auto_buy_enabled", "ALTER TABLE settings ADD COLUMN auto_buy_enabled INTEGER NOT NULL DEFAULT 0"),
            ("settings", "auto_buy_amount_sol", "ALTER TABLE settings ADD COLUMN auto_buy_amount_sol REAL NOT NULL DEFAULT 0.05"),
            ("settings", "tp_sl_enabled", "ALTER TABLE settings ADD COLUMN tp_sl_enabled INTEGER NOT NULL DEFAULT 0"),
            ("settings", "take_profit_pct", "ALTER TABLE settings ADD COLUMN take_profit_pct REAL NOT NULL DEFAULT 50"),
            ("settings", "stop_loss_pct", "ALTER TABLE settings ADD COLUMN stop_loss_pct REAL NOT NULL DEFAULT 25"),
            ("positions", "armed", "ALTER TABLE positions ADD COLUMN armed INTEGER NOT NULL DEFAULT 1"),
        ):
            existing = self._table_columns_sync(table)
            if column in existing:
                continue
            try:
                self.execute_sync(ddl)
                logger.info("added %s.%s column", table, column)
            except Exception as exc:
                text = str(exc).lower()
                if "duplicate column" not in text and "already exists" not in text:
                    logger.warning("%s column migration check: %s", table, exc.__class__.__name__)

    def create_user_sync(
        self,
        *,
        telegram_id: str,
        username: str | None,
        chat_id: str,
        wallet_address: str,
        enc_payload: str,
        defaults: dict,
    ) -> None:
        now = utcnow()

        def write() -> None:
            self.execute_sync(
                """
                INSERT INTO users (
                    telegram_id, username, chat_id, wallet_address, enc_payload,
                    trading_paused, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (telegram_id, username, chat_id, wallet_address, enc_payload, now, now),
            )
            self.execute_sync(
                """
                INSERT INTO settings (
                    telegram_id, slippage_bps, priority_fee_lamports, max_sol_per_trade,
                    daily_spend_limit_sol, autosnipe_enabled,
                    autosnipe_amount_sol, autosnipe_max_price_impact_pct, max_price_impact_pct
                ) VALUES (?, ?, ?, ?, ?, 0, ?, 30, 50)
                """,
                (
                    telegram_id,
                    defaults["slippage_bps"],
                    defaults["priority_fee_lamports"],
                    defaults["max_sol_per_trade"],
                    defaults["daily_spend_limit_sol"],
                    defaults["autosnipe_amount_sol"],
                ),
            )

        self._transaction_sync(write)

    async def create_user(self, **kwargs) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._pool, partial(self.create_user_sync, **kwargs))

    def touch_user_sync(self, telegram_id: str, username: str | None, chat_id: str) -> None:
        self.execute_sync(
            "UPDATE users SET username=?, chat_id=?, updated_at=? WHERE telegram_id=?",
            (username, chat_id, utcnow(), telegram_id),
        )

    async def touch_user(self, telegram_id: str, username: str | None, chat_id: str) -> None:
        await self.execute(
            "UPDATE users SET username=?, chat_id=?, updated_at=? WHERE telegram_id=?",
            (username, chat_id, utcnow(), telegram_id),
        )

    def get_user_sync(self, telegram_id: str) -> dict | None:
        return self.fetchone_sync(
            """
            SELECT telegram_id, username, chat_id, wallet_address,
                   trading_paused, created_at, updated_at
            FROM users WHERE telegram_id=?
            """,
            (telegram_id,),
        )

    async def get_user(self, telegram_id: str) -> dict | None:
        return await self.fetchone(
            """
            SELECT telegram_id, username, chat_id, wallet_address,
                   trading_paused, created_at, updated_at
            FROM users WHERE telegram_id=?
            """,
            (telegram_id,),
        )

    def get_secret_payload_sync(self, telegram_id: str) -> str | None:
        row = self.fetchone_sync("SELECT enc_payload FROM users WHERE telegram_id=?", (telegram_id,))
        if not row:
            return None
        return row.get("enc_payload")

    async def get_secret_payload(self, telegram_id: str) -> str | None:
        row = await self.fetchone("SELECT enc_payload FROM users WHERE telegram_id=?", (telegram_id,))
        if not row:
            return None
        return row.get("enc_payload")

    def set_wallet_sync(self, telegram_id: str, address: str, enc_payload: str) -> None:
        self.execute_sync(
            "UPDATE users SET wallet_address=?, enc_payload=?, updated_at=? WHERE telegram_id=?",
            (address, enc_payload, utcnow(), telegram_id),
        )

    async def set_wallet(self, telegram_id: str, address: str, enc_payload: str) -> None:
        await self.execute(
            "UPDATE users SET wallet_address=?, enc_payload=?, updated_at=? WHERE telegram_id=?",
            (address, enc_payload, utcnow(), telegram_id),
        )

    async def set_paused(self, telegram_id: str, paused: bool) -> None:
        await self.execute(
            "UPDATE users SET trading_paused=?, updated_at=? WHERE telegram_id=?",
            (1 if paused else 0, utcnow(), telegram_id),
        )
        if paused:
            # /stop halts every automation that can put the user's SOL at
            # risk. TP/SL auto-sell stays configured — it only fires for
            # users who are not paused, and resumes when they /resume.
            await self.execute(
                "UPDATE settings SET autosnipe_enabled=0, auto_buy_enabled=0 WHERE telegram_id=?",
                (telegram_id,),
            )

    async def get_settings(self, telegram_id: str) -> dict | None:
        return await self.fetchone("SELECT * FROM settings WHERE telegram_id=?", (telegram_id,))

    def get_settings_sync(self, telegram_id: str) -> dict | None:
        return self.fetchone_sync("SELECT * FROM settings WHERE telegram_id=?", (telegram_id,))

    _SETTING_COLUMNS = {
        "slippage_bps",
        "priority_fee_lamports",
        "max_sol_per_trade",
        "daily_spend_limit_sol",
        "autosnipe_enabled",
        "autosnipe_amount_sol",
        "autosnipe_max_price_impact_pct",
        "max_price_impact_pct",
        "auto_buy_enabled",
        "auto_buy_amount_sol",
        "tp_sl_enabled",
        "take_profit_pct",
        "stop_loss_pct",
    }

    async def update_setting(self, telegram_id: str, column: str, value) -> None:
        if column not in self._SETTING_COLUMNS:
            raise ValueError("refusing to update an unknown settings column")
        await self.execute(
            f"UPDATE settings SET {column}=? WHERE telegram_id=?",
            (value, telegram_id),
        )

    async def insert_trade(self, row: dict) -> bool:
        existing = await self.fetchone(
            "SELECT id, status FROM trades WHERE idempotency_key=?",
            (row["idempotency_key"],),
        )
        if existing:
            return False
        try:
            await self.execute(
                """
                INSERT INTO trades (
                    id, telegram_id, chat_id, side, token_mint, destination,
                    amount_in_raw, amount_out_raw, amount_sol, slippage_bps,
                    price_impact_pct, signature, status, error, source,
                    idempotency_key, fee_lamports, sol_usd, realized_pnl_sol,
                    request_id, claim_token, position_applied, priority_fee_lamports,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, 0, ?,
                    ?, ?
                )
                """,
                (
                    row["id"],
                    row["telegram_id"],
                    row.get("chat_id"),
                    row["side"],
                    row["token_mint"],
                    row.get("destination"),
                    row.get("amount_in_raw"),
                    row.get("amount_out_raw"),
                    row.get("amount_sol"),
                    row.get("slippage_bps"),
                    row.get("price_impact_pct"),
                    row.get("signature"),
                    row["status"],
                    row.get("error"),
                    row["source"],
                    row["idempotency_key"],
                    row.get("fee_lamports"),
                    row.get("sol_usd"),
                    row.get("realized_pnl_sol"),
                    row.get("request_id"),
                    row.get("claim_token"),
                    row.get("priority_fee_lamports"),
                    row["created_at"],
                    row["updated_at"],
                ),
            )
        except Exception as exc:
            if "unique" in str(exc).lower() or "constraint" in str(exc).lower():
                return False
            raise
        return True

    def claim_trade_sync(self, trade_id: str, token: str) -> bool:
        now = utcnow()
        self.execute_sync(
            """
            UPDATE trades
            SET status='processing', claim_token=?, updated_at=?
            WHERE id=? AND status='pending'
            """,
            (token, now, trade_id),
        )
        row = self.fetchone_sync("SELECT status, claim_token FROM trades WHERE id=?", (trade_id,))
        return bool(row and row["status"] == "processing" and row["claim_token"] == token)

    async def claim_trade(self, trade_id: str, token: str) -> bool:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, partial(self.claim_trade_sync, trade_id, token))

    async def update_trade_fields(self, trade_id: str, **fields) -> None:
        allowed = {
            "amount_in_raw",
            "amount_out_raw",
            "amount_sol",
            "slippage_bps",
            "price_impact_pct",
            "signature",
            "status",
            "error",
            "fee_lamports",
            "sol_usd",
            "realized_pnl_sol",
            "request_id",
            "position_applied",
            "attempts",
        }
        keys = [key for key in fields if key in allowed]
        if not keys:
            return
        assignments = ", ".join(f"{key}=?" for key in keys)
        params = tuple(fields[key] for key in keys) + (utcnow(), trade_id)
        await self.execute(f"UPDATE trades SET {assignments}, updated_at=? WHERE id=?", params)

    async def spent_today(self, telegram_id: str, day_start: str) -> float:
        row = await self.fetchone(
            """
            SELECT COALESCE(SUM(amount_sol), 0) AS spent
            FROM trades
            WHERE telegram_id=?
              AND side IN ('buy', 'withdraw')
              AND status IN ('pending', 'processing', 'submitted', 'confirmed')
              AND created_at >= ?
            """,
            (telegram_id, day_start),
        )
        return float(row["spent"] if row else 0)

    async def trades_since(self, telegram_id: str, since: str) -> int:
        row = await self.fetchone(
            """
            SELECT COUNT(*) AS n FROM trades
            WHERE telegram_id=? AND created_at >= ?
              AND status IN ('pending', 'processing', 'submitted', 'confirmed')
            """,
            (telegram_id, since),
        )
        return int(row["n"] if row else 0)

    async def recent_trades(self, telegram_id: str, limit: int = 10) -> list[dict]:
        return await self.fetchall(
            """
            SELECT id, side, token_mint, destination, amount_sol, amount_in_raw, amount_out_raw,
                   signature, status, error, source, realized_pnl_sol, created_at
            FROM trades
            WHERE telegram_id=?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (telegram_id, limit),
        )

    def prune_trades_sync(self, keep: int, grace_seconds: int) -> int:
        """Keep each user's newest `keep` trades, plus everything inside the
        grace window (recent rows still feed the hourly and daily spend caps),
        and delete the rest. Returns the number of rows removed."""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=grace_seconds)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        self.execute_sync(
            """
            DELETE FROM trades
            WHERE created_at < ?
              AND id NOT IN (
                  SELECT id FROM trades t2
                  WHERE t2.telegram_id = trades.telegram_id
                  ORDER BY created_at DESC, id DESC
                  LIMIT ?
              )
            """,
            (cutoff, keep),
        )
        row = self.fetchone_sync("SELECT changes() AS n")
        return int(row["n"]) if row else 0

    async def prune_trades(self, keep: int, grace_seconds: int) -> int:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, partial(self.prune_trades_sync, keep, grace_seconds))

    async def get_trade(self, trade_id: str) -> dict | None:
        return await self.fetchone("SELECT * FROM trades WHERE id=?", (trade_id,))

    def get_trade_sync(self, trade_id: str) -> dict | None:
        return self.fetchone_sync("SELECT * FROM trades WHERE id=?", (trade_id,))

    async def open_trades(self) -> list[dict]:
        return await self.fetchall(
            """
            SELECT id, signature, status, created_at, updated_at, chat_id, side,
                   position_applied, attempts
            FROM trades
            WHERE status IN ('pending', 'processing', 'submitted')
            """
        )

    async def outbox_add(self, chat_id, text: str) -> None:
        """Park a message that could not be delivered right now.

        Capped so an overnight outage cannot accumulate an unbounded backlog;
        the newest 200 messages survive, which is far more than any single
        user's trades and triggers can produce."""
        await self.execute(
            "INSERT INTO outbox (chat_id, text, created_at) VALUES (?, ?, ?)",
            (str(chat_id), text[:3900], utcnow()),
        )
        await self.execute(
            """
            DELETE FROM outbox WHERE id NOT IN (
                SELECT id FROM outbox ORDER BY id DESC LIMIT 200
            )
            """
        )

    async def outbox_rows(self, limit: int = 50) -> list[dict]:
        return await self.fetchall(
            "SELECT id, chat_id, text FROM outbox ORDER BY id ASC LIMIT ?",
            (limit,),
        )

    async def outbox_delete(self, ids: list[int]) -> None:
        if not ids:
            return
        placeholders = ", ".join("?" for _ in ids)
        await self.execute(f"DELETE FROM outbox WHERE id IN ({placeholders})", tuple(ids))

    async def list_positions(self, telegram_id: str) -> list[dict]:
        return await self.fetchall(
            """
            SELECT token_mint, amount_raw, cost_basis_sol, realized_pnl_sol, armed
            FROM positions WHERE telegram_id=?
            """,
            (telegram_id,),
        )

    async def positions_for_tp_sl(self) -> list[dict]:
        """Open, TP/SL-armed positions of users who switched TP/SL on.

        Paused users are excluded — their auto-sell would be refused by the
        trade guards anyway, and skipping them here keeps the protection
        armed so it resumes with /resume instead of silently disarming."""
        return await self.fetchall(
            """
            SELECT p.telegram_id, p.token_mint, p.amount_raw, p.cost_basis_sol,
                   p.updated_at, s.take_profit_pct, s.stop_loss_pct,
                   u.chat_id, u.wallet_address
            FROM positions p
            JOIN settings s ON s.telegram_id = p.telegram_id
            JOIN users u ON u.telegram_id = p.telegram_id
            WHERE s.tp_sl_enabled = 1 AND p.armed = 1 AND u.trading_paused = 0
            """,
        )

    async def set_position_armed(self, telegram_id: str, mint: str, armed: bool) -> None:
        await self.execute(
            "UPDATE positions SET armed=?, updated_at=? WHERE telegram_id=? AND token_mint=?",
            (1 if armed else 0, utcnow(), telegram_id, mint),
        )

    def finalize_sync(self, trade_id: str, *, success: bool, error: str | None, fee_lamports: int | None) -> str:
        """Apply cost-basis once and set the terminal status. Returns the resulting status.

        The ledger row, position update, and confirmed flag commit together.
        A crash in the middle leaves the trade unconfirmed and the position unchanged.
        """

        def body() -> str:
            now = utcnow()
            trade = self.fetchone_sync("SELECT * FROM trades WHERE id=?", (trade_id,))
            if not trade:
                return "missing"
            if not success:
                self.execute_sync(
                    "UPDATE trades SET status='failed', error=?, fee_lamports=COALESCE(?, fee_lamports), updated_at=? WHERE id=?",
                    (error or "failed", fee_lamports, now, trade_id),
                )
                return "failed"
            if int(trade.get("position_applied") or 0) == 1 or trade["status"] == "confirmed":
                self.execute_sync(
                    "UPDATE trades SET status='confirmed', updated_at=? WHERE id=?",
                    (now, trade_id),
                )
                return "confirmed"
            existing_ledger = self.fetchone_sync("SELECT trade_id FROM position_ledger WHERE trade_id=?", (trade_id,))
            if existing_ledger:
                self.execute_sync(
                    "UPDATE trades SET status='confirmed', position_applied=1, updated_at=? WHERE id=?",
                    (now, trade_id),
                )
                return "confirmed"
            side = trade["side"]
            self.execute_sync(
                "INSERT INTO position_ledger (trade_id, applied_at) VALUES (?, ?)",
                (trade_id, now),
            )
            realized = None
            if side == "buy":
                mint = trade["token_mint"]
                buy_raw = int(trade.get("amount_out_raw") or 0)
                buy_sol = float(trade.get("amount_sol") or 0)
                self._add_buy_position(trade["telegram_id"], mint, buy_raw, buy_sol, now)
            elif side == "sell":
                mint = trade["token_mint"]
                sell_raw = int(trade.get("amount_in_raw") or 0)
                sol_received = float(trade.get("amount_sol") or 0)
                realized = self._add_sell_position(trade["telegram_id"], mint, sell_raw, sol_received, now)
            self.execute_sync(
                """
                UPDATE trades
                SET status='confirmed', position_applied=1, error=NULL,
                    fee_lamports=COALESCE(?, fee_lamports),
                    realized_pnl_sol=COALESCE(?, realized_pnl_sol),
                    updated_at=?
                WHERE id=?
                """,
                (fee_lamports, realized, now, trade_id),
            )
            return "confirmed"

        return self._transaction_sync(body)

    def _add_buy_position(self, telegram_id: str, mint: str, buy_raw: int, buy_sol: float, now: str) -> None:
        current = self.fetchone_sync(
            "SELECT amount_raw, cost_basis_sol FROM positions WHERE telegram_id=? AND token_mint=?",
            (telegram_id, mint),
        )
        if not current:
            self.execute_sync(
                """
                INSERT INTO positions (telegram_id, token_mint, amount_raw, cost_basis_sol, realized_pnl_sol, armed, updated_at)
                VALUES (?, ?, ?, ?, 0, 1, ?)
                """,
                (telegram_id, mint, str(buy_raw), buy_sol, now),
            )
            return
        new_raw, new_cost = apply_buy(int(current["amount_raw"]), float(current["cost_basis_sol"]), buy_raw, buy_sol)
        self.execute_sync(
            # A new buy re-arms TP/SL for the whole averaged position — the
            # entry changed, so the take-profit and stop-loss levels apply
            # to the new entry from scratch.
            "UPDATE positions SET amount_raw=?, cost_basis_sol=?, armed=1, updated_at=? WHERE telegram_id=? AND token_mint=?",
            (str(new_raw), new_cost, now, telegram_id, mint),
        )

    def _add_sell_position(self, telegram_id: str, mint: str, sell_raw: int, sol_received: float, now: str) -> float:
        current = self.fetchone_sync(
            "SELECT amount_raw, cost_basis_sol, realized_pnl_sol FROM positions WHERE telegram_id=? AND token_mint=?",
            (telegram_id, mint),
        )
        if not current:
            self.execute_sync(
                """
                INSERT INTO positions (telegram_id, token_mint, amount_raw, cost_basis_sol, realized_pnl_sol, updated_at)
                VALUES (?, ?, '0', 0, ?, ?)
                """,
                (telegram_id, mint, sol_received, now),
            )
            return sol_received
        new_raw, new_cost, pnl = apply_sell(
            int(current["amount_raw"]),
            float(current["cost_basis_sol"]),
            sell_raw,
            sol_received,
        )
        realized = float(current["realized_pnl_sol"] or 0) + pnl
        self.execute_sync(
            """
            UPDATE positions
            SET amount_raw=?, cost_basis_sol=?, realized_pnl_sol=?, updated_at=?
            WHERE telegram_id=? AND token_mint=?
            """,
            (str(new_raw), new_cost, realized, now, telegram_id, mint),
        )
        return pnl

    async def finalize(self, trade_id: str, *, success: bool, error: str | None = None, fee_lamports: int | None = None) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._pool,
            partial(self.finalize_sync, trade_id, success=success, error=error, fee_lamports=fee_lamports),
        )

    # ------------------------------------------------------------------
    # Autosnipe watchlist. The bot never scans the chain for new tokens;
    # the only tokens it can auto-buy are ones a user added here from an
    # official announcement they trust.
    # ------------------------------------------------------------------

    def add_watch_entry_sync(self, telegram_id: str, mint: str, amount_sol: float) -> tuple[int, str]:
        """Insert or reactivate a watch entry. Returns (id, action).

        action is "added", "updated" (entry was already waiting), or
        "reactivated" (a triggered or aborted entry was reset).
        """
        now = utcnow()

        def body() -> tuple[int, str]:
            row = self.fetchone_sync(
                "SELECT id, status FROM snipe_watch WHERE telegram_id=? AND token_mint=?",
                (telegram_id, mint),
            )
            if row is None:
                self.execute_sync(
                    """
                    INSERT INTO snipe_watch (telegram_id, token_mint, amount_sol, status, created_at, updated_at)
                    VALUES (?, ?, ?, 'active', ?, ?)
                    """,
                    (telegram_id, mint, amount_sol, now, now),
                )
                return int(self.fetchone_sync("SELECT id FROM snipe_watch WHERE telegram_id=? AND token_mint=?", (telegram_id, mint))["id"]), "added"
            if row["status"] == "active":
                self.execute_sync(
                    "UPDATE snipe_watch SET amount_sol=?, updated_at=? WHERE id=?",
                    (amount_sol, now, row["id"]),
                )
                return int(row["id"]), "updated"
            self.execute_sync(
                """
                UPDATE snipe_watch
                SET amount_sol=?, status='active', trigger_error=NULL, created_at=?, updated_at=?
                WHERE id=?
                """,
                (amount_sol, now, now, row["id"]),
            )
            return int(row["id"]), "reactivated"

        return self._transaction_sync(body)

    async def add_watch_entry(self, telegram_id: str, mint: str, amount_sol: float) -> tuple[int, str]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._pool,
            partial(self.add_watch_entry_sync, telegram_id, mint, amount_sol),
        )

    async def watchlist_active(self) -> list[dict]:
        """Entries ready to trigger, with the user row and settings attached."""
        return await self.fetchall(
            """
            SELECT sw.id, sw.telegram_id, sw.token_mint, sw.amount_sol, sw.created_at,
                   u.chat_id, u.wallet_address, u.trading_paused,
                   s.autosnipe_enabled, s.max_sol_per_trade, s.daily_spend_limit_sol,
                   s.slippage_bps, s.autosnipe_max_price_impact_pct,
                   s.priority_fee_lamports, s.max_price_impact_pct
            FROM snipe_watch sw
            JOIN users u ON u.telegram_id = sw.telegram_id
            JOIN settings s ON s.telegram_id = sw.telegram_id
            WHERE sw.status = 'active'
              AND s.autosnipe_enabled = 1
              AND u.trading_paused = 0
              AND u.wallet_address IS NOT NULL
            ORDER BY sw.created_at
            """
        )

    async def watchlist_for(self, telegram_id: str) -> list[dict]:
        return await self.fetchall(
            """
            SELECT id, token_mint, amount_sol, status, trigger_error, created_at, updated_at
            FROM snipe_watch
            WHERE telegram_id=?
            ORDER BY created_at DESC
            """,
            (telegram_id,),
        )

    async def set_watch_status(self, watch_id: int, telegram_id: str, status: str, error: str | None = None) -> bool:
        await self.execute(
            "UPDATE snipe_watch SET status=?, trigger_error=?, updated_at=? WHERE id=? AND telegram_id=?",
            (status, error, utcnow(), watch_id, telegram_id),
        )
        row = await self.fetchone("SELECT id FROM snipe_watch WHERE id=? AND telegram_id=? AND status=?", (watch_id, telegram_id, status))
        return row is not None

    async def remove_watch_entry(self, watch_id: int, telegram_id: str) -> bool:
        row = await self.fetchone("SELECT id FROM snipe_watch WHERE id=? AND telegram_id=?", (watch_id, telegram_id))
        if not row:
            return False
        await self.execute("DELETE FROM snipe_watch WHERE id=? AND telegram_id=?", (watch_id, telegram_id))
        return True

    async def count_watch_entries(self, status: str | None = None) -> int:
        if status:
            row = await self.fetchone("SELECT COUNT(*) AS n FROM snipe_watch WHERE status=?", (status,))
        else:
            row = await self.fetchone("SELECT COUNT(*) AS n FROM snipe_watch")
        return int(row["n"] if row else 0)

    async def count_watch_for(self, telegram_id: str) -> int:
        row = await self.fetchone("SELECT COUNT(*) AS n FROM snipe_watch WHERE telegram_id=?", (telegram_id,))
        return int(row["n"] if row else 0)

    async def get_state(self, key: str) -> str | None:
        row = await self.fetchone("SELECT value FROM bot_state WHERE key=?", (key,))
        return None if not row else row["value"]

    async def set_state(self, key: str, value: str) -> None:
        existing = await self.get_state(key)
        if existing is None:
            await self.execute("INSERT INTO bot_state (key, value) VALUES (?, ?)", (key, value))
        else:
            await self.execute("UPDATE bot_state SET value=? WHERE key=?", (value, key))

    async def count_users(self) -> int:
        row = await self.fetchone("SELECT COUNT(*) AS n FROM users")
        return int(row["n"] if row else 0)

    async def count_trades_by_status(self) -> dict[str, int]:
        rows = await self.fetchall("SELECT status, COUNT(*) AS n FROM trades GROUP BY status")
        return {row["status"]: int(row["n"]) for row in rows}

    async def insert_fee_ledger(self, row: dict) -> None:
        """Audit row for the bot fee charged on an executed sell swap."""
        now = utcnow()
        await self.execute(
            """
            INSERT INTO fee_ledger (
                trade_id, side, token_mint, fee_mint, fee_bps,
                fee_amount_raw, fee_value_sol, volume_sol, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["trade_id"],
                row.get("side", "sell"),
                row["token_mint"],
                row.get("fee_mint"),
                row.get("fee_bps"),
                None if row.get("fee_amount_raw") is None else str(row["fee_amount_raw"]),
                row.get("fee_value_sol"),
                row.get("volume_sol"),
                now,
                now,
            ),
        )

    async def fee_ledger_totals(self) -> list[dict]:
        """Accumulated fees grouped by fee mint, with the SOL value where known."""
        return await self.fetchall(
            """
            SELECT fee_mint, COUNT(*) AS trades,
                   SUM(CASE WHEN fee_value_sol IS NOT NULL THEN fee_value_sol ELSE 0 END) AS value_sol
            FROM fee_ledger
            GROUP BY fee_mint
            ORDER BY trades DESC
            """,
        )

    async def fee_ledger_recent(self, limit: int = 10) -> list[dict]:
        return await self.fetchall(
            "SELECT * FROM fee_ledger ORDER BY created_at DESC, id DESC LIMIT ?",
            (int(limit),),
        )

    async def find_idempotency(self, key: str) -> dict | None:
        return await self.fetchone(
            "SELECT id, status FROM trades WHERE idempotency_key=?",
            (key,),
        )

    # ------------------------------------------------------------------
    # Trade triggers: user-armed limit orders and trailing stops that a
    # background monitor evaluates against live prices.
    # ------------------------------------------------------------------

    def add_trade_trigger_sync(
        self,
        *,
        telegram_id: str,
        kind: str,
        mint: str,
        target_price_usd: float | None = None,
        trail_distance_pct: float | None = None,
        amount_sol: float | None = None,
        sell_percent: float | None = None,
    ) -> int:
        now = utcnow()
        self.execute_sync(
            """
            INSERT INTO trade_triggers (
                telegram_id, kind, token_mint, target_price_usd, trail_distance_pct,
                peak_price_usd, amount_sol, sell_percent, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, 'open', ?, ?)
            """,
            (telegram_id, kind, mint, target_price_usd, trail_distance_pct, amount_sol, sell_percent, now, now),
        )
        row = self.fetchone_sync(
            "SELECT id FROM trade_triggers WHERE telegram_id=? ORDER BY id DESC LIMIT 1",
            (telegram_id,),
        )
        return int(row["id"]) if row else 0

    async def add_trade_trigger(self, **kwargs) -> int:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, partial(self.add_trade_trigger_sync, **kwargs))

    async def open_triggers(self) -> list[dict]:
        """Open triggers with the user row and settings attached, ready to fire."""
        return await self.fetchall(
            """
            SELECT tt.id, tt.telegram_id, tt.kind, tt.token_mint, tt.target_price_usd,
                   tt.trail_distance_pct, tt.peak_price_usd, tt.amount_sol, tt.sell_percent,
                   u.chat_id, u.wallet_address,
                   s.slippage_bps, s.max_sol_per_trade, s.daily_spend_limit_sol,
                   s.max_price_impact_pct, s.autosnipe_max_price_impact_pct, s.priority_fee_lamports
            FROM trade_triggers tt
            JOIN users u ON u.telegram_id = tt.telegram_id
            JOIN settings s ON s.telegram_id = tt.telegram_id
            WHERE tt.status = 'open'
              AND u.trading_paused = 0
              AND u.wallet_address IS NOT NULL
            ORDER BY tt.created_at
            """
        )

    async def triggers_for(self, telegram_id: str) -> list[dict]:
        return await self.fetchall(
            """
            SELECT id, kind, token_mint, target_price_usd, trail_distance_pct,
                   peak_price_usd, amount_sol, sell_percent, status, trigger_error,
                   created_at, updated_at
            FROM trade_triggers
            WHERE telegram_id=?
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (telegram_id,),
        )

    async def set_trigger_status(
        self, trigger_id: int, status: str, *, error: str | None = None, trade_id: str | None = None
    ) -> None:
        await self.execute(
            "UPDATE trade_triggers SET status=?, trigger_error=?, trade_id=?, updated_at=? WHERE id=?",
            (status, error, trade_id, utcnow(), trigger_id),
        )

    async def update_trigger_peak(self, trigger_id: int, peak_price_usd: float) -> None:
        await self.execute(
            "UPDATE trade_triggers SET peak_price_usd=?, updated_at=? WHERE id=?",
            (peak_price_usd, utcnow(), trigger_id),
        )

    async def update_trigger_distance(self, trigger_id: int, telegram_id: str, distance_pct: float) -> bool:
        row = await self.fetchone(
            "SELECT id FROM trade_triggers WHERE id=? AND telegram_id=? AND kind='trailing_stop' AND status='open'",
            (trigger_id, telegram_id),
        )
        if not row:
            return False
        await self.execute(
            "UPDATE trade_triggers SET trail_distance_pct=?, updated_at=? WHERE id=?",
            (distance_pct, utcnow(), trigger_id),
        )
        return True

    async def cancel_trigger(self, trigger_id: int, telegram_id: str) -> bool:
        row = await self.fetchone(
            "SELECT id FROM trade_triggers WHERE id=? AND telegram_id=? AND status='open'",
            (trigger_id, telegram_id),
        )
        if not row:
            return False
        await self.execute(
            "UPDATE trade_triggers SET status='cancelled', updated_at=? WHERE id=?",
            (utcnow(), trigger_id),
        )
        return True

    async def count_triggers_for(self, telegram_id: str) -> int:
        row = await self.fetchone(
            "SELECT COUNT(*) AS n FROM trade_triggers WHERE telegram_id=? AND status='open'",
            (telegram_id,),
        )
        return int(row["n"] if row else 0)


class PathText:
    @staticmethod
    def read(path) -> str:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
