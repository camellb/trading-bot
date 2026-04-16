"""
MemoryManager — reads and writes the Obsidian markdown vault that gives
Claude persistent memory across sessions.

Vault layout
------------
~/Documents/trading-bot-memory/
├── trades/                     one .md file per trade
├── market-context/             daily briefs
├── strategy/
│   ├── what-works.md
│   ├── what-doesnt-work.md
│   └── current-thesis.md
├── patterns/
└── performance/
    ├── weekly-reviews/
    └── monthly-reviews/

File naming
-----------
trades/  {YYYY-MM-DD}-{pair}-{direction}-{OPEN|WIN|LOSS}.md
"""

from datetime import datetime, timezone
from pathlib import Path

import config


class MemoryManager:
    """
    All vault I/O is synchronous (fast local filesystem reads/writes).
    Call from async code with asyncio.to_thread() if needed.
    """

    def __init__(self) -> None:
        self.vault_path = Path(config.OBSIDIAN_VAULT_PATH).expanduser()
        self._ensure_structure()

    # ── Initialisation ────────────────────────────────────────────────────────

    def _ensure_structure(self) -> None:
        """Create the full vault directory tree on first run."""
        for subdir in [
            "trades",
            "market-context",
            "strategy",
            "patterns",
            "performance/weekly-reviews",
            "performance/monthly-reviews",
        ]:
            (self.vault_path / subdir).mkdir(parents=True, exist_ok=True)

        # Seed empty strategy files so read_strategy_memory() always works
        for fname in ("what-works.md", "what-doesnt-work.md", "current-thesis.md"):
            fpath = self.vault_path / "strategy" / fname
            if not fpath.exists():
                fpath.write_text(
                    f"# {fname.replace('-', ' ').replace('.md', '').title()}\n\n"
                    "_No entries yet._\n",
                    encoding="utf-8",
                )

    # ── Trade files ───────────────────────────────────────────────────────────

    def write_trade_entry(self, trade: dict, claude_reasoning: str) -> Path:
        """
        Create a trade note when a position is opened.

        Required keys in trade: pair, direction, entry_price, size_usd,
        stop_loss, take_profit, trigger_event.
        Optional: btc_price, eth_price, iv, funding, macro_sentiment.

        Returns the Path of the created file.
        """
        now      = datetime.now(timezone.utc)
        date     = now.strftime("%Y-%m-%d")
        time     = now.strftime("%H:%M UTC")
        pair     = trade["pair"]
        dirn     = trade["direction"]
        trade_id = trade.get("id") or trade.get("trade_id")
        id_str   = f"-{trade_id}" if trade_id else ""

        fname = f"{date}-{pair}-{dirn}{id_str}-OPEN.md"
        fpath = self.vault_path / "trades" / fname

        playbook         = trade.get("playbook")
        time_horizon_days = trade.get("time_horizon_days")
        catalyst         = trade.get("catalyst")
        invalidation     = trade.get("invalidation")
        primary_signal   = trade.get("primary_signal")
        risk_reward      = trade.get("risk_reward")
        market_condition = trade.get("market_condition")

        rr_str = f"{risk_reward:.2f}" if risk_reward is not None else "N/A"
        horizon_str = f"{time_horizon_days}d" if time_horizon_days is not None else "N/A"

        content = f"""# {pair} {dirn} — {date} {time}

## Entry
- Price: {trade.get('entry_price', 'N/A')}
- Size: ${trade.get('size_usd', 0):.2f}
- Stop: {trade.get('stop_loss', 'N/A')}
- Target: {trade.get('take_profit', 'N/A')}
- Trigger: {trade.get('trigger_event', 'N/A')}

## Trade Details
- Playbook: {playbook or 'N/A'}
- Time horizon: {horizon_str}
- Catalyst: {catalyst or 'N/A'}
- Invalidation: {invalidation or 'N/A'}
- Primary signal: {primary_signal or 'N/A'}
- Risk/reward: {rr_str}
- Market condition: {market_condition or 'N/A'}

## Claude's Reasoning
{claude_reasoning}

## Market Context at Entry
- BTC Price: {trade.get('btc_price', 'N/A')}
- ETH Price: {trade.get('eth_price', 'N/A')}
- BTC IV: {trade.get('iv', 'N/A')}
- Funding: {trade.get('funding', 'N/A')}
- Macro: {trade.get('macro_sentiment', 'N/A')}

## Status: OPEN
"""
        fpath.write_text(content, encoding="utf-8")
        return fpath

    def write_trade_exit(
        self,
        trade: dict,
        exit_reason: str,
        claude_post_mortem: str,
    ) -> Path:
        """
        Update an existing trade note when the position closes.

        Renames OPEN → WIN or LOSS based on pnl_usd.
        Appends exit block and post-mortem to the file.

        Required keys in trade: pair, direction, entry_price (or opened_at
        for the date), exit_price, pnl_usd.  Optional: size_usd, opened_at.
        """
        pair     = trade["pair"]
        dirn     = trade["direction"]
        pnl      = trade.get("pnl_usd", 0.0)
        trade_id = trade.get("id") or trade.get("trade_id")
        id_str   = f"-{trade_id}" if trade_id else ""

        # Determine the date string used when the file was opened
        if "opened_at" in trade and trade["opened_at"]:
            opened_at = trade["opened_at"]
            if isinstance(opened_at, str):
                date_str = opened_at[:10]
            else:
                date_str = opened_at.strftime("%Y-%m-%d")
        else:
            # Fall back to today if opened_at not provided
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        outcome = "WIN" if pnl >= 0 else "LOSS"

        # Prefer the new trade_id-based filename; fall back to old format for
        # files written before this fix was deployed.
        new_fname_base = f"{date_str}-{pair}-{dirn}{id_str}"
        old_fname_base = f"{date_str}-{pair}-{dirn}"
        trades_dir     = self.vault_path / "trades"

        open_path = trades_dir / f"{new_fname_base}-OPEN.md"
        if not open_path.exists():
            open_path = trades_dir / f"{old_fname_base}-OPEN.md"

        new_path = trades_dir / f"{new_fname_base}-{outcome}.md"

        # Calculate hold duration
        duration = "N/A"
        if "opened_at" in trade and trade["opened_at"] and "closed_at" in trade and trade["closed_at"]:
            try:
                opened = trade["opened_at"] if hasattr(trade["opened_at"], "timestamp") \
                         else datetime.fromisoformat(str(trade["opened_at"]))
                closed = trade["closed_at"] if hasattr(trade["closed_at"], "timestamp") \
                         else datetime.fromisoformat(str(trade["closed_at"]))
                delta = closed - opened
                hours = int(delta.total_seconds() // 3600)
                mins  = int((delta.total_seconds() % 3600) // 60)
                duration = f"{hours}h {mins}m"
            except Exception:
                pass

        exit_block = f"""
## Exit
- Exit Price: {trade.get('exit_price', 'N/A')}
- P&L: {pnl:+.2f} USD
- Held: {duration}
- Exit Reason: {exit_reason}

## Post-Mortem
{claude_post_mortem}

## Status: {outcome}
"""
        if open_path.exists():
            existing = open_path.read_text(encoding="utf-8")
            # Replace OPEN status line with the exit block
            updated = existing.replace("## Status: OPEN", "").rstrip() + "\n" + exit_block
            new_path.write_text(updated, encoding="utf-8")
            if open_path != new_path:
                open_path.unlink()
        else:
            # File not found — write a minimal standalone exit note
            new_path.write_text(
                f"# {pair} {dirn} — exit\n\n{exit_block}", encoding="utf-8"
            )

        return new_path

    # ── Daily briefs ──────────────────────────────────────────────────────────

    def write_daily_brief(self, date: str, content: str) -> None:
        """Write the daily macro briefing to market-context/{date}-daily-brief.md"""
        fpath = self.vault_path / "market-context" / f"{date}-daily-brief.md"
        fpath.write_text(content, encoding="utf-8")

    # ── Strategy memory ───────────────────────────────────────────────────────

    def read_strategy_memory(self) -> dict:
        """
        Return the three strategy files as a dict.
        Keys: what_works, what_doesnt_work, current_thesis.
        Returns empty strings for missing files.
        """
        def _read(fname: str) -> str:
            fpath = self.vault_path / "strategy" / fname
            try:
                return fpath.read_text(encoding="utf-8")
            except FileNotFoundError:
                return ""

        return {
            "what_works":       _read("what-works.md"),
            "what_doesnt_work": _read("what-doesnt-work.md"),
            "current_thesis":   _read("current-thesis.md"),
        }

    def update_strategy_memory(
        self,
        what_works: str,
        what_doesnt: str,
        current_thesis: str,
    ) -> None:
        """Overwrite all three strategy files, prepending a timestamp."""
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        pairs = [
            ("what-works.md",        what_works),
            ("what-doesnt-work.md",  what_doesnt),
            ("current-thesis.md",    current_thesis),
        ]
        for fname, content in pairs:
            fpath = self.vault_path / "strategy" / fname
            fpath.write_text(
                f"_Last updated: {now_str}_\n\n{content}", encoding="utf-8"
            )

    # ── Recent trades ─────────────────────────────────────────────────────────

    def get_recent_trades(self, days: int = 14) -> list[str]:
        """
        Return the markdown content of all trade files created within the last
        N days.  Sorted by filename (chronological).
        """
        cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
        trades_dir = self.vault_path / "trades"
        results = []
        for fpath in sorted(trades_dir.glob("*.md")):
            if fpath.stat().st_mtime >= cutoff:
                try:
                    results.append(fpath.read_text(encoding="utf-8"))
                except Exception:
                    pass
        return results

    # ── Weekly / monthly reviews ──────────────────────────────────────────────

    def write_weekly_review(self, date: str, content: str) -> None:
        """Write to performance/weekly-reviews/{date}-review.md"""
        fpath = (
            self.vault_path / "performance" / "weekly-reviews" / f"{date}-review.md"
        )
        fpath.write_text(content, encoding="utf-8")

    def write_monthly_review(self, date: str, content: str) -> None:
        """Write to performance/monthly-reviews/{date}-review.md"""
        fpath = (
            self.vault_path / "performance" / "monthly-reviews" / f"{date}-review.md"
        )
        fpath.write_text(content, encoding="utf-8")
