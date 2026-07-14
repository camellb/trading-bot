"""User-visible alerts for runtime failures that block new trades."""

from __future__ import annotations

from db.logger import log_event
from feeds import telegram_messages


_ACTIVE_FAILURES: dict[str, str] = {}

_FAILURE_LABELS = {
    "forecast_provider": "Forecast provider unavailable",
    "market_scan": "Market scan failed",
}


def report_failure(kind: str, detail: str) -> None:
    """Record the first active failure of a known trade-blocking kind."""
    if kind in _ACTIVE_FAILURES:
        return
    label = _FAILURE_LABELS.get(kind, "Trading system unavailable")
    _ACTIVE_FAILURES[kind] = detail
    log_event(
        event_type="trading_blocked",
        severity=30,
        description=(
            f"{label}. No new positions can open until Delfi recovers. "
            f"{detail}"
        ),
        source="engine.runtime_alerts",
        telegram_html=telegram_messages.trading_blocked(
            title=label,
            detail=detail,
        ),
    )


def report_recovery(kind: str) -> None:
    """Record recovery only after a matching failure was reported."""
    if kind not in _ACTIVE_FAILURES:
        return
    label = _FAILURE_LABELS.get(kind, "Trading system")
    _ACTIVE_FAILURES.pop(kind, None)
    log_event(
        event_type="trading_blocked",
        severity=10,
        description=f"{label} recovered. Delfi can open new positions again.",
        source="engine.runtime_alerts",
        telegram_html=telegram_messages.trading_restored(title=label),
    )
