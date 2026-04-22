"""
Polymarket Analyst — the decision engine.

Pipeline per market:
    1. Skip if we already have an open position on this market.
    2. Skip if we've already evaluated this market in the last N days
       (cost control — Claude calls aren't free).
    3. Fetch research bundle (Wikipedia, recent news, base rates).
    4. Ask Claude for probability + confidence + category via the
       PolymarketEvaluator (now research-aware).
    5. Log a `predictions` row so the outcome feeds calibration.
    6. Log a `market_evaluations` row for the dashboard/audit trail.
    7. Run the sizer (positive-EV with flat, confidence-scaled stake).
    8. If the sizer approves and we're under the concurrent cap,
       open a shadow/live position via PMExecutor.
    9. Send Telegram notification.

The analyst never crashes the caller. Every stage is wrapped; failures
are logged and the next market proceeds.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

import calibration
import config
from db.engine import get_engine
from engine.archetype_classifier import classify_archetype
from engine.notifier_state import is_trading_paused
from engine.polymarket_evaluator import PolymarketEvaluator, MarketEvaluation
from engine.risk_manager import evaluate as evaluate_risk
from engine.user_config import get_user_config
from execution.pm_executor import PMExecutor
from execution.pm_sizer import size_position, SizingDecision
from feeds import telegram_messages as tm
from feeds.polymarket_feed import PolymarketFeed, PolyMarket
from research.fetcher import fetch_research, ResearchBundle


SOURCE = "polymarket"



@dataclass
class AnalysisOutcome:
    market_id:     str
    question:      str
    status:        str          # 'OPENED' | 'LOGGED_NO_TRADE' | 'SKIP_*' | 'ERROR'
    detail:        str
    evaluation:    Optional[MarketEvaluation] = None
    decision:      Optional[SizingDecision]   = None
    prediction_id: Optional[int]              = None
    position_id:   Optional[int]              = None


class PMAnalyst:
    def __init__(
        self,
        executor:  Optional[PMExecutor]          = None,
        evaluator: Optional[PolymarketEvaluator] = None,
        notifier:  Optional[object]              = None,
        news_feed: Optional[object]              = None,
    ):
        self.executor  = executor  or PMExecutor()
        self.evaluator = evaluator or PolymarketEvaluator()
        self.notifier  = notifier
        self.news_feed = news_feed

    # ── Single market ────────────────────────────────────────────────────────
    async def analyze_market(
        self,
        market:      PolyMarket,
        skip_existing_days: int = 3,
    ) -> AnalysisOutcome:
        q = market.question[:80]
        # 0. User has paused trading? Skip all entries; open positions
        #    continue to resolve normally (see resolver).
        if is_trading_paused():
            return AnalysisOutcome(
                market_id=market.id, question=q,
                status="SKIP_PAUSED",
                detail="trading paused via /pause",
            )

        # 1. Already holding this market?
        if self.executor.has_open_position_on_market(market.id):
            return AnalysisOutcome(
                market_id=market.id, question=q,
                status="SKIP_EXISTING_POSITION",
                detail="already holding this market",
            )

        # 2. Recently evaluated?
        if _recently_predicted(market.id, skip_existing_days):
            return AnalysisOutcome(
                market_id=market.id, question=q,
                status="SKIP_RECENT_EVAL",
                detail=f"evaluated within {skip_existing_days}d",
            )

        # 3. Research.
        try:
            research = await fetch_research(
                market.question,
                market.category_hint,
                days_to_resolution=market.days_to_end,
            )
        except Exception as exc:
            print(f"[pm_analyst] research failed for {market.id}: {exc}",
                  file=sys.stderr)
            research = ResearchBundle(question=market.question)

        research_block = research.to_prompt_block()

        # 4. Evaluate.
        try:
            evaluation = await self.evaluator.evaluate(market, research_block)
        except Exception as exc:
            print(f"[pm_analyst] evaluate failed for {market.id}: {exc}",
                  file=sys.stderr)
            return AnalysisOutcome(
                market_id=market.id, question=q,
                status="ERROR", detail=f"evaluate: {exc}",
            )
        if evaluation is None:
            return AnalysisOutcome(
                market_id=market.id, question=q,
                status="ERROR", detail="evaluator returned None",
            )

        # 5. Log prediction for calibration.
        pred_meta = {
            "market_id":               market.id,
            "condition_id":            market.condition_id,
            "question":                market.question,
            "slug":                    market.slug,
            "outcome_yes":             market.outcome_yes,
            "outcome_no":              market.outcome_no,
            "yes_price_at_prediction": market.yes_price,
            "volume_24h":              market.volume_24h_clob,
            "end_date":                market.end_date_iso.isoformat(),
            "category_hint":           market.category_hint,
            "research_sources":        research.sources,
            "research_keywords":       research.keywords,
            "key_factors":             evaluation.key_factors,
        }
        prediction_id = calibration.log_prediction(
            source        = SOURCE,
            subject_key   = f"polymarket:{market.id}",
            probability   = evaluation.probability_yes,
            category      = evaluation.category,
            confidence    = evaluation.confidence,
            horizon_hours = market.days_to_end * 24.0,
            reasoning     = evaluation.reasoning,
            metadata      = pred_meta,
        )
        if prediction_id is None or prediction_id <= 0:
            print(f"[pm_analyst] prediction log failed for {market.id} — "
                  f"skipping trade (no calibration link)", file=sys.stderr)
            prediction_id = None
            return AnalysisOutcome(
                market_id=market.id, question=q,
                status="ERROR", detail="prediction log failed — no calibration link",
                evaluation=evaluation,
            )

        # 6. Size.
        user_config = get_user_config()
        bankroll = self.executor.get_bankroll()
        starting_cash = self.executor.get_starting_cash()
        verdict = evaluate_risk(
            user_config=user_config, bankroll=bankroll,
            starting_cash=starting_cash, mode=self.executor.mode,
        )
        if verdict.halted:
            return AnalysisOutcome(
                market_id=market.id, question=q,
                status="SKIP_RISK_HALT",
                detail=verdict.halt_reason or "risk breaker tripped",
                evaluation=evaluation, prediction_id=prediction_id,
            )

        archetype = classify_archetype(
            market.question,
            category=evaluation.category,
            event_slug=getattr(market, "event_slug", None),
        )

        decision = size_position(
            claude_p    = evaluation.probability_yes,
            confidence  = evaluation.confidence,
            ask_yes     = market.yes_price,
            ask_no      = market.no_price,
            bankroll    = verdict.effective_bankroll,
            user_config = user_config,
            archetype   = archetype,
        )

        # Streak-cooldown halves the stake without changing the EV side choice.
        if decision.should_trade and verdict.stake_multiplier != 1.0:
            decision.stake_usd *= verdict.stake_multiplier
            decision.shares    *= verdict.stake_multiplier
            if decision.stake_usd < 2.0:
                decision.skip_reason = (
                    f"streak cooldown ({verdict.notes}) halved stake below "
                    f"$2.00 minimum — skipping"
                )
                decision.stake_usd = 0.0
                decision.shares    = 0.0

        # Log a market_evaluations row regardless of trade outcome — this is
        # the auditing surface for the dashboard.
        recommendation = (
            f"BUY_{decision.side}" if decision.should_trade else "SKIP"
        )
        eval_row_id = _log_market_evaluation(
            market=market,
            evaluation=evaluation,
            decision=decision,
            research_sources=research.sources,
            prediction_id=prediction_id,
            recommendation=recommendation,
            market_archetype=archetype,
        )

        # 7. Trade gating.
        if not decision.should_trade:
            return AnalysisOutcome(
                market_id=market.id, question=q,
                status="LOGGED_NO_TRADE",
                detail=decision.skip_reason or "sizer declined",
                evaluation=evaluation, decision=decision,
                prediction_id=prediction_id,
            )

        max_concurrent = int(getattr(config, "PM_MAX_CONCURRENT_POSITIONS", 10))
        if self.executor.open_position_count() >= max_concurrent:
            return AnalysisOutcome(
                market_id=market.id, question=q,
                status="SKIP_MAX_CONCURRENT",
                detail=f"{max_concurrent} positions already open",
                evaluation=evaluation, decision=decision,
                prediction_id=prediction_id,
            )

        # 7b. Event-group correlation cap.
        max_per_event = int(getattr(config, "PM_MAX_PER_EVENT", 3))
        if market.event_slug:
            event_count = self.executor.count_positions_for_event(market.event_slug)
            if event_count >= max_per_event:
                return AnalysisOutcome(
                    market_id=market.id, question=q,
                    status="SKIP_EVENT_CAP",
                    detail=f"{event_count}/{max_per_event} positions in event '{market.event_slug}'",
                    evaluation=evaluation, decision=decision,
                    prediction_id=prediction_id,
                )

        # 8. Open position.
        pos_id = self.executor.open_position(
            market=market, decision=decision,
            claude_probability=evaluation.probability_yes,
            prediction_id=prediction_id,
            reasoning=evaluation.reasoning,
            category=evaluation.category,
            market_archetype=archetype,
        )
        if pos_id is None:
            return AnalysisOutcome(
                market_id=market.id, question=q,
                status="ERROR", detail="executor returned None",
                evaluation=evaluation, decision=decision,
                prediction_id=prediction_id,
            )

        # Link the evaluation row to the position for dashboard joins.
        _link_evaluation_to_position(eval_row_id, pos_id)

        # 9. Side effects: Telegram.
        if self.notifier is not None:
            try:
                await self._notify_open(market, evaluation, decision, pos_id)
            except Exception as exc:
                print(f"[pm_analyst] notify failed: {exc}", file=sys.stderr)

        return AnalysisOutcome(
            market_id=market.id, question=q,
            status="OPENED",
            detail=f"pm_position={pos_id} side={decision.side} "
                   f"stake=${decision.stake_usd:.2f} "
                   f"forecast_p={decision.p_win:.2f} conf={decision.confidence:.2f}",
            evaluation=evaluation, decision=decision,
            prediction_id=prediction_id, position_id=pos_id,
        )

    # ── Batch scan ───────────────────────────────────────────────────────────
    async def scan_and_analyze(
        self,
        limit:          int   = 20,
        min_volume_24h: float = 5_000.0,
    ) -> dict:
        """
        Fetch candidate markets and run the analyst pipeline on each.
        Returns a summary dict suitable for scheduler logging and /status.
        """
        skip_days = int(getattr(config, "PM_SKIP_EXISTING_DAYS", 3))
        min_days  = int(getattr(config, "PM_MIN_DAYS_TO_END", 0))
        max_days  = int(getattr(config, "PM_MAX_DAYS_TO_END", 90))

        summary = {
            "fetched":     0,
            "analyzed":    0,
            "opened":      0,
            "no_trade":    0,
            "skipped":     0,
            "errors":      0,
            "outcomes":    [],
        }

        async with PolymarketFeed() as feed:
            try:
                markets = await feed.fetch_candidate_markets(
                    limit=limit, min_volume_24h=min_volume_24h,
                    min_days=min_days, max_days=max_days,
                )
            except Exception as exc:
                print(f"[pm_analyst] fetch candidates failed: {exc}",
                      file=sys.stderr)
                summary["errors"] += 1
                return summary

            summary["fetched"] = len(markets)
            if not markets:
                return summary

            for mk in markets:
                try:
                    outcome = await self.analyze_market(mk, skip_existing_days=skip_days)
                except Exception as exc:
                    print(f"[pm_analyst] unexpected failure {mk.id}: {exc}",
                          file=sys.stderr)
                    summary["errors"] += 1
                    continue

                summary["analyzed"] += 1
                summary["outcomes"].append({
                    "market_id": outcome.market_id,
                    "question":  outcome.question,
                    "status":    outcome.status,
                    "detail":    outcome.detail,
                })
                if outcome.status == "OPENED":
                    summary["opened"] += 1
                elif outcome.status == "LOGGED_NO_TRADE":
                    summary["no_trade"] += 1
                elif outcome.status.startswith("SKIP"):
                    summary["skipped"] += 1
                elif outcome.status == "ERROR":
                    summary["errors"] += 1

        print(f"[pm_analyst] scan complete: "
              f"fetched={summary['fetched']} "
              f"opened={summary['opened']} "
              f"no_trade={summary['no_trade']} "
              f"skipped={summary['skipped']} "
              f"errors={summary['errors']}", flush=True)
        return summary

    # ── Notifications ────────────────────────────────────────────────────────
    async def _notify_open(self, market: PolyMarket,
                            evaluation: MarketEvaluation,
                            decision: SizingDecision,
                            position_id: int) -> None:
        if self.notifier is None or not hasattr(self.notifier, "send"):
            return
        bankroll_after = 0.0
        try:
            bankroll_after = float(self.executor.get_bankroll())
        except Exception:
            pass
        # `forecast_pct` is Delfi's probability for the side we're buying.
        forecast_pct = decision.p_win * 100.0
        msg = tm.new_position(
            question=market.question,
            side=decision.side,
            entry_cents=decision.entry_price * 100.0,
            stake_usd=decision.stake_usd,
            shares=decision.shares,
            forecast_pct=forecast_pct,
            confidence=evaluation.confidence,
            bankroll_after=bankroll_after,
            resolve_date=market.end_date_iso.strftime("%Y-%m-%d"),
            mode="live" if self.executor.mode == "live" else "simulation",
        )
        try:
            await self.notifier.send(msg)
        except Exception as exc:
            print(f"[pm_analyst] telegram send failed: {exc}", file=sys.stderr)


# ── Helpers ──────────────────────────────────────────────────────────────────
def _recently_predicted(market_id: str, days: int) -> bool:
    try:
        with get_engine().begin() as conn:
            row = conn.execute(text(
                "SELECT 1 FROM predictions "
                "WHERE source = :src "
                "  AND subject_key = :sk "
                "  AND created_at >= NOW() - (:d || ' days')::interval "
                "LIMIT 1"
            ), {
                "src": SOURCE,
                "sk":  f"polymarket:{market_id}",
                "d":   str(int(days)),
            }).fetchone()
            return row is not None
    except Exception as exc:
        print(f"[pm_analyst] recency check failed: {exc}", file=sys.stderr)
        return False


def _log_market_evaluation(
    market:           PolyMarket,
    evaluation:       MarketEvaluation,
    decision:         SizingDecision,
    research_sources: list[str],
    prediction_id:    Optional[int],
    recommendation:   str,
    market_archetype: Optional[str] = None,
) -> Optional[int]:
    try:
        with get_engine().begin() as conn:
            row = conn.execute(text(
                "INSERT INTO market_evaluations ("
                "  market_id, condition_id, slug, question, category, "
                "  market_price_yes, claude_probability, confidence, "
                "  ev_bps, recommendation, reasoning, research_sources, "
                "  prediction_id, market_archetype, event_slug"
                ") VALUES ("
                "  :mid, :cid, :slug, :q, :cat, "
                "  :mp, :cp, :conf, "
                "  :ev_bps, :rec, :reason, :srcs, "
                "  :pid, :arch, :event_slug"
                ") RETURNING id"
            ), {
                "mid":  market.id,
                "cid":  market.condition_id,
                "slug": market.slug,
                "q":    market.question,
                "cat":  evaluation.category,
                "mp":   market.yes_price,
                "cp":   evaluation.probability_yes,
                "conf": evaluation.confidence,
                "ev_bps": decision.ev * 10_000.0,
                "rec":  recommendation,
                "reason": evaluation.reasoning,
                "srcs": json.dumps(research_sources),
                "pid":  prediction_id,
                "arch": market_archetype,
                "event_slug": getattr(market, "event_slug", None),
            }).fetchone()
            return int(row[0]) if row else None
    except Exception as exc:
        print(f"[pm_analyst] log_market_evaluation failed: {exc}", file=sys.stderr)
        return None


def _link_evaluation_to_position(eval_row_id: Optional[int],
                                  position_id: int) -> None:
    if eval_row_id is None:
        return
    try:
        with get_engine().begin() as conn:
            conn.execute(text(
                "UPDATE market_evaluations SET pm_position_id = :pos "
                "WHERE id = :eid"
            ), {"pos": position_id, "eid": eval_row_id})
    except Exception as exc:
        print(f"[pm_analyst] link evaluation failed: {exc}", file=sys.stderr)


# ── CLI ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(override=True)
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--min-volume", type=float, default=5_000.0)
    args = ap.parse_args()

    async def _main():
        analyst = PMAnalyst()
        summary = await analyst.scan_and_analyze(limit=args.limit,
                                                   min_volume_24h=args.min_volume)
        print(json.dumps(summary, indent=2, default=str))

    asyncio.run(_main())
