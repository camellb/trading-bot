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
from typing import Optional

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

    # ── Polymarket position entries ───────────────────────────────────────────

    def log_pm_entry(
        self,
        market,
        evaluation,
        decision,
        position_id: int,
        research=None,
    ) -> "Path":
        """
        Write a markdown note for a newly-opened Polymarket position.

        Called by engine.pm_analyst.PMAnalyst after executor.open_position
        succeeds. `market` is feeds.polymarket_feed.PolyMarket, `evaluation`
        is engine.polymarket_evaluator.MarketEvaluation, `decision` is
        execution.pm_sizer.SizingDecision, `research` is the
        research.fetcher.ResearchBundle.

        File layout mirrors the crypto trade notes:
            trades/{YYYY-MM-DD}-pm-{market_id[:8]}-{side}-{position_id}-OPEN.md
        """
        now      = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M UTC")
        mid_stub = (getattr(market, "id", "") or "")[:8] or "unknown"
        side     = getattr(decision, "side", "?")

        fname = f"{date_str}-pm-{mid_stub}-{side}-{position_id}-OPEN.md"
        fpath = self.vault_path / "trades" / fname

        key_factors = getattr(evaluation, "key_factors", []) or []
        factors_md = "\n".join(f"- {f}" for f in key_factors) or "- (none recorded)"

        research_md = ""
        if research is not None:
            sources = getattr(research, "sources", []) or []
            if sources:
                research_md = "\n".join(f"- {s}" for s in sources[:10])

        end_date = (getattr(market, "end_date_iso", None) or now).strftime("%Y-%m-%d")
        edge_bps = getattr(decision, "edge", 0.0) * 10_000

        content = f"""# Polymarket {side} — {date_str} {time_str}

## Market
- Question: {getattr(market, 'question', '')}
- Market ID: {getattr(market, 'id', '')}
- Category: {getattr(evaluation, 'category', 'other')}
- Volume 24h: ${getattr(market, 'volume_24h_clob', 0):,.0f}
- Resolves: {end_date}

## Entry
- Side: {side}
- Entry price: {getattr(decision, 'entry_price', 0):.3f}
- Shares: {getattr(decision, 'shares', 0):.2f}
- Stake: ${getattr(decision, 'stake_usd', 0):.2f}
- Claude p(YES): {getattr(evaluation, 'probability_yes', 0):.3f}
- Market p(YES): {getattr(market, 'yes_price', 0):.3f}
- Edge: {edge_bps:.0f} bps
- Confidence: {getattr(evaluation, 'confidence', 0):.2f}
- Position ID: #{position_id}

## Key Factors
{factors_md}

## Claude Reasoning
{getattr(evaluation, 'reasoning', '') or '(no reasoning recorded)'}

## Research Sources
{research_md or '- (no research sources recorded)'}

## Status: OPEN
"""
        fpath.write_text(content, encoding="utf-8")
        return fpath

    def log_pm_settlement(
        self,
        position_id: int,
        market_id: str,
        question: str,
        side: str,
        outcome: str,
        pnl_usd: float,
        opened_at: Optional[datetime] = None,
    ) -> "Path":
        """
        Rename the OPEN note for a settled PM position to WIN/LOSS/INVALID
        and append the outcome block.
        """
        mid_stub = (market_id or "")[:8] or "unknown"
        date_str = (opened_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
        open_name = f"{date_str}-pm-{mid_stub}-{side}-{position_id}-OPEN.md"
        open_path = self.vault_path / "trades" / open_name
        verdict = "INVALID" if outcome == "INVALID" else ("WIN" if pnl_usd >= 0 else "LOSS")
        new_name = f"{date_str}-pm-{mid_stub}-{side}-{position_id}-{verdict}.md"
        new_path = self.vault_path / "trades" / new_name

        settled_block = f"""
## Outcome
- Resolution: {outcome}
- P&L: {pnl_usd:+.2f} USD
- Settled at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

## Status: {verdict}
"""
        if open_path.exists():
            existing = open_path.read_text(encoding="utf-8")
            updated  = existing.replace("## Status: OPEN", "").rstrip() + "\n" + settled_block
            new_path.write_text(updated, encoding="utf-8")
            if open_path != new_path:
                open_path.unlink()
        else:
            new_path.write_text(
                f"# Polymarket {side} — {question[:80]} (settlement only)\n{settled_block}",
                encoding="utf-8",
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
