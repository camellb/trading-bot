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
    7. Run the sizer (quarter-Kelly with guardrails).
    8. If the sizer approves and we're under the concurrent cap,
       open a shadow/live position via PMExecutor.
    9. Send Telegram notification + Obsidian memory entry.

The analyst never crashes the caller. Every stage is wrapped; failures
are logged and the next market proceeds.
"""

from __future__ import annotations

import asyncio
import time
import json
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

import calibration
import config
from db.engine import get_engine
from engine.polymarket_evaluator import PolymarketEvaluator, MarketEvaluation
from execution.pm_executor import PMExecutor
from execution.pm_sizer import size_position, SizingDecision
from execution.risk_manager import RiskManager
from engine.user_controls import UserControls
from feeds.polymarket_feed import PolymarketFeed, PolyMarket, _as_market
from research.fetcher import fetch_research, ResearchBundle


SOURCE = "polymarket"


async def _timeout_guard(coro, timeout: float):
    """Like asyncio.wait_for but safe with run_in_executor.

    Python's asyncio.wait_for() cancels the inner task on timeout,
    then waits for the cancel to complete.  If the task is stuck in
    loop.run_in_executor(), the cancel never completes (threads can't
    be interrupted), so wait_for() hangs FOREVER.

    This uses asyncio.wait() instead, which simply returns after the
    timeout without cancelling anything.  The orphaned executor thread
    will finish (or die with the process) — the event loop moves on.
    """
    task = asyncio.ensure_future(coro)
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if done:
        return task.result()
    raise asyncio.TimeoutError()



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
        memory:    Optional[object]              = None,
        news_feed: Optional[object]              = None,
    ):
        self.executor  = executor  or PMExecutor()
        self.evaluator = evaluator or PolymarketEvaluator()
        self.notifier  = notifier
        self.memory    = memory
        self.news_feed = news_feed
        self.risk_mgr  = RiskManager()
        self.user_controls = UserControls()
        # Cache strategy block for the duration of a scan batch (refreshed per scan).
        self._cached_strategy_block: Optional[str] = None
        self._strategy_cache_valid = False

    # ── Single market ────────────────────────────────────────────────────────
    async def analyze_market(
        self,
        market:      PolyMarket,
        skip_existing_days: int = 3,
    ) -> AnalysisOutcome:
        q = market.question[:80]

        # 0a. Global pause — stop all new position opening.
        paused, pause_reason = await asyncio.to_thread(self.user_controls.is_paused)
        if paused:
            return AnalysisOutcome(
                market_id=market.id, question=q,
                status="SKIP_PAUSED",
                detail=f"trading paused: {pause_reason or 'no reason given'}",
            )

        # 0b. Market blocklist — never trade this specific market.
        if await asyncio.to_thread(self.user_controls.is_blocked, market.id):
            return AnalysisOutcome(
                market_id=market.id, question=q,
                status="SKIP_BLOCKED",
                detail="market is on the blocklist",
            )

        # 1. Already holding this market?
        if await asyncio.to_thread(
            self.executor.has_open_position_on_market, market.id
        ):
            return AnalysisOutcome(
                market_id=market.id, question=q,
                status="SKIP_EXISTING_POSITION",
                detail="already holding this market",
            )

        # 2. Recently evaluated?
        if await asyncio.to_thread(_recently_predicted, market.id, skip_existing_days):
            return AnalysisOutcome(
                market_id=market.id, question=q,
                status="SKIP_RECENT_EVAL",
                detail=f"evaluated within {skip_existing_days}d",
            )

        # 3. Research (hard timeout to prevent blocking the event loop).
        try:
            research = await _timeout_guard(
                fetch_research(
                    market.question, market.category_hint,
                    market_id=market.id,
                    event_slug=getattr(market, "event_slug", None),
                    market_slug=getattr(market, "slug", None),
                    resolution_source=getattr(market, "resolution_source", None),
                ),
                timeout=120,
            )
        except asyncio.TimeoutError:
            print(f"[pm_analyst] research TIMEOUT (120s) for {market.id}",
                  file=sys.stderr)
            research = ResearchBundle(question=market.question)
        except Exception as exc:
            print(f"[pm_analyst] research failed for {market.id}: {exc}",
                  file=sys.stderr)
            research = ResearchBundle(question=market.question)

        research_block = research.to_prompt_block()
        strategy_block = await asyncio.to_thread(self._build_strategy_block)

        # 4. Evaluate (hard timeout — Claude API can hang on network issues).
        #    Use ensemble evaluator (Claude + Gemini) for more robust estimates.
        try:
            evaluation = await _timeout_guard(
                self.evaluator.evaluate_ensemble(
                    market,
                    research_block=research_block,
                    strategy_block=strategy_block,
                ),
                timeout=120,  # ensemble runs two models concurrently
            )
        except asyncio.TimeoutError:
            print(f"[pm_analyst] evaluate TIMEOUT (90s) for {market.id}",
                  file=sys.stderr)
            return AnalysisOutcome(
                market_id=market.id, question=q,
                status="ERROR", detail="evaluate: timeout (90s)",
            )
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

        # 4b. Attach research quality to evaluation for downstream use.
        evaluation.research_quality = getattr(research, "quality_score", 0.0)

        # 4c. Extreme edge justification — if Claude's raw edge exceeds
        # PM_EXTREME_EDGE_JUSTIFICATION_BPS, send a follow-up prompt
        # asking for specific verifiable evidence.  This recovers alpha
        # from high-edge markets with verifiable data while filtering
        # out markets where Claude is simply wrong.
        raw_edge = abs(evaluation.probability_yes - market.yes_price)
        justify_threshold_bps = float(
            getattr(config, "PM_EXTREME_EDGE_JUSTIFICATION_BPS", 1500)
        )
        _justification_skip = False  # set True if extreme edge is unjustified
        if raw_edge * 10_000.0 > justify_threshold_bps:
            try:
                justification = await _timeout_guard(
                    self.evaluator.justify_extreme_edge(
                        market, evaluation, research_block=research_block,
                    ),
                    timeout=30,
                )
            except asyncio.TimeoutError:
                justification = None
            except Exception as exc:
                print(f"[pm_analyst] justification error for {market.id}: {exc}",
                      file=sys.stderr)
                justification = None

            if justification is None:
                justification = {"action": "skip", "cited_evidence": "justification call failed"}

            action = justification.get("action", "skip")
            if action == "skip":
                _justification_skip = True
                print(f"[pm_analyst] extreme edge {raw_edge*10000:.0f}bps REJECTED "
                      f"for {market.id}: {justification.get('cited_evidence', '')[:100]}",
                      file=sys.stderr, flush=True)
            elif action == "revise":
                revised_p = justification.get("revised_probability", evaluation.probability_yes)
                old_p = evaluation.probability_yes
                evaluation.probability_yes = float(max(0.0, min(1.0, revised_p)))
                print(f"[pm_analyst] extreme edge REVISED for {market.id}: "
                      f"{old_p:.3f} → {evaluation.probability_yes:.3f} "
                      f"(quality={justification.get('justification_quality', 0):.2f})",
                      file=sys.stderr, flush=True)
            else:  # "allow"
                print(f"[pm_analyst] extreme edge {raw_edge*10000:.0f}bps JUSTIFIED "
                      f"for {market.id}: {justification.get('cited_evidence', '')[:100]}",
                      file=sys.stderr, flush=True)

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
            "market_archetype":        evaluation.market_archetype,
            "resolution_style":        evaluation.resolution_style,
            "resolution_quality_score": evaluation.resolution_quality_score,
            "research_quality":        getattr(research, "quality_score", None),
            "resolution_source_score": getattr(research, "resolution_source_score", None),
            "model_disagreement":      getattr(evaluation, "model_disagreement", None),
            "n_models":                getattr(evaluation, "n_models", None),
        }
        prediction_id = await asyncio.to_thread(
            calibration.log_prediction,
            source        = SOURCE,
            subject_key   = f"polymarket:{market.id}",
            probability   = evaluation.probability_yes,
            category      = evaluation.category,
            confidence    = evaluation.confidence,
            horizon_hours = market.days_to_end * 24.0,
            reasoning     = evaluation.reasoning,
            metadata      = pred_meta,
            market_archetype = evaluation.market_archetype,
            resolution_style = evaluation.resolution_style,
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

        # 5b. Extreme edge justification skip — prediction is logged for
        # calibration, but we won't open a position.
        if _justification_skip:
            return AnalysisOutcome(
                market_id=market.id, question=q,
                status="LOGGED_NO_TRADE",
                detail=(f"extreme edge {raw_edge*10000:.0f}bps — "
                        f"justification insufficient"),
                evaluation=evaluation,
                prediction_id=prediction_id,
            )

        # 5c. Archetype pause — skip if this archetype is paused.
        if evaluation.market_archetype:
            arch_paused, arch_reason = await asyncio.to_thread(
                self.user_controls.is_archetype_paused,
                evaluation.market_archetype,
            )
            if arch_paused:
                return AnalysisOutcome(
                    market_id=market.id, question=q,
                    status="SKIP_ARCHETYPE_PAUSED",
                    detail=f"archetype '{evaluation.market_archetype}' paused: "
                           f"{arch_reason or 'no reason given'}",
                    evaluation=evaluation,
                    prediction_id=prediction_id,
                )

        # 6. Size.
        bankroll = await asyncio.to_thread(self.executor.get_bankroll)
        news_mult = 1.0
        if self.news_feed is not None and hasattr(self.news_feed, "is_degraded"):
            if self.news_feed.is_degraded():
                news_mult = float(getattr(config, "NEWS_DEGRADED_SIZE_MULTIPLIER", 0.5))
        decision = size_position(
            market_price_yes   = market.yes_price,
            claude_probability = evaluation.probability_yes,
            confidence         = evaluation.confidence,
            bankroll_usd       = bankroll,
            days_to_end        = market.days_to_end,
            size_multiplier    = news_mult,
            mode               = self.executor.mode,
            archetype          = evaluation.market_archetype,
            resolution_quality = evaluation.resolution_quality_score,
            resolution_source_score = getattr(research, "resolution_source_score", None),
            research_quality   = evaluation.research_quality,
        )

        # Log a market_evaluations row regardless of trade outcome — this is
        # the auditing surface for the dashboard.
        recommendation = (
            f"BUY_{decision.side}" if decision.should_trade else "SKIP"
        )
        eval_row_id = await asyncio.to_thread(
            _log_market_evaluation,
            market=market,
            evaluation=evaluation,
            decision=decision,
            research_sources=research.sources,
            prediction_id=prediction_id,
            recommendation=recommendation,
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
        if await asyncio.to_thread(self.executor.open_position_count) >= max_concurrent:
            return AnalysisOutcome(
                market_id=market.id, question=q,
                status="SKIP_MAX_CONCURRENT",
                detail=f"{max_concurrent} positions already open",
                evaluation=evaluation, decision=decision,
                prediction_id=prediction_id,
            )

        # 7b. Portfolio-level risk checks (daily/weekly loss limit, heat, drawdown, etc.)
        risk_ok, risk_reason = await asyncio.to_thread(
            self.risk_mgr.check_risk,
            decision=decision,
            bankroll=bankroll,
            mode=self.executor.mode,
            event_slug=getattr(market, "event_slug", None),
            archetype=evaluation.market_archetype,
        )
        if not risk_ok:
            return AnalysisOutcome(
                market_id=market.id, question=q,
                status="SKIP_RISK",
                detail=risk_reason or "risk manager declined",
                evaluation=evaluation, decision=decision,
                prediction_id=prediction_id,
            )

        # 7c. Streak cooldown — reduce size if on a losing streak.
        decision = self.risk_mgr.apply_streak_adjustment(decision)
        if not decision.should_trade:
            return AnalysisOutcome(
                market_id=market.id, question=q,
                status="LOGGED_NO_TRADE",
                detail=decision.skip_reason or "streak cooldown — below min trade",
                evaluation=evaluation, decision=decision,
                prediction_id=prediction_id,
            )

        # 7d. Correlation-aware sizing — shrink stake based on correlation
        #     with existing portfolio positions.
        decision = await asyncio.to_thread(
            self.risk_mgr.adjust_stake_for_correlation,
            decision=decision,
            event_slug=getattr(market, "event_slug", None),
            archetype=evaluation.market_archetype,
            category=evaluation.category,
            mode=self.executor.mode,
        )
        if not decision.should_trade:
            return AnalysisOutcome(
                market_id=market.id, question=q,
                status="LOGGED_NO_TRADE",
                detail=decision.skip_reason or "correlation adjustment — below min trade",
                evaluation=evaluation, decision=decision,
                prediction_id=prediction_id,
            )

        # 8. Open position.
        pos_id = await asyncio.to_thread(
            self.executor.open_position,
            market=market, decision=decision,
            claude_probability=evaluation.probability_yes,
            prediction_id=prediction_id,
            reasoning=evaluation.reasoning,
            category=evaluation.category,
            market_archetype=evaluation.market_archetype,
            research_quality=getattr(evaluation, "research_quality", None),
            model_disagreement=getattr(evaluation, "model_disagreement", None),
            n_models=getattr(evaluation, "n_models", None),
        )
        if pos_id is None:
            return AnalysisOutcome(
                market_id=market.id, question=q,
                status="ERROR", detail="executor returned None",
                evaluation=evaluation, decision=decision,
                prediction_id=prediction_id,
            )

        # Link the evaluation row to the position for dashboard joins.
        await asyncio.to_thread(_link_evaluation_to_position, eval_row_id, pos_id)

        # 9. Side effects: Telegram + Obsidian.
        if self.notifier is not None:
            try:
                await self._notify_open(market, evaluation, decision, pos_id)
            except Exception as exc:
                print(f"[pm_analyst] notify failed: {exc}", file=sys.stderr)

        if self.memory is not None and hasattr(self.memory, "log_pm_entry"):
            try:
                await asyncio.to_thread(
                    self.memory.log_pm_entry,
                    market=market, evaluation=evaluation,
                    decision=decision, position_id=pos_id,
                    research=research,
                )
            except Exception as exc:
                print(f"[pm_analyst] memory log failed: {exc}", file=sys.stderr)

        return AnalysisOutcome(
            market_id=market.id, question=q,
            status="OPENED",
            detail=f"pm_position={pos_id} side={decision.side} "
                   f"stake=${decision.stake_usd:.2f} edge={decision.edge*10000:.0f}bps",
            evaluation=evaluation, decision=decision,
            prediction_id=prediction_id, position_id=pos_id,
        )

    def _build_strategy_block(self) -> Optional[str]:
        # Return cached version if available (refreshed once per scan batch).
        if self._strategy_cache_valid:
            return self._cached_strategy_block

        if self.memory is None or not hasattr(self.memory, "read_strategy_memory"):
            return None
        try:
            memory = self.memory.read_strategy_memory() or {}
        except Exception as exc:
            print(f"[pm_analyst] read_strategy_memory failed: {exc}",
                  file=sys.stderr)
            return None

        sections = []
        for label, raw in [
            ("What has been working recently", memory.get("what_works")),
            ("What has not been working", memory.get("what_doesnt_work")),
            ("Current thesis", memory.get("current_thesis")),
        ]:
            cleaned = self._clean_strategy_text(raw)
            if cleaned:
                sections.append(f"{label}: {cleaned}")

        # Include archetype-specific lessons if available.
        if hasattr(self.memory, "read_archetype_lessons"):
            try:
                arch_lessons = self.memory.read_archetype_lessons() or {}
                if arch_lessons:
                    arch_section_parts = []
                    for arch, content in sorted(arch_lessons.items()):
                        cleaned = self._clean_strategy_text(content)
                        if cleaned:
                            arch_section_parts.append(f"  {arch}: {cleaned[:300]}")
                    if arch_section_parts:
                        sections.append(
                            "Archetype-specific lessons:\n" +
                            "\n".join(arch_section_parts)
                        )
            except Exception as exc:
                print(f"[pm_analyst] read_archetype_lessons failed: {exc}",
                      file=sys.stderr)

        result = "\n".join(sections)[:2500] or None
        self._cached_strategy_block = result
        self._strategy_cache_valid = True
        return result

    @staticmethod
    def _clean_strategy_text(raw) -> str:
        if not raw:
            return ""
        lines = []
        for line in str(raw).splitlines():
            text = line.strip()
            if not text:
                continue
            if text.startswith("#") or text.startswith("_Last updated:"):
                continue
            text = text.lstrip("-* ").strip()
            if text:
                lines.append(text)
        return " ".join(lines)[:600]

    # ── Batch scan ───────────────────────────────────────────────────────────
    async def scan_and_analyze(
        self,
        limit:          int   = 20,
        min_volume_24h: float = 5_000.0,
        max_seconds:    int   = 0,
    ) -> dict:
        """
        Fetch candidate markets and run the analyst pipeline on each.
        Returns a summary dict suitable for scheduler logging and /status.

        max_seconds: if > 0, stop processing new markets when the elapsed
            time exceeds (max_seconds - 120s buffer).  This prevents the
            subprocess timeout from killing the scan and losing ALL results.
        """
        deadline = (time.monotonic() + max_seconds - 120) if max_seconds > 0 else 0
        # Invalidate strategy cache so it's refreshed once for this scan.
        self._strategy_cache_valid = False

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
            print("[pm_analyst] fetching candidate markets...", flush=True)
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

            # Merge priority markets that aren't already in the candidate list.
            try:
                priority_list = await asyncio.to_thread(
                    self.user_controls.get_priority_markets,
                )
                if priority_list:
                    existing_ids = {mk.id for mk in markets}
                    priority_ids = [
                        p["market_id"] for p in priority_list
                        if p["market_id"] not in existing_ids
                    ]
                    if priority_ids:
                        raw_markets = await feed.fetch_many(priority_ids)
                        for mid, raw in raw_markets.items():
                            pm = _as_market(raw)
                            if pm is not None:
                                markets.append(pm)
                                print(f"[pm_analyst] added priority market: "
                                      f"{pm.question[:60]}", flush=True)
            except Exception as exc:
                print(f"[pm_analyst] priority market merge failed: {exc}",
                      file=sys.stderr)

            summary["fetched"] = len(markets)
            print(f"[pm_analyst] got {len(markets)} candidates, starting evaluation",
                  flush=True)
            if not markets:
                return summary

            for mk in markets:
                # Time budget: stop before the subprocess timeout kills us
                # and we lose all results processed so far.
                if deadline and time.monotonic() > deadline:
                    remaining = len(markets) - summary["analyzed"] - summary["skipped"] - summary["errors"]
                    print(f"[pm_analyst] time budget exhausted — "
                          f"processed {summary['analyzed']} markets, "
                          f"{remaining} skipped due to time",
                          file=sys.stderr, flush=True)
                    break
                market_idx = markets.index(mk) + 1
                scan_start = (deadline - max_seconds + 120) if deadline else time.monotonic()
                elapsed = int(time.monotonic() - scan_start)
                q_short = (mk.question or "")[:50]
                print(f"[pm_analyst] [{market_idx}/{len(markets)}] "
                      f"evaluating: {q_short} ({elapsed}s)",
                      file=sys.stderr, flush=True)
                try:
                    outcome = await _timeout_guard(
                        self.analyze_market(mk, skip_existing_days=skip_days),
                        timeout=300,  # 5 min max per market
                    )
                except asyncio.TimeoutError:
                    print(f"[pm_analyst] TIMEOUT analyzing {mk.id} (300s) — skipping",
                          file=sys.stderr)
                    summary["errors"] += 1
                    continue
                except Exception as exc:
                    print(f"[pm_analyst] unexpected failure {mk.id}: {exc}",
                          file=sys.stderr)
                    summary["errors"] += 1
                    continue

                summary["analyzed"] += 1
                done_elapsed = int(time.monotonic() - scan_start)
                status_char = {"OPENED": "✓", "LOGGED_NO_TRADE": "—",
                               "ERROR": "✗"}.get(outcome.status, "⊘")
                print(f"[pm_analyst] [{market_idx}/{len(markets)}] "
                      f"{status_char} {outcome.status} ({done_elapsed}s)",
                      file=sys.stderr, flush=True)

                outcome_dict = {
                    "market_id": outcome.market_id,
                    "question":  outcome.question,
                    "status":    outcome.status,
                    "detail":    outcome.detail,
                }
                # Include trade details for OPENED positions so the main
                # process (which has the Telegram notifier) can send
                # per-position notifications.
                if outcome.status == "OPENED" and outcome.decision and outcome.evaluation:
                    d = outcome.decision
                    e = outcome.evaluation
                    outcome_dict["trade"] = {
                        "position_id":    outcome.position_id,
                        "side":           d.side,
                        "entry_price":    d.entry_price,
                        "stake_usd":      d.stake_usd,
                        "shares":         d.shares,
                        "edge_bps":       d.edge * 10_000,
                        "probability":    e.probability_yes,
                        "confidence":     e.confidence,
                        "market_price":   getattr(d, "market_price", None),
                        "end_date":       getattr(mk, "end_date_iso", None),
                    }
                summary["outcomes"].append(outcome_dict)
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
        entry_cents = decision.entry_price * 100.0
        crowd_price = market.yes_price * 100.0
        bot_estimate = evaluation.probability_yes * 100.0
        mispricing = decision.edge * 10_000.0
        why_line = (
            f"Why: the bot estimate for YES ({bot_estimate:.1f}%) is above "
            f"the crowd price ({crowd_price:.1f}%)."
            if decision.side == "YES" else
            f"Why: the bot estimate for YES ({bot_estimate:.1f}%) is below "
            f"the crowd price ({crowd_price:.1f}%)."
        )
        msg = (
            f"🎯 <b>New PM position</b> [{self.executor.mode}]\n"
            f"<b>{market.question[:140]}</b>\n"
            f"Bet: buy {decision.side} at {entry_cents:.1f}c\n"
            f"Stake: ${decision.stake_usd:.2f} for {decision.shares:.1f} shares\n"
            f"Bot estimate: {bot_estimate:.1f}%\n"
            f"Crowd price: {crowd_price:.1f}%\n"
            f"Mispricing: {mispricing:.0f} bps\n"
            f"Confidence: {evaluation.confidence:.2f}\n"
            f"{why_line}\n"
            f"Resolves: {market.end_date_iso.strftime('%Y-%m-%d')}\n"
            f"Position: #{position_id}"
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
) -> Optional[int]:
    try:
        with get_engine().begin() as conn:
            row = conn.execute(text(
                "INSERT INTO market_evaluations ("
                "  market_id, condition_id, slug, question, category, "
                "  market_price_yes, claude_probability, confidence, "
                "  edge_bps, recommendation, reasoning, research_sources, "
                "  prediction_id, market_archetype, resolution_style, "
                "  resolution_quality_score, event_slug, skip_reason"
                ") VALUES ("
                "  :mid, :cid, :slug, :q, :cat, "
                "  :mp, :cp, :conf, "
                "  :edge_bps, :rec, :reason, :srcs, :pid, "
                "  :archetype, :res_style, :res_quality, :event_slug, :skip_reason"
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
                "edge_bps": decision.edge * 10_000.0,
                "rec":  recommendation,
                "reason": evaluation.reasoning,
                "srcs": json.dumps(research_sources),
                "pid":  prediction_id,
                "archetype":   evaluation.market_archetype,
                "res_style":   evaluation.resolution_style,
                "res_quality": evaluation.resolution_quality_score,
                "event_slug":  getattr(market, "event_slug", None),
                "skip_reason": decision.skip_reason,
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
