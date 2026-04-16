"""
Layer E — Event Overlay.

Bifurcated into two sub-systems:

  1. Deterministic scheduled calendar: checks Fed FOMC and BLS CPI/PPI windows
     loaded from macro_calendar. A configurable pre/post window
     (EVENT_PRE_WINDOW_HOURS / EVENT_POST_WINDOW_HOURS) sets EVENT_RISK.
     Scheduled windows always block entirely regardless of Claude score.

  2. Probabilistic Claude severity classifier: sends the last
     CLAUDE_NEWS_HEADLINES_COUNT headlines to claude-sonnet-4-20250514.
     Returns severity 1–10. Score >= CLAUDE_SEVERITY_BLOCK_THRESHOLD blocks
     entirely; score >= CLAUDE_SEVERITY_EVENT_RISK_THRESHOLD sets EVENT_RISK at
     25% size; score >= CLAUDE_SEVERITY_REDUCE_THRESHOLD reduces size by 50%.

Each Claude call is stateless with no conversation history.

evaluate() is async because it may call the Claude API.

Returns:
  regime_override: 'EVENT_RISK' or None
  size_multiplier: float (0.0 = fully blocked, 1.0 = no adjustment)
  blocked: bool
  reason: str
  severity: int | None
"""

import os
import re
import sys
from typing import Optional

import config
from db import logger as db_logger
from feeds.feed_health_monitor import FeedHealthMonitor
from feeds.macro_calendar import MacroCalendar
from feeds.news_feed import NewsFeed

# Import Anthropic SDK conditionally — handles missing install gracefully
try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False
    print(
        "[event_overlay] WARNING: anthropic SDK not installed — "
        "Claude severity scoring will be skipped",
        file=sys.stderr,
    )


class EventOverlay:
    """
    Layer E: event risk detection via scheduled macro calendar and Claude severity
    scoring.

    evaluate() is async because it may call the Claude API.
    """

    def __init__(
        self,
        macro_calendar: MacroCalendar,
        news_feed: NewsFeed,
        health_monitor: FeedHealthMonitor,
    ) -> None:
        self._calendar = macro_calendar
        self._news = news_feed
        self._monitor = health_monitor

        # Cache for inspection / logging
        self._last_severity: Optional[int] = None
        self._last_severity_text: str = ""

    # ── Main evaluation ───────────────────────────────────────────────────────

    async def evaluate(self, pair: Optional[str] = None) -> dict:
        """
        Evaluate event overlay. pair is accepted but not currently used
        (overlay is global, not pair-specific).

        Returns dict:
          regime_override (str|None) — 'EVENT_RISK' or None
          size_multiplier (float)    — 1.0 / 0.5 / 0.25 / 0.0
          blocked (bool)             — True = trade blocked entirely
          reason (str)
          severity (int|None)        — Claude severity score 1–10
        """
        result: dict = {
            "regime_override": None,
            "size_multiplier": 1.0,
            "blocked": False,
            "reason": "",
            "severity": None,
        }

        # ── 1. Scheduled macro window check ───────────────────────────────────
        # Scheduled windows (FOMC / CPI / PPI) always block entirely.
        if self._calendar.is_event_window_active():
            next_event = self._calendar.get_next_event()
            if next_event:
                event_str = f"{next_event['type']} at {next_event['date']}"
            else:
                # We are inside a post-window — next event is already past
                event_str = "scheduled macro event (window active)"

            result["regime_override"] = "EVENT_RISK"
            result["blocked"] = True
            result["size_multiplier"] = 0.0
            result["reason"] = f"scheduled macro window active: {event_str}"
            return result

        # ── 2. Claude severity scoring ─────────────────────────────────────────
        severity = await self._get_claude_severity()
        result["severity"] = severity

        if severity is None:
            # Claude unavailable — apply news-degraded size multiplier if feed is down
            if self._news.is_degraded():
                result["size_multiplier"] = config.NEWS_DEGRADED_SIZE_MULTIPLIER
                result["reason"] = (
                    "news/Claude unavailable — "
                    f"{int(config.NEWS_DEGRADED_SIZE_MULTIPLIER * 100)}% size "
                    "(NEWS_DEGRADED_SIZE_MULTIPLIER)"
                )
            else:
                result["reason"] = "Claude severity unavailable — no size adjustment"
            return result

        # Apply severity rules per CLAUDE.md hierarchy
        if severity >= config.CLAUDE_SEVERITY_BLOCK_THRESHOLD:
            result["regime_override"] = "EVENT_RISK"
            result["blocked"] = True
            result["size_multiplier"] = 0.0
            result["reason"] = (
                f"Claude severity={severity} >= "
                f"{config.CLAUDE_SEVERITY_BLOCK_THRESHOLD} — blocked entirely"
            )
        elif severity >= config.CLAUDE_SEVERITY_EVENT_RISK_THRESHOLD:
            result["regime_override"] = "EVENT_RISK"
            result["size_multiplier"] = config.SIZE_MULTIPLIER_EVENT_RISK
            result["reason"] = (
                f"Claude severity={severity} >= "
                f"{config.CLAUDE_SEVERITY_EVENT_RISK_THRESHOLD} — "
                f"EVENT_RISK at {int(config.SIZE_MULTIPLIER_EVENT_RISK * 100)}% size"
            )
        elif severity >= config.CLAUDE_SEVERITY_REDUCE_THRESHOLD:
            result["size_multiplier"] = config.NEWS_DEGRADED_SIZE_MULTIPLIER
            result["reason"] = (
                f"Claude severity={severity} >= "
                f"{config.CLAUDE_SEVERITY_REDUCE_THRESHOLD} — "
                f"{int(config.NEWS_DEGRADED_SIZE_MULTIPLIER * 100)}% size"
            )
        else:
            result["reason"] = (
                f"Claude severity={severity} < "
                f"{config.CLAUDE_SEVERITY_REDUCE_THRESHOLD} — no size adjustment"
            )

        return result

    # ── Claude severity call ───────────────────────────────────────────────────

    async def _get_claude_severity(self) -> Optional[int]:
        """
        Send the latest headlines to Claude and parse a severity score 1–10.

        Each call is stateless — no conversation history, no memory.
        Returns None on any failure (API error, no key, no headlines, bad response).
        """
        if not _ANTHROPIC_AVAILABLE:
            return None

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            print("[event_overlay] ANTHROPIC_API_KEY not set", file=sys.stderr)
            return None

        headlines = self._news.get_latest_headlines(config.CLAUDE_NEWS_HEADLINES_COUNT)
        if not headlines:
            # No headlines available — skip severity scoring
            return None

        headline_text = "\n".join(f"- {h}" for h in headlines)
        prompt = (
            "Rate the market risk severity of these crypto/macro news headlines "
            "on a scale of 1–10, where:\n"
            "  1 = no market risk\n"
            " 10 = extreme systemic risk (e.g. exchange hack, regulatory ban, "
            "major protocol failure, systemic contagion)\n\n"
            "Return only a single integer and nothing else.\n\n"
            f"Headlines:\n{headline_text}"
        )

        try:
            client = anthropic.Anthropic(api_key=api_key)
            message = client.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=config.CLAUDE_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
            text = message.content[0].text.strip()
            self._last_severity_text = text

            # Extract the first integer 1–10 from the response
            match = re.search(r"\b(10|[1-9])\b", text)
            if match:
                severity = int(match.group(1))
                self._last_severity = severity
                print(
                    f"[event_overlay] Claude severity={severity} "
                    f"(raw={text!r})"
                )
                db_logger.log_event(
                    event_type="news_severity",
                    severity=severity,
                    description=headline_text[:500],
                    source="claude_severity",
                )
                return severity
            else:
                print(
                    f"[event_overlay] Claude returned non-integer: {text!r}",
                    file=sys.stderr,
                )
                return None

        except Exception as exc:
            print(f"[event_overlay] Claude API error: {exc}", file=sys.stderr)
            return None

    # ── Accessors ─────────────────────────────────────────────────────────────

    def get_last_severity(self) -> Optional[int]:
        """Return the most recently cached Claude severity score, or None."""
        return self._last_severity
