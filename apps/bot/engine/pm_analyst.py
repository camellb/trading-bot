"""
Polymarket Analyst - the decision engine.

Pipeline per market:
    1. Skip if we already have an open position on this market.
    2. Skip if we've already evaluated this market in the last N days
       (cost control - Claude calls aren't free).
    3. Fetch research bundle (Wikipedia, recent news, base rates).
    4. Ask Claude for probability + confidence + category via the
       PolymarketEvaluator (now research-aware).
    5. Log a `predictions` row so the outcome feeds calibration.
    6. Log a `market_evaluations` row for the dashboard/audit trail.
    7. Run the sizer (positive-EV with flat, confidence-scaled stake).
    8. If the sizer approves and we're under the concurrent cap,
       open a simulation/live position via PMExecutor.
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
from engine.user_config import (
    get_user_config,
    list_onboarded_user_ids,
)
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
        evaluator: Optional[PolymarketEvaluator] = None,
        notifier:  Optional[object]              = None,
        news_feed: Optional[object]              = None,
    ):
        self.evaluator = evaluator or PolymarketEvaluator()
        self.notifier  = notifier
        self.news_feed = news_feed

    # ── Shared evaluation phase ──────────────────────────────────────────────
    async def _shared_evaluate(
        self,
        market: PolyMarket,
    ) -> Optional[tuple[MarketEvaluation, ResearchBundle, int]]:
        """
        Do the cost-intensive work that's identical for every user: research,
        Claude evaluation, and logging the `predictions` row that anchors
        calibration. Returns (evaluation, research, prediction_id) on success
        or None on any failure / skip.
        """
        try:
            research = await fetch_research(
                market.question, market.category_hint,
                days_to_resolution=market.days_to_end,
            )
        except Exception as exc:
            print(f"[pm_analyst] research failed for {market.id}: {exc}",
                  file=sys.stderr)
            research = ResearchBundle(question=market.question)
        research_block = research.to_prompt_block()

        try:
            evaluation = await self.evaluator.evaluate(market, research_block)
        except Exception as exc:
            print(f"[pm_analyst] evaluate failed for {market.id}: {exc}",
                  file=sys.stderr)
            return None
        if evaluation is None:
            print(f"[pm_analyst] evaluator returned None for {market.id}",
                  file=sys.stderr)
            return None

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
            print(f"[pm_analyst] prediction log failed for {market.id} - "
                  f"skipping trade (no calibration link)", file=sys.stderr)
            return None

        return evaluation, research, int(prediction_id)

    # ── Per-user trading phase ───────────────────────────────────────────────
    async def _maybe_trade_for_user(
        self,
        market:        PolyMarket,
        evaluation:    MarketEvaluation,
        research:      ResearchBundle,
        prediction_id: int,
        user_id:       str,
    ) -> AnalysisOutcome:
        q = market.question[:80]
        executor = PMExecutor(user_id)
        if not executor.ready:
            return AnalysisOutcome(
                market_id=market.id, question=q,
                status="SKIP_USER_NOT_READY",
                detail=f"user {user_id} not onboarded - no mode or starting_cash",
                evaluation=evaluation, prediction_id=prediction_id,
            )

        user_config = get_user_config(user_id)
        if not user_config.bot_enabled:
            return AnalysisOutcome(
                market_id=market.id, question=q,
                status="SKIP_BOT_DISABLED",
                detail=f"user {user_id} has not enabled the bot yet",
                evaluation=evaluation, prediction_id=prediction_id,
            )

        if executor.has_open_position_on_market(market.id):
            return AnalysisOutcome(
                market_id=market.id, question=q,
                status="SKIP_EXISTING_POSITION",
                detail=f"user {user_id} already holding this market",
                evaluation=evaluation, prediction_id=prediction_id,
            )

        bankroll = executor.get_bankroll()
        starting_cash = executor.get_starting_cash()
        verdict = evaluate_risk(
            user_config=user_config, bankroll=bankroll,
            starting_cash=starting_cash, mode=executor.mode,
            user_id=user_id,
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
        if decision.should_trade and verdict.stake_multiplier != 1.0:
            decision.stake_usd *= verdict.stake_multiplier
            decision.shares    *= verdict.stake_multiplier
            if decision.stake_usd < 2.0:
                decision.skip_reason = (
                    f"streak cooldown ({verdict.notes}) halved stake below "
                    f"$2.00 minimum - skipping"
                )
                decision.stake_usd = 0.0
                decision.shares    = 0.0

        # Log per-user evaluation row (one row per onboarded user per market).
        recommendation = (
            f"BUY_{decision.side}" if decision.should_trade else "SKIP"
        )
        eval_row_id = _log_market_evaluation(
            market=market, evaluation=evaluation, decision=decision,
            research_sources=research.sources, prediction_id=prediction_id,
            recommendation=recommendation, market_archetype=archetype,
            user_id=user_id,
        )

        if not decision.should_trade:
            return AnalysisOutcome(
                market_id=market.id, question=q,
                status="LOGGED_NO_TRADE",
                detail=decision.skip_reason or "sizer declined",
                evaluation=evaluation, decision=decision,
                prediction_id=prediction_id,
            )

        max_concurrent = int(getattr(config, "PM_MAX_CONCURRENT_POSITIONS", 10))
        if executor.open_position_count() >= max_concurrent:
            return AnalysisOutcome(
                market_id=market.id, question=q,
                status="SKIP_MAX_CONCURRENT",
                detail=f"{max_concurrent} positions already open for user {user_id}",
                evaluation=evaluation, decision=decision,
                prediction_id=prediction_id,
            )

        max_per_event = int(getattr(config, "PM_MAX_PER_EVENT", 3))
        if market.event_slug:
            event_count = executor.count_positions_for_event(market.event_slug)
            if event_count >= max_per_event:
                return AnalysisOutcome(
                    market_id=market.id, question=q,
                    status="SKIP_EVENT_CAP",
                    detail=f"{event_count}/{max_per_event} positions in event '{market.event_slug}'",
                    evaluation=evaluation, decision=decision,
                    prediction_id=prediction_id,
                )

        pos_id = executor.open_position(
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
        _link_evaluation_to_position(eval_row_id, pos_id)

        if self.notifier is not None:
            try:
                await self._notify_open(market, evaluation, decision,
                                         pos_id, executor, user_id)
            except Exception as exc:
                print(f"[pm_analyst] notify failed: {exc}", file=sys.stderr)

        return AnalysisOutcome(
            market_id=market.id, question=q,
            status="OPENED",
            detail=f"pm_position={pos_id} user={user_id} side={decision.side} "
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
        SaaS fan-out:
            1. Fetch candidate markets ONCE.
            2. For each market: research + Claude evaluation + prediction log
               happen ONCE (shared work).
            3. For each onboarded user: size + risk + open position using
               their own user_config, executor, and bankroll.

        Returns a summary keyed by counters across all users.
        """
        skip_days = int(getattr(config, "PM_SKIP_EXISTING_DAYS", 1))
        min_days  = int(getattr(config, "PM_MIN_DAYS_TO_END", 0))
        max_days  = int(getattr(config, "PM_MAX_DAYS_TO_END", 7))

        summary = {
            "fetched":     0,
            "analyzed":    0,
            "opened":      0,
            "no_trade":    0,
            "skipped":     0,
            "errors":      0,
            "users":       0,
            "outcomes":    [],
        }

        if is_trading_paused():
            print("[pm_analyst] trading paused - skipping scan", flush=True)
            summary["outcomes"].append({
                "market_id": "*", "question": "*",
                "status": "SKIP_PAUSED", "detail": "trading paused via /pause",
            })
            return summary

        user_ids = list_onboarded_user_ids()
        summary["users"] = len(user_ids)
        if not user_ids:
            print("[pm_analyst] no onboarded users - skipping scan", flush=True)
            return summary

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
                if _recently_predicted(mk.id, skip_days):
                    summary["outcomes"].append({
                        "market_id": mk.id, "question": mk.question[:80],
                        "status": "SKIP_RECENT_EVAL",
                        "detail": f"evaluated within {skip_days}d",
                    })
                    summary["skipped"] += 1
                    continue

                # Shared work: one Claude call per market, period.
                shared = await self._shared_evaluate(mk)
                if shared is None:
                    summary["errors"] += 1
                    continue
                evaluation, research, prediction_id = shared
                summary["analyzed"] += 1

                # Per-user work: fan out.
                for user_id in user_ids:
                    try:
                        outcome = await self._maybe_trade_for_user(
                            market=mk, evaluation=evaluation,
                            research=research, prediction_id=prediction_id,
                            user_id=user_id,
                        )
                    except Exception as exc:
                        print(f"[pm_analyst] user {user_id} failure on "
                              f"{mk.id}: {exc}", file=sys.stderr)
                        summary["errors"] += 1
                        continue

                    summary["outcomes"].append({
                        "market_id": outcome.market_id,
                        "question":  outcome.question,
                        "status":    outcome.status,
                        "detail":    outcome.detail,
                        "user_id":   user_id,
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
              f"users={summary['users']} "
              f"fetched={summary['fetched']} "
              f"analyzed={summary['analyzed']} "
              f"opened={summary['opened']} "
              f"no_trade={summary['no_trade']} "
              f"skipped={summary['skipped']} "
              f"errors={summary['errors']}", flush=True)
        return summary

    # ── Notifications ────────────────────────────────────────────────────────
    async def _notify_open(self, market: PolyMarket,
                            evaluation: MarketEvaluation,
                            decision: SizingDecision,
                            position_id: int,
                            executor: PMExecutor,
                            user_id: str) -> None:
        if self.notifier is None or not hasattr(self.notifier, "send"):
            return
        bankroll_after = 0.0
        try:
            bankroll_after = float(executor.get_bankroll())
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
            mode="live" if executor.mode == "live" else "simulation",
        )
        try:
            await self.notifier.send(user_id, msg)
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
    user_id:          Optional[str] = None,
) -> Optional[int]:
    if user_id is None:
        from engine.user_config import DEFAULT_USER_ID
        user_id = DEFAULT_USER_ID
    try:
        with get_engine().begin() as conn:
            row = conn.execute(text(
                "INSERT INTO market_evaluations ("
                "  user_id, market_id, condition_id, slug, question, category, "
                "  market_price_yes, claude_probability, confidence, "
                "  ev_bps, recommendation, reasoning, reasoning_short, "
                "  research_sources, prediction_id, market_archetype, event_slug"
                ") VALUES ("
                "  :user_id, :mid, :cid, :slug, :q, :cat, "
                "  :mp, :cp, :conf, "
                "  :ev_bps, :rec, :reason, :reason_short, "
                "  :srcs, :pid, :arch, :event_slug"
                ") RETURNING id"
            ), {
                "user_id": user_id,
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
                "reason_short": (getattr(evaluation, "reasoning_short", "") or None),
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
