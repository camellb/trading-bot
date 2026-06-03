"""
DB Logger - thin write helpers for the Polymarket bot.

Only two tables flow through this module: `event_log` and `feed_health_log`.
All other tables (pm_positions, predictions, market_evaluations) are written
directly by the modules that own them - keeping this file small so mass
imports stay cheap.

Every function swallows exceptions and logs to stderr; the caller should
treat every write as best-effort audit data, not a control signal.
"""

import hashlib
import sys
import time
from datetime import datetime, timezone

from db.engine import get_engine
from db.models import event_log, feed_health_log
from engine.user_config import DEFAULT_USER_ID


# ── Notification dedupe ─────────────────────────────────────────────────────
# Process-level throttle so repeat retries on the same position don't spam
# Telegram. The exit-policy evaluator fires every 60s, so on a failed SELL
# (Polymarket outage, post-only mode, position locked in a prior LIVE
# order, etc.) the SAME `order_error` event would fire every minute until
# the underlying condition clears. 2026-05-29 incident: 47 Telegram
# messages on Position #363 in 47 minutes.
#
# Dedupe key: sha1(event_type + first 100 chars of description). The
# first 100 chars carry the constant prefix + market title for any of
# our event types ("Early-exit SELL rejected on 'Question text'..."),
# so two retries with different error-detail tails still dedupe to the
# same fingerprint.
#
# The SQL write to event_log is NEVER throttled - the dashboard's
# activity feed shows every single event. Only the outbound Telegram
# push is suppressed. The user sees one message, the engineer can dig
# into the full retry sequence on the dashboard.
_NOTIFICATION_THROTTLE_CACHE: dict[str, float] = {}
_NOTIFICATION_THROTTLE_SECONDS = 600.0  # 10 minutes
_NOTIFICATION_THROTTLE_PRUNE_AT = 200   # prune the cache when it grows past this


def _should_throttle_notification(event_type: str, description: str) -> bool:
    """Return True iff a notification with this fingerprint was already
    pushed within the last _NOTIFICATION_THROTTLE_SECONDS.

    Mutates `_NOTIFICATION_THROTTLE_CACHE` on the False path (records
    the just-sent fingerprint with its timestamp). On the True path
    leaves the cache untouched - the existing timestamp stays as the
    anchor for the throttle window.

    Categories that should always pass (settlement, daily_summary, etc.)
    are filtered by the caller via `should_notify`; we don't need to
    enumerate them here. A bug that wrongly throttles a settlement
    surfaces in the dashboard event_log immediately because the SQL
    write is independent.
    """
    fingerprint_input = f"{event_type}:{description[:100]}"
    fp = hashlib.sha1(fingerprint_input.encode("utf-8")).hexdigest()
    now = time.monotonic()

    # Prune stale entries opportunistically when the cache grows.
    if len(_NOTIFICATION_THROTTLE_CACHE) >= _NOTIFICATION_THROTTLE_PRUNE_AT:
        cutoff = now - _NOTIFICATION_THROTTLE_SECONDS
        stale_keys = [
            k for k, ts in _NOTIFICATION_THROTTLE_CACHE.items()
            if ts < cutoff
        ]
        for k in stale_keys:
            _NOTIFICATION_THROTTLE_CACHE.pop(k, None)

    last_ts = _NOTIFICATION_THROTTLE_CACHE.get(fp)
    if last_ts is not None and (now - last_ts) < _NOTIFICATION_THROTTLE_SECONDS:
        return True
    _NOTIFICATION_THROTTLE_CACHE[fp] = now
    return False


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
    #
    # Telegram push is GATED on `telegram_html` being non-None. The
    # raw `description` field is the dashboard's audit copy; it's
    # never push-worthy on its own (it carries developer-format
    # strings like "PolyApiException[status_code=400, error_message=
    # {'error': 'not enough balance / allowance...'}]" that read
    # like garbage in a chat client). Every event type the user
    # actually receives MUST come with a hand-crafted card from
    # `feeds/telegram_messages.py`. If a future contributor calls
    # log_event without telegram_html for a category that's in
    # NOTIFICATION_CATEGORIES, we skip the push entirely AND emit a
    # loud stderr warning so the gap surfaces in the next session
    # log. Prior behaviour (fall back to `description`) silently
    # leaked raw exception text into the user's Telegram feed; bug
    # class fixed 2026-06-02 (v1.5.54/.55/.56 chain).
    #
    # Throttle gate: if an identical-fingerprint notification was
    # already pushed within the last 10 min, skip the Telegram push
    # (the SQL write above is unaffected so the dashboard activity
    # feed still shows every event). Prevents the 47-message
    # spam-storm class of failure (2026-05-29 Bitcoin SELL retries).
    try:
        from engine.user_config import (
            should_notify, NOTIFICATION_CATEGORIES,
        )
        if should_notify(category=event_type):
            if telegram_html is None:
                # Engineer-visible warning, never user-visible. Only
                # noisy for categories the user expects to see in
                # Telegram; ad-hoc audit-only events still log to
                # event_log without pestering stderr.
                if event_type in NOTIFICATION_CATEGORIES:
                    print(
                        f"[logger] missing telegram_html for "
                        f"event_type={event_type!r} source={source!r}; "
                        f"skipping Telegram push (raw description is "
                        f"never sent to chat). Add a template in "
                        f"feeds/telegram_messages.py and pass it via "
                        f"telegram_html=.",
                        file=sys.stderr,
                    )
                return
            if _should_throttle_notification(event_type, description):
                print(
                    f"[logger] telegram push throttled (event_type="
                    f"{event_type!r}, fp first-100={description[:100]!r})",
                    file=sys.stderr,
                )
            else:
                from feeds.telegram_notifier import notify as _tg_notify
                _tg_notify(telegram_html)
    except Exception as exc:
        print(f"[logger] telegram push error: {exc}", file=sys.stderr)
