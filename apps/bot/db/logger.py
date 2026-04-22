"""
DB Logger — thin write helpers for the Polymarket bot.

Only two tables flow through this module: `event_log` and `feed_health_log`.
All other tables (pm_positions, predictions, market_evaluations) are written
directly by the modules that own them — keeping this file small so mass
imports stay cheap.

Every function swallows exceptions and logs to stderr; the caller should
treat every write as best-effort audit data, not a control signal.
"""

import sys
from datetime import datetime, timezone

from db.engine import get_engine
from db.models import event_log, feed_health_log


def log_feed_health(feed_name: str, state: str, detail: str | None) -> None:
    """Insert a row into feed_health_log."""
    try:
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(
                feed_health_log.insert().values(
                    timestamp=datetime.now(timezone.utc),
                    feed_name=feed_name,
                    state=state,
                    detail=detail,
                )
            )
    except Exception as exc:
        print(f"[logger] log_feed_health error: {exc}", file=sys.stderr)


def log_event(
    event_type: str,
    severity: int | None,
    description: str,
    source: str,
) -> None:
    """Insert a row into event_log."""
    try:
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(
                event_log.insert().values(
                    timestamp=datetime.now(timezone.utc),
                    event_type=event_type,
                    severity=severity,
                    description=description,
                    source=source,
                )
            )
    except Exception as exc:
        print(f"[logger] log_event error: {exc}", file=sys.stderr)
