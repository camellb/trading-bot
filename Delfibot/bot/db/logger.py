"""
DB Logger - thin write helpers for the Polymarket bot.

Only two tables flow through this module: `event_log` and `feed_health_log`.
All other tables (pm_positions, predictions, market_evaluations) are written
directly by the modules that own them - keeping this file small so mass
imports stay cheap.

Every function swallows exceptions and logs to stderr; the caller should
treat every write as best-effort audit data, not a control signal.
"""

import sys
from datetime import datetime, timezone

from db.engine import get_engine
from db.models import event_log, feed_health_log
from engine.user_config import DEFAULT_USER_ID


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
    *,
    telegram_html: str | None = None,
) -> None:
    """Insert a row into event_log + best-effort push to Telegram.

    `description` is the plain-text in-app event copy that the
    dashboard renders. `telegram_html`, when supplied, is the rich
    Telegram-HTML rendering from `feeds.telegram_messages` for the
    same event - it gets pushed instead of the description so
    Telegram output matches the SaaS Messages Spec verbatim.

    The Telegram side-effect is best-effort and gated by
    `should_notify(category=event_type)`. A Telegram outage never
    blocks in-app event surfacing.
    """
    try:
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(
                event_log.insert().values(
                    user_id=DEFAULT_USER_ID,
                    timestamp=datetime.now(timezone.utc),
                    event_type=event_type,
                    severity=severity,
                    description=description,
                    source=source,
                )
            )
    except Exception as exc:
        print(f"[logger] log_event error: {exc}", file=sys.stderr)

    # Outbound Telegram push. Imports are local so a fresh install
    # with no keychain entry doesn't pay the import cost on every
    # event_log write.
    try:
        from engine.user_config import should_notify
        if should_notify(category=event_type):
            from feeds.telegram_notifier import notify as _tg_notify
            _tg_notify(telegram_html or description)
    except Exception as exc:
        print(f"[logger] telegram push error: {exc}", file=sys.stderr)
