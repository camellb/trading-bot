"""
User Controls — granular runtime controls for the Polymarket bot.

Provides:
    1. Global pause / resume — halt all new position opening.
    2. Market blocklist — never trade specific markets.
    3. Archetype pause — stop trading entire archetypes.
    4. Market watchlist — track markets without trading.
    5. Priority markets — force-include in every scan.

All state is persisted in PostgreSQL (via the shared engine) so it
survives bot restarts. Methods are synchronous — call via
asyncio.to_thread / run_in_executor from async code.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

from db.engine import get_engine


class UserControls:
    """Singleton-safe controller for user-facing bot overrides."""

    def __init__(self):
        self._tables_created = False

    # ── Schema bootstrap ────────────────────────────────────────────────────
    def _ensure_tables(self) -> None:
        if self._tables_created:
            return
        try:
            with get_engine().begin() as conn:
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS bot_controls (
                        id             SERIAL PRIMARY KEY,
                        control_type   TEXT NOT NULL,
                        control_key    TEXT NOT NULL DEFAULT '',
                        enabled        BOOLEAN NOT NULL DEFAULT TRUE,
                        reason         TEXT,
                        created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (control_type, control_key)
                    )
                """))
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS market_blocklist (
                        id          SERIAL PRIMARY KEY,
                        market_id   TEXT NOT NULL UNIQUE,
                        question    TEXT,
                        reason      TEXT,
                        blocked_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """))
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS market_watchlist (
                        id          SERIAL PRIMARY KEY,
                        market_id   TEXT NOT NULL UNIQUE,
                        question    TEXT,
                        notes       TEXT DEFAULT '',
                        added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """))
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS priority_markets (
                        id          SERIAL PRIMARY KEY,
                        market_id   TEXT NOT NULL UNIQUE,
                        question    TEXT,
                        reason      TEXT,
                        added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """))
            self._tables_created = True
        except Exception as exc:
            print(f"[user_controls] table creation failed: {exc}",
                  file=sys.stderr)
            raise

    # ── 1. Global Pause / Resume ────────────────────────────────────────────
    def pause_trading(self, reason: str = "") -> None:
        """Stop all new position opening. Existing positions still resolve."""
        self._ensure_tables()
        with get_engine().begin() as conn:
            conn.execute(text("""
                INSERT INTO bot_controls (control_type, control_key, enabled, reason, updated_at)
                VALUES ('global_pause', '', TRUE, :reason, NOW())
                ON CONFLICT (control_type, control_key)
                DO UPDATE SET enabled = TRUE, reason = :reason, updated_at = NOW()
            """), {"reason": reason})

    def resume_trading(self) -> None:
        """Resume normal operation."""
        self._ensure_tables()
        with get_engine().begin() as conn:
            conn.execute(text("""
                INSERT INTO bot_controls (control_type, control_key, enabled, reason, updated_at)
                VALUES ('global_pause', '', FALSE, NULL, NOW())
                ON CONFLICT (control_type, control_key)
                DO UPDATE SET enabled = FALSE, reason = NULL, updated_at = NOW()
            """))

    def is_paused(self) -> tuple[bool, Optional[str]]:
        """Check if trading is globally paused. Returns (paused, reason)."""
        self._ensure_tables()
        with get_engine().begin() as conn:
            row = conn.execute(text("""
                SELECT enabled, reason FROM bot_controls
                WHERE control_type = 'global_pause' AND control_key = ''
            """)).fetchone()
        if row is None:
            return (False, None)
        return (bool(row[0]), row[1])

    # ── 2. Market Blocklist ─────────────────────────────────────────────────
    def block_market(self, market_id: str, reason: str = "",
                     question: str = "") -> None:
        """Never trade this specific market."""
        self._ensure_tables()
        with get_engine().begin() as conn:
            conn.execute(text("""
                INSERT INTO market_blocklist (market_id, question, reason)
                VALUES (:mid, :q, :reason)
                ON CONFLICT (market_id)
                DO UPDATE SET reason = :reason, question = :q, blocked_at = NOW()
            """), {"mid": market_id, "q": question, "reason": reason})

    def unblock_market(self, market_id: str) -> None:
        """Remove a market from the blocklist."""
        self._ensure_tables()
        with get_engine().begin() as conn:
            conn.execute(text("""
                DELETE FROM market_blocklist WHERE market_id = :mid
            """), {"mid": market_id})

    def is_blocked(self, market_id: str) -> bool:
        """Check if a market is blocked."""
        self._ensure_tables()
        with get_engine().begin() as conn:
            row = conn.execute(text("""
                SELECT 1 FROM market_blocklist WHERE market_id = :mid LIMIT 1
            """), {"mid": market_id}).fetchone()
        return row is not None

    def get_blocked_markets(self) -> list[dict]:
        """List all blocked markets with reasons."""
        self._ensure_tables()
        with get_engine().begin() as conn:
            rows = conn.execute(text("""
                SELECT market_id, question, reason, blocked_at
                FROM market_blocklist ORDER BY blocked_at DESC
            """)).fetchall()
        return [
            {
                "market_id":  r[0],
                "question":   r[1],
                "reason":     r[2],
                "blocked_at": r[3].isoformat() if r[3] else None,
            }
            for r in rows
        ]

    # ── 3. Archetype Pause ──────────────────────────────────────────────────
    def pause_archetype(self, archetype: str, reason: str = "") -> None:
        """Stop trading all markets of this archetype."""
        self._ensure_tables()
        with get_engine().begin() as conn:
            conn.execute(text("""
                INSERT INTO bot_controls (control_type, control_key, enabled, reason, updated_at)
                VALUES ('archetype_pause', :arch, TRUE, :reason, NOW())
                ON CONFLICT (control_type, control_key)
                DO UPDATE SET enabled = TRUE, reason = :reason, updated_at = NOW()
            """), {"arch": archetype, "reason": reason})

    def resume_archetype(self, archetype: str) -> None:
        """Resume trading for an archetype."""
        self._ensure_tables()
        with get_engine().begin() as conn:
            conn.execute(text("""
                INSERT INTO bot_controls (control_type, control_key, enabled, reason, updated_at)
                VALUES ('archetype_pause', :arch, FALSE, NULL, NOW())
                ON CONFLICT (control_type, control_key)
                DO UPDATE SET enabled = FALSE, reason = NULL, updated_at = NOW()
            """), {"arch": archetype})

    def is_archetype_paused(self, archetype: str) -> tuple[bool, Optional[str]]:
        """Check if an archetype is paused. Returns (paused, reason)."""
        self._ensure_tables()
        with get_engine().begin() as conn:
            row = conn.execute(text("""
                SELECT enabled, reason FROM bot_controls
                WHERE control_type = 'archetype_pause' AND control_key = :arch
            """), {"arch": archetype}).fetchone()
        if row is None:
            return (False, None)
        return (bool(row[0]), row[1])

    def get_paused_archetypes(self) -> list[dict]:
        """List all paused archetypes with reasons."""
        self._ensure_tables()
        with get_engine().begin() as conn:
            rows = conn.execute(text("""
                SELECT control_key, reason, updated_at FROM bot_controls
                WHERE control_type = 'archetype_pause' AND enabled = TRUE
                ORDER BY updated_at DESC
            """)).fetchall()
        return [
            {
                "archetype":  r[0],
                "reason":     r[1],
                "paused_at":  r[2].isoformat() if r[2] else None,
            }
            for r in rows
        ]

    # ── 4. Market Watchlist ─────────────────────────────────────────────────
    def add_to_watchlist(self, market_id: str, question: str,
                         notes: str = "") -> None:
        """Track a market without trading it."""
        self._ensure_tables()
        with get_engine().begin() as conn:
            conn.execute(text("""
                INSERT INTO market_watchlist (market_id, question, notes)
                VALUES (:mid, :q, :notes)
                ON CONFLICT (market_id)
                DO UPDATE SET question = :q, notes = :notes, added_at = NOW()
            """), {"mid": market_id, "q": question, "notes": notes})

    def remove_from_watchlist(self, market_id: str) -> None:
        """Remove a market from the watchlist."""
        self._ensure_tables()
        with get_engine().begin() as conn:
            conn.execute(text("""
                DELETE FROM market_watchlist WHERE market_id = :mid
            """), {"mid": market_id})

    def get_watchlist(self) -> list[dict]:
        """All watchlisted markets."""
        self._ensure_tables()
        with get_engine().begin() as conn:
            rows = conn.execute(text("""
                SELECT market_id, question, notes, added_at
                FROM market_watchlist ORDER BY added_at DESC
            """)).fetchall()
        return [
            {
                "market_id": r[0],
                "question":  r[1],
                "notes":     r[2],
                "added_at":  r[3].isoformat() if r[3] else None,
            }
            for r in rows
        ]

    # ── 5. Priority Markets ────────────────────────────────────────────────
    def add_priority_market(self, market_id: str, question: str,
                            reason: str = "") -> None:
        """Evaluate this market on every scan regardless of filters."""
        self._ensure_tables()
        with get_engine().begin() as conn:
            conn.execute(text("""
                INSERT INTO priority_markets (market_id, question, reason)
                VALUES (:mid, :q, :reason)
                ON CONFLICT (market_id)
                DO UPDATE SET question = :q, reason = :reason, added_at = NOW()
            """), {"mid": market_id, "q": question, "reason": reason})

    def remove_priority_market(self, market_id: str) -> None:
        """Remove from priority list."""
        self._ensure_tables()
        with get_engine().begin() as conn:
            conn.execute(text("""
                DELETE FROM priority_markets WHERE market_id = :mid
            """), {"mid": market_id})

    def get_priority_markets(self) -> list[dict]:
        """List all priority markets."""
        self._ensure_tables()
        with get_engine().begin() as conn:
            rows = conn.execute(text("""
                SELECT market_id, question, reason, added_at
                FROM priority_markets ORDER BY added_at DESC
            """)).fetchall()
        return [
            {
                "market_id": r[0],
                "question":  r[1],
                "reason":    r[2],
                "added_at":  r[3].isoformat() if r[3] else None,
            }
            for r in rows
        ]
