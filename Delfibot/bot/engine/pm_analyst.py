"""
Polymarket Analyst - the decision engine.

Pipeline per market:
    1. Skip if we already have an open position on this market.
    2. Skip if we've already evaluated this market in the last N days
       (cost control - forecaster calls aren't free).
    3. Fetch research bundle (Wikipedia, recent news, base rates).
    4. Ask the forecaster for a probability + confidence + category via
       the PolymarketEvaluator (research-aware).
    5. Log a `predictions` row so the outcome feeds calibration.
    6. Log a `market_evaluations` row for the dashboard/audit trail.
    7. Run the V1 sizer: side = market favourite, skip if the
       forecaster disagrees with the market, flat archetype-multiplied
       stake.
    8. If the sizer approves and we're under the concurrent cap,
       open a simulation/live position via PMExecutor.
    9. Write a position_opened row to event_log; the dashboard surfaces it.

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
from db.logger import log_event
from execution.pm_executor import PMExecutor
from execution.pm_sizer import (
    size_position, SizingDecision, POLYMARKET_MIN_ORDER_USD,
)
from feeds.polymarket_feed import PolymarketFeed, PolyMarket
from research.fetcher import fetch_research, ResearchBundle


SOURCE = "polymarket"

# Per-process dedup for the one-shot "Polymarket platform minimum is
# blocking most trades" Telegram alert. Adds the user_id once we've
# fired the notification for that user; the rest of the session is
# silent (skips still hit the Skipped tab, just no Telegram spam).
# Reset on process restart — fine since the alert is informational,
# not a critical event the user needs to see every time.
_POLYMARKET_MIN_SKIP_NOTIFIED: set = set()


# ─── Bankroll precondition gate ───────────────────────────────────────────
#
# Halt the scan entirely when the user can't afford to place even a
# minimum-size bet. Calling Claude on markets we couldn't trade on is
# pure waste on the user's LLM bill (the user pays for tokens).
#
# Threshold = POLYMARKET_MIN_ORDER_USD ($1) — the absolute platform
# floor. Any deployable cash above this can theoretically buy something
# on a longshot market (5 shares × $0.20 = $1.00). Below this, no bet
# can pass Polymarket's size check regardless of which market we look
# at. Pre-Claude.
#
# Recovery is automatic: the 60s `pm_balance_refresh` job keeps the
# cached wallet probe fresh; the next scan tick re-checks and resumes
# as soon as a deposit lands or an open position settles back to cash.
#
# Same rule applies in simulation: LLM tokens cost the same in both
# modes, so a $0 sim bankroll halts scanning identically.
#
# Announcement state persists to a marker file under app-data so it
# survives daemon restarts. The previous version was a module-level
# boolean that reset to False on every process start, which meant
# every install / launchd respawn / crash re-broadcast the "paused"
# Telegram. User got six identical paused-messages in a single day
# on 2026-05-23 from my deploy loop; engraved this as "once is
# fucking enough."
_PAUSE_MARKER_NAME = "bankroll_pause_announced"


def _pause_marker_path():
    """Return the marker-file path, created lazily under app-data.
    Single-user local-first so no per-user keying.
    """
    from db.engine import app_data_dir
    base = app_data_dir() / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base / _PAUSE_MARKER_NAME


def _pause_already_announced() -> bool:
    try:
        return _pause_marker_path().exists()
    except Exception:
        # If we can't read the marker (permissions, etc.), default
        # to "already announced" so we don't spam on a degraded FS.
        return True


def _set_pause_announced(flag: bool) -> None:
    try:
        p = _pause_marker_path()
        if flag:
            p.touch()
        elif p.exists():
            p.unlink()
    except Exception as exc:
        print(
            f"[pm_analyst] pause-marker write failed: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def _user_deployable_cash(user_id: str) -> Optional[tuple[float, float]]:
    """Return ``(bankroll, deployable)`` for ``user_id``, or None on error.

    ``deployable = bankroll * (1 - dry_powder_reserve_pct)``.

    Open positions are already netted by `executor.get_bankroll()` in
    both live (wallet probe reflects on-chain spent collateral) and
    simulation (DB formula subtracts open cost).
    """
    try:
        cfg = get_user_config(user_id)
        exec_ = PMExecutor(user_id)
        if not exec_.ready:
            return None
        bankroll = float(exec_.get_bankroll())
        reserve_pct = float(getattr(cfg, "dry_powder_reserve_pct", 0.0) or 0.0)
        deployable = bankroll * max(0.0, 1.0 - reserve_pct)
        return bankroll, deployable
    except Exception as exc:
        print(
            f"[pm_analyst] deployable probe failed for {user_id}: {exc}",
            file=sys.stderr,
        )
        return None


def is_scan_idle_for_bankroll(user_id: str = "local") -> bool:
    """True iff `user_id` is currently below the platform-minimum
    deployable floor.

    Used by:
      * scan_and_analyze   as the gate that skips per-market LLM work.
      * local_api._state   to surface `idle_reason` on the dashboard.

    Cheap: the wallet probe is cached with a 60s TTL, so this is a
    dict lookup in steady state. Fail-open on any error (caller treats
    'unknown' as 'not idle' so the scan still runs and the downstream
    sizer remains the source of truth).
    """
    try:
        cfg = get_user_config(user_id)
    except Exception:
        return False
    if not getattr(cfg, "bot_enabled", False):
        # Different state entirely (manually paused / not onboarded).
        # The dashboard surfaces those separately.
        return False
    pair = _user_deployable_cash(user_id)
    if pair is None:
        return False
    _, deployable = pair
    return deployable < POLYMARKET_MIN_ORDER_USD


def _maybe_broadcast_bankroll_pause(
    user_id: str, bankroll: float, deployable: float,
) -> None:
    """One-shot Telegram alert on the active→paused transition.

    Subsequent paused scans no-op until the flag is reset by a
    resume (via `_reset_bankroll_pause_announcement`).

    State is persisted to a marker file under app-data so the
    "once-only" guarantee survives daemon restarts. See the long
    comment block above `_PAUSE_MARKER_NAME`.
    """
    if _pause_already_announced():
        return
    _set_pause_announced(True)
    try:
        from feeds.telegram_notifier import notify
        notify(
            "💤 Delfi has paused. Your available cash is below the "
            "minimum needed to place a trade. Trading will resume "
            "automatically once more funds are available.",
            user_id=user_id,
        )
    except Exception as exc:
        print(
            f"[pm_analyst] bankroll-pause telegram failed: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def _reset_bankroll_pause_announcement() -> None:
    """Called every time a scan actually runs (gate passes, which
    means deployable >= POLYMARKET_MIN_ORDER_USD). Lets the next
    active→paused transition re-broadcast."""
    _set_pause_announced(False)


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
            # Hard wall-clock ceiling on the research phase. Without
            # this, one stuck network call (observed 2026-05-14:
            # pycares getaddrinfo hung indefinitely on a Wikipedia
            # fetch) holds the whole scan, and APScheduler's
            # max_instances=1 means every subsequent scan gets
            # dropped. wait_for cancels the awaiting task even when
            # aiohttp's ClientTimeout fails to propagate into a
            # wedged resolver. We continue with an empty research
            # bundle on timeout — the forecaster falls back to its
            # own reasoning, which is degraded but better than the
            # bot freezing.
            research = await asyncio.wait_for(
                fetch_research(
                    market.question, market.category_hint,
                    days_to_resolution=market.days_to_end,
                    # Resolution date pins DDG queries to the SPECIFIC
                    # event month+year. Without it, evergreen searches
                    # like "head to head" surface 18-month-old results
                    # that confuse the forecaster.
                    resolution_date=market.resolution_at_estimate,
                    # Event slug carries the opponent+date for markets
                    # whose question is opaque on its own (e.g.
                    # "Spread: Thunder (-5.5)" with event slug
                    # "nba-sas-okc-2026-05-20"). Without this the
                    # keyword extractor misses the opponent and pulls
                    # research about the wrong game.
                    event_slug=getattr(market, "event_slug", None),
                    # Resolution description is the highest-fidelity
                    # event identifier we have. Without it the keyword
                    # extractor falls back to the question alone and
                    # produces queries like "AL vs WE LoL" that match
                    # adjacent series instead of the SPECIFIC game.
                    # Set 2026-06-01 after off-event skips on LoL 1504,
                    # GOAT 1486, Musk 1484.
                    description=getattr(market, "description", None),
                ),
                timeout=30,
            )
        except asyncio.TimeoutError:
            print(f"[pm_analyst] research exceeded 30s for {market.id} "
                  f"- aborted, continuing with empty bundle",
                  file=sys.stderr)
            research = ResearchBundle(question=market.question)
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

        # Classify archetype up front so the SKIP rows below can record
        # it; the sizer below uses the same value.
        archetype = classify_archetype(
            market.question,
            category=evaluation.category,
            event_slug=getattr(market, "event_slug", None),
        )

        if executor.has_open_position_on_market(market.id):
            # Persist a SKIP row so the user can see the bot DID look at
            # this market and chose to pass because of an existing
            # position. Without this, re-scans of held markets evaporate
            # silently and the Recent Activity surface stays empty.
            _log_market_evaluation(
                market=market, evaluation=evaluation, decision=None,
                research_sources=research.sources,
                prediction_id=prediction_id, recommendation="SKIP",
                market_archetype=archetype,
                user_id=user_id, mode=user_config.mode,
                skip_reason="Already holding an open position on this market.",
            )
            return AnalysisOutcome(
                market_id=market.id, question=q,
                status="SKIP_EXISTING_POSITION",
                detail=f"user {user_id} already holding this market",
                evaluation=evaluation, prediction_id=prediction_id,
            )

        bankroll = executor.get_bankroll()
        starting_cash = executor.get_starting_cash()
        # Current MTM equity for the drawdown calc. Live mode pulls
        # this from Polymarket's data-api (cash + sum of currentValue
        # across every position the wallet holds). Sim mode returns
        # cost-basis equity; risk_manager falls back to bankroll +
        # open_cost if this raises.
        try:
            equity = executor.get_equity()
        except Exception:
            equity = None
        verdict = evaluate_risk(
            user_config=user_config, bankroll=bankroll,
            starting_cash=starting_cash, mode=executor.mode,
            user_id=user_id, equity=equity,
        )
        if verdict.halted:
            # Record the risk-halt skip so the user can see why the bot
            # passed on otherwise-tradeable markets when a circuit
            # breaker is active.
            _log_market_evaluation(
                market=market, evaluation=evaluation, decision=None,
                research_sources=research.sources,
                prediction_id=prediction_id, recommendation="SKIP",
                market_archetype=archetype,
                user_id=user_id, mode=user_config.mode,
                skip_reason=(
                    f"Risk circuit breaker is active: "
                    f"{verdict.halt_reason or 'risk breaker tripped'}"
                ),
            )
            return AnalysisOutcome(
                market_id=market.id, question=q,
                status="SKIP_RISK_HALT",
                detail=verdict.halt_reason or "risk breaker tripped",
                evaluation=evaluation, prediction_id=prediction_id,
            )

        # The force_skip branch that lived here historically (the
        # evaluator's `same_event_verified=no` -> hard skip path) was
        # removed in v1.5.45 per doctrine: research mismatch is NOT a
        # valid user-visible skip reason. The fix lives upstream in two
        # places:
        #   1. research/fetcher.py + pm_analyst.py now pass
        #      market.description into the keyword extractor so the
        #      retrieval targets the right event (bulletproofing).
        #   2. engine/polymarket_evaluator.py no longer sets
        #      force_skip=True on off-event research; it produces a
        #      calibrated estimate anchored on the description + base
        #      rates instead (no refusal).
        # The MarketEvaluation.force_skip field is kept on the
        # dataclass for forward compatibility but has no live writer.
        # If a future change reintroduces a force-skip path, the
        # handler must be added back HERE alongside doctrine-compliant
        # skip_reason copy. Do not paper this over with "research is
        # off-event" phrasing - that pattern is banned per the user's
        # 2026-06-01 directive.

        decision = size_position(
            delfi_p    = evaluation.probability_yes,
            confidence  = evaluation.confidence,
            ask_yes     = market.yes_price,
            ask_no      = market.no_price,
            bankroll    = verdict.effective_bankroll,
            user_config = user_config,
            archetype   = archetype,
            volume_usd  = getattr(market, "volume_24h_clob", None),
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
            user_id=user_id, mode=user_config.mode,
        )

        if not decision.should_trade:
            # One-shot user-facing alert: if the bot is in live mode and
            # the FIRST skip we see from this user is the Polymarket
            # 5-share platform minimum, fire ONE bot_status event with
            # Telegram so the user knows why most markets are being
            # passed on. Subsequent skips just land silently in the
            # Skipped tab. Per-user dedup so multi-tenant accounts each
            # get exactly one alert; reset on process restart.
            if (
                user_config.mode == "live"
                and decision.skip_reason
                and "Polymarket needs" in decision.skip_reason
                and user_id not in _POLYMARKET_MIN_SKIP_NOTIFIED
            ):
                _POLYMARKET_MIN_SKIP_NOTIFIED.add(user_id)
                try:
                    telegram_html = (
                        "<b>ℹ️ Most markets being skipped</b>\n"
                        "Your stake is below Polymarket's per-order minimum.\n"
                        "Settings → Risk → raise Base stake (or fund the wallet)."
                    )
                    log_event(
                        event_type="bot_status",
                        severity=2,
                        description=(
                            "Polymarket 5-share floor blocking most trades "
                            f"(skip reason: {decision.skip_reason})"
                        ),
                        source="pm_analyst.polymarket_min_skip",
                        telegram_html=telegram_html,
                    )
                except Exception as _exc:
                    # Don't let the notification path break the scan.
                    pass
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
            delfi_probability=evaluation.probability_yes,
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

        # Replace in-memory decision with the ACTUAL fill from the
        # persisted row. The executor's `_open_live` swaps to actual
        # values internally but its mutation doesn't propagate up the
        # call stack (dataclass.replace returns a new instance bound
        # only to its local scope). Without this re-read, every
        # downstream consumer of `decision` — the notification, the
        # event_log description, the AnalysisOutcome detail string,
        # the dashboard's activity feed — would render INTENT values
        # (e.g. "stake $3.23") even when only $3.04 actually filled
        # on-chain. User-reported 2026-05-18.
        try:
            from sqlalchemy import text as _text
            from db.engine import get_engine as _eng
            with _eng().begin() as _conn:
                _row = _conn.execute(_text(
                    "SELECT shares, cost_usd, entry_price "
                    "FROM pm_positions WHERE id = :pid"
                ), {"pid": pos_id}).fetchone()
            if _row:
                from dataclasses import replace as _replace
                try:
                    decision = _replace(
                        decision,
                        shares=float(_row[0]),
                        stake_usd=float(_row[1]),
                        entry_price=float(_row[2]),
                    )
                except TypeError:
                    pass
        except Exception as _exc:
            print(
                f"[pm_analyst] actual-fill re-read failed for pos "
                f"{pos_id}: {_exc}",
                file=sys.stderr,
            )

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
                   f"market_p={decision.p_win:.2f} conf={decision.confidence:.2f}",
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

        # Per-user time-to-resolution filter overrides the global
        # config defaults when the user has set explicit DAYS via
        # the dashboard. NULL on either side means "no constraint
        # on this side"; we fall back to the global PM_MIN/MAX
        # defaults from config.py.
        #
        # Single-user local-first: there's exactly one onboarded
        # user, so we pick their config as the authoritative bound.
        # In a multi-user fan-out we'd take the most permissive
        # bounds across users (so we fetch every market any user
        # might want) and re-filter per-user later.
        from engine.user_config import get_user_config, list_onboarded_user_ids
        global_min = int(getattr(config, "PM_MIN_DAYS_TO_END", 0))
        global_max = int(getattr(config, "PM_MAX_DAYS_TO_END", 7))
        _uids = list_onboarded_user_ids() or []
        # Top-of-loop early-out: if no users are onboarded OR every
        # onboarded user has bot_enabled=False, the per-market work
        # (research + Claude eval + size) is wasted - the executor
        # would refuse to open anyway. Skip the whole scan instead
        # of paying for ~20 Claude calls only to throw the answers
        # away. The per-market `bot_enabled` check inside
        # evaluate_market remains as a defence-in-depth gate for
        # races where the user disables the bot mid-scan.
        if not _uids:
            return {
                "fetched": 0, "analyzed": 0, "opened": 0,
                "no_trade": 0, "skipped": 0,
                "skip_reason": "no_onboarded_users",
            }
        any_enabled = any(get_user_config(u).bot_enabled for u in _uids)
        if not any_enabled:
            return {
                "fetched": 0, "analyzed": 0, "opened": 0,
                "no_trade": 0, "skipped": 0,
                "skip_reason": "bot_disabled",
            }

        # Bankroll precondition: if NO bot-enabled user has deployable
        # cash above the platform minimum, halt the entire scan. Calling
        # Claude on markets we can't trade is pure spend on the user's
        # LLM bill. See the module-level docstring for the full rationale.
        bankroll_summaries: list[tuple[str, float, float]] = []
        any_can_trade = False
        for _uid in _uids:
            try:
                if not get_user_config(_uid).bot_enabled:
                    continue
            except Exception:
                continue
            pair = _user_deployable_cash(_uid)
            if pair is None:
                # Fail-open: if we can't read this user's bankroll we let
                # the scan run. The sizer's per-market check remains the
                # source of truth.
                any_can_trade = True
                continue
            bankroll, deployable = pair
            bankroll_summaries.append((_uid, bankroll, deployable))
            if deployable >= POLYMARKET_MIN_ORDER_USD:
                any_can_trade = True

        if not any_can_trade and bankroll_summaries:
            # Fire one Telegram per active→paused transition (per user).
            for _uid, _bk, _dep in bankroll_summaries:
                _maybe_broadcast_bankroll_pause(_uid, _bk, _dep)
            _summary_line = " | ".join(
                f"{u}: bankroll=${b:.2f} deployable=${d:.2f}"
                for u, b, d in bankroll_summaries
            )
            print(
                f"[pm_analyst] scan halted: insufficient bankroll. "
                f"{_summary_line}",
                flush=True,
            )
            return {
                "fetched": 0, "analyzed": 0, "opened": 0,
                "no_trade": 0, "skipped": 0,
                "skip_reason": "insufficient_bankroll",
            }

        # Risk circuit breaker pre-flight. If a circuit breaker is
        # active (gross exposure cap reached, drawdown halt, daily /
        # weekly loss limit, streak cooldown), HALT THE WHOLE SCAN
        # rather than evaluating each market only to skip every one
        # downstream. Previously the risk verdict ran per-market
        # inside _maybe_trade_for_user, AFTER the research fetch and
        # LLM evaluation had already burned tokens on a trade that
        # the sizer was about to refuse anyway. With ~20 markets
        # per scan, that's 20 wasted LLM calls every five minutes
        # while the breaker stays active. User instruction
        # 2026-05-23: "we should never SKIP because of the circuit
        # breaker. If we have no money to play or we have too much
        # exposure the bot should just stop evaluating."
        risk_halted_uids: list[tuple[str, str]] = []
        for _uid, _bk, _dep in bankroll_summaries:
            try:
                ucfg = get_user_config(_uid)
                ex = PMExecutor(_uid)
                if not ex.ready:
                    continue
                try:
                    _equity = ex.get_equity()
                except Exception:
                    _equity = None
                v = evaluate_risk(
                    user_config=ucfg, bankroll=_bk,
                    starting_cash=ex.get_starting_cash(),
                    mode=ex.mode, user_id=_uid,
                    equity=_equity,
                )
                if v.halted:
                    risk_halted_uids.append((_uid, v.halt_reason or "risk halted"))
            except Exception as exc:
                # Fail-open: let the scan run if we can't determine
                # risk state. The per-user risk check inside the
                # trade phase remains as a backstop.
                print(
                    f"[pm_analyst] risk pre-flight failed for {_uid}: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
        # If EVERY bot-enabled user is risk-halted, skip the scan.
        # In multi-user fan-out we'd just exclude the halted users
        # and proceed for the rest; in single-user local-first
        # mode this is equivalent to halting outright.
        active_uids = {u for u, _, _ in bankroll_summaries}
        halted_uids = {u for u, _ in risk_halted_uids}
        if active_uids and halted_uids >= active_uids:
            _reasons = " | ".join(
                f"{u}: {r}" for u, r in risk_halted_uids
            )
            print(
                f"[pm_analyst] scan halted: risk circuit breakers "
                f"active. {_reasons}",
                flush=True,
            )
            return {
                "fetched": 0, "analyzed": 0, "opened": 0,
                "no_trade": 0, "skipped": 0,
                "skip_reason": "risk_halted",
                "risk_reasons": dict(risk_halted_uids),
            }

        # Scan is going to run: reset the pause-announce flag so the
        # next active→paused transition re-broadcasts.
        _reset_bankroll_pause_announcement()

        if _uids:
            _ucfg = get_user_config(_uids[0])
            user_min = _ucfg.min_days_to_resolution
            user_max = _ucfg.max_days_to_resolution
        else:
            user_min = None
            user_max = None
        min_days = user_min if user_min is not None else global_min
        max_days = user_max if user_max is not None else global_max

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

        # Tag-balanced fan-out when PM_SCAN_TAG_QUOTAS is configured.
        # Without this, a top-by-volume scan is ~80% sports (the highest-
        # volume archetype on Polymarket), which crowds out the categories
        # where Delfi has been better calibrated. Quotas sum to the
        # effective scan size; an empty dict falls back to the legacy
        # untagged path.
        tag_quotas: dict[int, int] = dict(getattr(config, "PM_SCAN_TAG_QUOTAS", {}) or {})

        async with PolymarketFeed() as feed:
            try:
                if tag_quotas:
                    markets = await feed.fetch_candidates_balanced(
                        tag_quotas=tag_quotas,
                        min_volume_24h=min_volume_24h,
                        min_days=min_days, max_days=max_days,
                    )
                else:
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

            # Pre-load every user's config once per scan so the inner
            # per-market pre-filter doesn't hit SQLite per-market.
            user_cfgs = {uid: get_user_config(uid) for uid in user_ids}

            for mk in markets:
                if _recently_predicted(mk.id, skip_days):
                    summary["outcomes"].append({
                        "market_id": mk.id, "question": mk.question[:80],
                        "status": "SKIP_RECENT_EVAL",
                        "detail": f"evaluated within {skip_days}d",
                    })
                    summary["skipped"] += 1
                    continue

                # ─────────────────────────────────────────────────────
                # PRE-CLAUDE FILTER
                # ─────────────────────────────────────────────────────
                # INPUT axis : mk.yes_price = raw market_p_yes, [0, 1]
                # BANDS axis : raw market_p_yes (matches sizer gate)
                # INVARIANT  : drop the market only if EVERY user would
                #              skip at the sizer for the same reason
                #              (archetype on skip list, or price inside
                #              a per-archetype disabled band).
                #
                # The archetype classifier is a pure regex/keyword
                # function; running it BEFORE the Claude call saves
                # one Anthropic API call per dropped market. Post-Claude
                # classify still runs and may yield a different
                # (category-disambiguated) archetype, so this is purely
                # an optimization on cases where question text alone
                # is enough to know the trade would be skipped.
                #
                # If any user wouldn't skip, run Claude. The post-sizer
                # gate is the source of truth, not this filter.
                pre_arch = classify_archetype(
                    mk.question,
                    event_slug=getattr(mk, "event_slug", None),
                )
                pre_skip_reason = None
                # Fast-path: if EVERY user would skip this market for
                # the same reason, skip pre-Claude. If any user would
                # take it, run Claude.
                all_users_would_skip = bool(user_cfgs)
                for cfg in user_cfgs.values():
                    if pre_arch in (cfg.archetype_skip_list or ()):
                        pre_skip_reason = (
                            f"archetype '{pre_arch}' on skip list"
                        )
                        continue
                    arch_bands = (
                        cfg.archetype_skip_market_price_bands or {}
                    ).get(pre_arch, ())
                    hit = next(
                        (
                            (lo, hi) for lo, hi in arch_bands
                            if float(lo) <= mk.yes_price < float(hi)
                        ),
                        None,
                    )
                    if hit is not None:
                        lo, hi = hit
                        pre_skip_reason = (
                            f"market price {mk.yes_price:.2f} inside "
                            f"disabled {float(lo):.2f}-{float(hi):.2f} "
                            f"band on {pre_arch}"
                        )
                        continue
                    # This user wouldn't pre-skip; abort pre-filter.
                    all_users_would_skip = False
                    pre_skip_reason = None
                    break
                if all_users_would_skip and pre_skip_reason:
                    summary["outcomes"].append({
                        "market_id": mk.id,
                        "question":  mk.question[:80],
                        "status":    "SKIP_PRE_FILTER",
                        "detail":    pre_skip_reason,
                    })
                    summary["skipped"] += 1
                    continue

                # Risk-breaker re-check INSIDE the loop. The pre-flight
                # at the top of the scan caught the state at scan-start,
                # but circuit breakers can trip mid-scan when (a) the
                # wallet probe refresh job (every 60s) lands a fresh
                # balance, (b) an earlier market in THIS scan opened a
                # position that pushed gross exposure over the cap, or
                # (c) a settlement landed via the resolver. Without this
                # check the scan keeps feeding every remaining market
                # to the LLM only for the sizer to skip them - burning
                # the user's Anthropic tokens for nothing.
                #
                # User instruction (2026-05-26): "when the circuit
                # breaker is on it should completely stop scanning for
                # new markets, not skip them. Those are wasted tokens
                # out there."
                _halt_reasons_now: list[tuple[str, str]] = []
                _any_active_now = False
                for _uid in user_ids:
                    try:
                        _ucfg = user_cfgs.get(_uid) or get_user_config(_uid)
                        if not _ucfg.bot_enabled:
                            continue
                        _ex = PMExecutor(_uid)
                        if not _ex.ready:
                            continue
                        try:
                            _eq = _ex.get_equity()
                        except Exception:
                            _eq = None
                        _v = evaluate_risk(
                            user_config=_ucfg,
                            bankroll=_ex.get_bankroll(),
                            starting_cash=_ex.get_starting_cash(),
                            mode=_ex.mode,
                            user_id=_uid,
                            equity=_eq,
                        )
                        if _v.halted:
                            _halt_reasons_now.append(
                                (_uid, _v.halt_reason or "risk halted"),
                            )
                        else:
                            _any_active_now = True
                    except Exception as _exc:
                        # Fail-open: if the probe fails, treat the user
                        # as active so the scan proceeds. The per-market
                        # backstop inside _maybe_trade_for_user catches
                        # any genuine halts that slip through.
                        print(
                            f"[pm_analyst] mid-scan risk re-check failed "
                            f"for {_uid}: {type(_exc).__name__}: {_exc}",
                            file=sys.stderr,
                        )
                        _any_active_now = True
                if not _any_active_now and _halt_reasons_now:
                    _reasons = " | ".join(
                        f"{u}: {r}" for u, r in _halt_reasons_now
                    )
                    print(
                        f"[pm_analyst] risk breaker tripped mid-scan, "
                        f"halting remaining markets to save LLM calls. "
                        f"{_reasons}",
                        flush=True,
                    )
                    summary["skip_reason"] = "risk_halted_mid_scan"
                    summary["risk_reasons"] = dict(_halt_reasons_now)
                    break

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
        # Local-first build: notifications go to the SQLite event_log table,
        # which the dashboard reads via GET /api/events. No Telegram, no
        # outbound network for user notifications.

        # CRITICAL: read the ACTUAL persisted shares / cost / entry_price
        # from the pm_positions row, not from the in-memory `decision`.
        # `_open_live` mutates a LOCAL copy of `decision` after polling
        # the actual fill, but the caller's reference (this method's
        # `decision` arg) is the un-mutated INTENT. Reading the DB row
        # — which has the post-fill values — makes the notification
        # match what's actually on-chain. User-reported 2026-05-18:
        # "you told me Stake is $3.23 but the actual value is $3.04.
        # What happened to the $0.19?" Answer: the $0.19 never left
        # the wallet (5-share intent only partially filled at 4.68
        # shares), but the notification fired with intent values.
        try:
            from sqlalchemy import text as _text
            from db.engine import get_engine as _eng
            with _eng().begin() as _conn:
                _row = _conn.execute(_text(
                    "SELECT shares, cost_usd, entry_price "
                    "FROM pm_positions WHERE id = :pid"
                ), {"pid": position_id}).fetchone()
            if _row:
                from dataclasses import replace as _replace
                try:
                    decision = _replace(
                        decision,
                        shares=float(_row[0]),
                        stake_usd=float(_row[1]),
                        entry_price=float(_row[2]),
                    )
                except TypeError:
                    pass
        except Exception as _exc:
            print(
                f"[pm_analyst] notify: actual-fill lookup failed for "
                f"pos {position_id}: {_exc}",
                file=sys.stderr,
            )
        # Wallet probe refresh after opening a position. The order
        # just spent N dollars of pUSD; without this the cached
        # probe (5-min TTL) shows pre-trade balance for up to 5 min.
        #
        # NOTE: a8e9625 escalated this to
        # force_refresh_all_polymarket_caches (wallet + data-api
        # positions + user-pnl, all in one synchronous burst).
        # Reverted 2026-05-23 — the bundled HTTPS calls saturated
        # the api_executor under load and wedged the daemon (same
        # GIL-contention pattern as earlier "user-pnl-in-request
        # -path" bugs). The pending_payout SQL narrowing in
        # a718bc3 fixes the actual root cause of the inflated-
        # Balance bug; this lighter wallet-only refresh is enough
        # to keep the new_position message's bankroll fresh.
        try:
            from engine.user_config import get_user_polymarket_creds
            from feeds.polymarket_wallet import refresh_live_balance_cache
            _creds = get_user_polymarket_creds(user_id)
            _pk = (_creds or {}).get("private_key") if _creds else None
            if _pk:
                refresh_live_balance_cache(_pk)
        except Exception as _exc:
            print(f"[pm_analyst] post-open wallet refresh failed: {_exc}",
                  file=sys.stderr)

        bankroll_after = 0.0
        equity_after: float = 0.0
        locked_capital_after: float = float(decision.stake_usd)
        try:
            # Single source of truth for the message numbers: pull
            # bankroll + locked_capital + equity from the SAME stats
            # snapshot that settled_win/loss uses (polymarket_runner).
            # In live mode locked_capital comes from Polymarket's
            # data-api currentValue sum, exactly matching what the
            # user sees on polymarket.com. Previously this path
            # computed locked_capital from SUM(cost_usd) on
            # pm_positions, which is the BOT'S cost basis — diverged
            # from Polymarket's MTM-based "Locked Capital" tile and
            # caused the new_position message to disagree with the
            # adjacent settled_win/loss messages.
            stats = executor.get_portfolio_stats()
            bankroll_after = float(stats.get("bankroll", 0.0))
            locked_capital_after = float(
                stats.get("locked_capital",
                          stats.get("open_cost", 0.0)) or 0.0
            )
            equity_after = float(
                stats.get("equity", bankroll_after + locked_capital_after)
            )
        except Exception:
            equity_after = bankroll_after + float(decision.stake_usd)
        market_pct = decision.p_win * 100.0
        # `probability_yes` is the forecaster's P(YES), always.
        # `forecast_pct_yes` keeps it as P(YES) for the description
        # log line (which labels market + forecast as YES%
        # explicitly so context is unambiguous).
        # `forecast_pct_side` flips to P(NO) on a NO bet so the
        # Telegram message "Delfi forecasts: NO (X% probability)"
        # reports the probability of the side actually bet on,
        # not P(YES). The previous wiring passed P(YES) regardless
        # of side, which mis-labelled every NO trade.
        forecast_pct_yes  = evaluation.probability_yes * 100.0
        forecast_pct_side = (
            forecast_pct_yes if decision.side == "YES"
            else (1.0 - evaluation.probability_yes) * 100.0
        )
        mode = "live" if executor.mode == "live" else "simulation"
        # Description labels probabilities side-scoped (matching the
        # Telegram message). The earlier line wrote
        # "forecast {forecast_pct_yes}%" without an explicit side, so
        # a NO bet showed up on the dashboard activity feed as
        # "forecast 25%" while the Telegram message said
        # "Delfi forecasts: NO (75% probability)" - same trade, two
        # different numbers depending on which surface you read.
        description = (
            f"Opened {decision.side} on {market.question[:120]} "
            f"for ${decision.stake_usd:.2f} "
            f"(market P(YES) {market_pct:.1f}%, "
            f"Delfi P({decision.side}) {forecast_pct_side:.1f}%, "
            f"confidence {evaluation.confidence:.2f}, "
            f"bankroll ${bankroll_after:.2f}, mode {mode}, "
            f"position={position_id})"
        )
        # Telegram rendering follows the SaaS Messages Spec v1.
        telegram_html: str | None = None
        try:
            from feeds import telegram_messages as _tm
            telegram_html = _tm.new_position(
                question=market.question,
                side=decision.side,
                stake_usd=float(decision.stake_usd),
                forecast_pct=forecast_pct_side,
                confidence=float(evaluation.confidence or 0.0),
                bankroll_after=bankroll_after,
                equity_after=equity_after,
                locked_capital=locked_capital_after,
                mode=mode,
            )
        except Exception as exc:
            print(f"[pm_analyst] telegram render failed: {exc}",
                  file=sys.stderr)
        try:
            log_event(
                event_type="position_opened",
                severity=20,
                description=description,
                source="pm_analyst",
                telegram_html=telegram_html,
            )
        except Exception as exc:
            print(f"[pm_analyst] event log write failed: {exc}",
                  file=sys.stderr)


# ── Helpers ──────────────────────────────────────────────────────────────────
def _recently_predicted(market_id: str, days: int) -> bool:
    try:
        with get_engine().begin() as conn:
            row = conn.execute(text(
                "SELECT 1 FROM predictions "
                "WHERE source = :src "
                "  AND subject_key = :sk "
                "  AND created_at >= datetime('now', '-' || :d || ' days') "
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
    decision:         Optional[SizingDecision],
    research_sources: list[str],
    prediction_id:    Optional[int],
    recommendation:   str,
    market_archetype: Optional[str] = None,
    user_id:          Optional[str] = None,
    mode:             Optional[str] = None,
    skip_reason:      Optional[str] = None,
) -> Optional[int]:
    # `decision` may be None for early-return skip paths (force_skip,
    # risk halt, existing-position). Those bail before the sizer
    # runs, but the evaluation still happened and is worth persisting
    # so the Dashboard's Recent Activity surface (and Positions ->
    # Skipped tab) can show the user WHY each market was passed on.
    # Without this, force_skip rejections evaporated and the user
    # saw a silent 5h gap in activity while the bot was actually
    # running normally (2026-05-20 ticket).
    if user_id is None:
        from engine.user_config import DEFAULT_USER_ID
        user_id = DEFAULT_USER_ID
    # Default mode to whatever the user is currently in. The hot-path
    # caller (`_maybe_trade_for_user`) passes it explicitly so we
    # avoid a second DB read per scanned market.
    if mode is None:
        from engine.user_config import get_user_config
        mode = get_user_config(user_id).mode or "simulation"
    try:
        ev_bps = float(decision.ev * 10_000.0) if decision is not None else 0.0
        with get_engine().begin() as conn:
            # Derive an effective skip_reason. Three sources, in priority
            # order:
            #   1. Explicit `skip_reason` arg (from force_skip / risk halt /
            #      existing-position / per-event cap / max-concurrent etc).
            #   2. `decision.skip_reason` from the sizer (direction
            #      disagreement, archetype skip-list, price-band, platform
            #      minimum, etc).
            #   3. None - only on BUY rows where there's nothing to explain.
            effective_skip_reason = skip_reason
            if effective_skip_reason is None and decision is not None:
                effective_skip_reason = getattr(decision, "skip_reason", None)

            row = conn.execute(text(
                "INSERT INTO market_evaluations ("
                "  user_id, market_id, condition_id, slug, question, category, "
                "  market_price_yes, delfi_probability, confidence, "
                "  ev_bps, recommendation, reasoning, reasoning_short, "
                "  research_sources, prediction_id, market_archetype, event_slug, "
                "  mode, skip_reason"
                ") VALUES ("
                "  :user_id, :mid, :cid, :slug, :q, :cat, "
                "  :mp, :cp, :conf, "
                "  :ev_bps, :rec, :reason, :reason_short, "
                "  :srcs, :pid, :arch, :event_slug, "
                "  :mode, :skip_reason"
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
                "ev_bps": ev_bps,
                "rec":  recommendation,
                "reason": evaluation.reasoning,
                "reason_short": (getattr(evaluation, "reasoning_short", "") or None),
                "srcs": json.dumps(research_sources),
                "pid":  prediction_id,
                "arch": market_archetype,
                "event_slug": getattr(market, "event_slug", None),
                "mode": mode,
                "skip_reason": effective_skip_reason,
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
