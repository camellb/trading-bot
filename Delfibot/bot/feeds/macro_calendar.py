"""
Macro Calendar - manages the scheduled macro event calendar.

Scrapes FOMC meeting dates and BLS CPI/PPI release dates from official sources.
Stores events locally in data/macro_calendar.json. Refreshes weekly.

Per the feed integrity policy: if calendar data is missing or older than
MACRO_CALENDAR_REFRESH_DAYS, report degraded and set EVENT_RISK for all
scheduled-event windows until fresh data is obtained.
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
from db.engine import app_data_dir
from feeds.feed_health_monitor import FeedHealthMonitor

# Local storage path. Anchored to the app-data directory so it works
# when the sidecar is launched by Tauri (cwd=/, where Path("data/...")
# would resolve to a read-only system path and crash the sidecar
# during MacroCalendar.__init__ before it can bind its HTTP port).
_CALENDAR_FILE = app_data_dir() / "data" / "macro_calendar.json"

# Source URLs
_FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
_BLS_CPI_URL = "https://www.bls.gov/schedule/news_release/cpi.htm"
_BLS_PPI_URL = "https://www.bls.gov/schedule/news_release/ppi.htm"

# Months lookup for text scraping
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
}


class MacroCalendar:
    """Loads, refreshes, and queries the scheduled macro event calendar."""

    def __init__(self, health_monitor: FeedHealthMonitor) -> None:
        self._monitor = health_monitor
        self._monitor.register("macro")
        self._events: list[dict] = []
        self._scheduler: Optional[AsyncIOScheduler] = None

        # Ensure data directory exists
        _CALENDAR_FILE.parent.mkdir(parents=True, exist_ok=True)

    # ── Startup ───────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Load local calendar, then schedule weekly refresh via APScheduler."""
        loaded = self._load_local()
        if not loaded or self._is_stale():
            print("[macro_calendar] No valid local calendar - fetching now...")
            await self.refresh()
        else:
            print(
                f"[macro_calendar] Loaded {len(self._events)} events from local cache"
            )
            self._monitor.report_healthy("macro")

        # Schedule weekly refresh
        self._scheduler = AsyncIOScheduler()
        self._scheduler.add_job(
            self.refresh,
            trigger="interval",
            days=config.MACRO_CALENDAR_REFRESH_DAYS,
            id="macro_calendar_refresh",
        )
        self._scheduler.start()
        print("[macro_calendar] Weekly refresh scheduled")

    # ── Data loading ──────────────────────────────────────────────────────────

    def _load_local(self) -> bool:
        """Load events from local JSON file. Returns True on success."""
        if not _CALENDAR_FILE.exists():
            return False
        try:
            with open(_CALENDAR_FILE, "r") as f:
                data = json.load(f)
            self._events = [
                {
                    "date": datetime.fromisoformat(e["date"]),
                    "type": e["type"],
                    "description": e["description"],
                }
                for e in data.get("events", [])
            ]
            return True
        except Exception as exc:
            print(f"[macro_calendar] Failed to load local file: {exc}", file=sys.stderr)
            return False

    def _save_local(self) -> None:
        """Persist events to local JSON file."""
        try:
            data = {
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "events": [
                    {
                        "date": e["date"].isoformat(),
                        "type": e["type"],
                        "description": e["description"],
                    }
                    for e in self._events
                ],
            }
            with open(_CALENDAR_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as exc:
            print(f"[macro_calendar] Failed to save local file: {exc}", file=sys.stderr)

    def _is_stale(self) -> bool:
        """Return True if local file is older than MACRO_CALENDAR_REFRESH_DAYS."""
        if not _CALENDAR_FILE.exists():
            return True
        mtime = datetime.fromtimestamp(
            _CALENDAR_FILE.stat().st_mtime, tz=timezone.utc
        )
        age = datetime.now(timezone.utc) - mtime
        return age > timedelta(days=config.MACRO_CALENDAR_REFRESH_DAYS)

    # ── Scraping ──────────────────────────────────────────────────────────────

    async def refresh(self) -> None:
        """
        Scrape FOMC, CPI, and PPI dates from official sources and save locally.
        On failure, keep stale data (if any) but log a clear warning and report degraded.
        """
        print("[macro_calendar] Refreshing macro calendar...")
        events: list[dict] = []
        current_year = datetime.now(timezone.utc).year

        try:
            async with aiohttp.ClientSession() as session:
                fomc_events = await self._scrape_fomc(session, current_year)
                cpi_events = await self._scrape_bls(session, _BLS_CPI_URL, "CPI", current_year)
                ppi_events = await self._scrape_bls(session, _BLS_PPI_URL, "PPI", current_year)

            events = fomc_events + cpi_events + ppi_events
            # Sort by date ascending
            events.sort(key=lambda e: e["date"])

            if not events:
                raise ValueError("No events scraped - all sources returned empty")

            self._events = events
            self._save_local()
            self._monitor.report_healthy("macro")
            print(
                f"[macro_calendar] Refreshed: {len(events)} events "
                f"({len(fomc_events)} FOMC, {len(cpi_events)} CPI, {len(ppi_events)} PPI)"
            )

        except Exception as exc:
            msg = f"Calendar refresh failed: {exc}"
            print(f"[macro_calendar] WARNING: {msg}", file=sys.stderr)
            if self._events:
                print(
                    "[macro_calendar] Keeping stale data - EVENT_RISK active for "
                    "all scheduled windows until calendar refreshes successfully",
                    file=sys.stderr,
                )
                # Keep stale data but mark degraded so engine activates EVENT_RISK
                self._monitor.report_degraded("macro", msg)
            else:
                self._monitor.report_degraded("macro", msg)

    async def _scrape_fomc(
        self, session: aiohttp.ClientSession, current_year: int
    ) -> list[dict]:
        """
        Scrape FOMC meeting dates from federalreserve.gov.

        Page structure (verified 2026-04-14):
          - Year sections identified by <h4><a id="NNNNN">YYYY FOMC Meetings</a></h4>
          - Within each meeting row:
              <div class="fomc-meeting__month ..."><strong>January</strong></div>
              <div class="fomc-meeting__date ...">27-28</div>
          - NOTE: The page also contains minutes release dates in the format
            "(Released Month Day, Year)" - these must be excluded. The div-based
            parsing correctly avoids them.
        """
        events = []
        headers = {"User-Agent": "Mozilla/5.0 (compatible; trading-bot/1.0)"}
        try:
            async with session.get(
                _FOMC_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                html = await resp.text()

            # Extract year sections from the HTML
            # Year header pattern: matches "<h4><a id="NNNNN">YYYY FOMC Meetings</a></h4>"
            year_header_re = re.compile(
                r'<h4><a id="(\d+)">(\d{4}) FOMC Meetings</a></h4>',
                re.IGNORECASE,
            )
            # Month in the meeting row
            month_re = re.compile(
                r'fomc-meeting__month[^>]*>.*?<strong>([^<]+)</strong>',
                re.DOTALL | re.IGNORECASE,
            )
            # Day range in the meeting row
            date_re = re.compile(
                r'fomc-meeting__date[^>]*>(\d{1,2})(?:[-–]\d{1,2})?',
                re.DOTALL | re.IGNORECASE,
            )

            # Find all year sections and split HTML by them
            year_headers = list(year_header_re.finditer(html))
            for i, yh in enumerate(year_headers):
                year = int(yh.group(2))
                if year < current_year:
                    continue

                # Section: from this year header to the next
                section_start = yh.start()
                section_end = year_headers[i + 1].start() if i + 1 < len(year_headers) else len(html)
                section = html[section_start:section_end]

                # Find meeting rows within this section
                # Each row has both a month div and a date div
                row_re = re.compile(
                    r'fomc-meeting__month.*?fomc-meeting__date.*?(?=fomc-meeting__month|$)',
                    re.DOTALL | re.IGNORECASE,
                )
                for row_match in row_re.finditer(section):
                    row = row_match.group(0)
                    m_month = month_re.search(row)
                    m_date = date_re.search(row)
                    if not m_month or not m_date:
                        continue
                    month_str = m_month.group(1).strip()
                    day_str = m_date.group(1).strip()
                    month = _MONTHS.get(month_str.lower())
                    if not month:
                        continue
                    try:
                        # Use the LAST day of the meeting (the decision day) at 14:00 UTC
                        # FOMC typically announces at ~14:00 ET (19:00 UTC), but we use
                        # the start of the day as the event trigger for pre-window blocking
                        dt = datetime(year, month, int(day_str), 14, 0, tzinfo=timezone.utc)
                        events.append({
                            "date": dt,
                            "type": "FOMC",
                            "description": f"FOMC Meeting {month_str} {day_str}, {year}",
                        })
                    except ValueError:
                        continue

            # Deduplicate
            seen = set()
            unique = []
            for e in events:
                key = e["date"].strftime("%Y-%m-%d")
                if key not in seen:
                    seen.add(key)
                    unique.append(e)
            events = unique

        except Exception as exc:
            print(f"[macro_calendar] FOMC scrape failed: {exc}", file=sys.stderr)

        if not events:
            print(
                "[macro_calendar] FOMC scrape returned 0 events - "
                "using hardcoded fallback",
                file=sys.stderr,
            )
            events = self._hardcoded_fomc_dates(current_year)
        else:
            print(f"[macro_calendar] Scraped {len(events)} FOMC events")
        return events

    @staticmethod
    def _hardcoded_fomc_dates(current_year: int) -> list[dict]:
        """
        Hardcoded FOMC meeting schedule fallback.
        FOMC meets ~8 times per year. Announcement typically at 14:00 ET.
        Source: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
        IMPORTANT: Verify annually. Dates are the last/decision day of each meeting.
        """
        _FOMC_SCHEDULE: dict[int, list[tuple[int, int]]] = {
            2025: [(1, 29), (3, 19), (5, 7), (6, 18), (7, 30), (9, 17), (10, 29), (12, 10)],
            2026: [(1, 28), (3, 18), (4, 29), (6, 17), (7, 29), (9, 16), (11, 4), (12, 16)],
        }
        schedule = _FOMC_SCHEDULE.get(current_year, [])
        if not schedule:
            return []

        now = datetime.now(timezone.utc)
        events = []
        for month, day in schedule:
            try:
                dt = datetime(current_year, month, day, 19, 0, tzinfo=timezone.utc)
                if dt < now:
                    continue
                month_name = dt.strftime("%B")
                events.append({
                    "date": dt,
                    "type": "FOMC",
                    "description": f"FOMC Meeting {month_name} {day}, {current_year} (hardcoded fallback)",
                })
            except ValueError:
                continue
        return events

    async def _scrape_bls(
        self,
        session: aiohttp.ClientSession,
        url: str,
        release_type: str,
        current_year: int,
    ) -> list[dict]:
        """
        Scrape CPI or PPI release dates from bls.gov.
        bls.gov blocks programmatic access (returns 403 for all automated requests).
        Falls back to hardcoded schedule for the current year when scraping fails.
        """
        events = []
        try:
            headers = {"User-Agent": "Mozilla/5.0 (compatible; trading-bot/1.0)"}
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status} - bls.gov may be blocking automated access")
                html = await resp.text()

            pattern = re.compile(
                r"(January|February|March|April|May|June|July|August|"
                r"September|October|November|December)\s+(\d{1,2})[,\s]+(\d{4})",
                re.IGNORECASE,
            )
            for match in pattern.finditer(html):
                month_str, day_str, year_str = match.groups()
                month = _MONTHS.get(month_str.lower())
                if not month:
                    continue
                year = int(year_str)
                if year < current_year:
                    continue
                try:
                    dt = datetime(year, month, int(day_str), 8, 30, tzinfo=timezone.utc)
                    events.append({
                        "date": dt,
                        "type": release_type,
                        "description": f"BLS {release_type} Release {month_str} {day_str}, {year}",
                    })
                except ValueError:
                    continue

            seen = set()
            unique = []
            for e in events:
                key = e["date"].strftime("%Y-%m-%d")
                if key not in seen:
                    seen.add(key)
                    unique.append(e)
            events = unique

        except Exception as exc:
            print(
                f"[macro_calendar] {release_type} scrape failed: {exc} - "
                f"using hardcoded fallback dates for {current_year}",
                file=sys.stderr,
            )
            events = self._hardcoded_bls_dates(release_type, current_year)

        if not events:
            print(
                f"[macro_calendar] {release_type}: 0 events found; "
                "using hardcoded fallback",
                file=sys.stderr,
            )
            events = self._hardcoded_bls_dates(release_type, current_year)
        else:
            print(f"[macro_calendar] {release_type}: {len(events)} events loaded")
        return events

    @staticmethod
    def _hardcoded_bls_dates(release_type: str, year: int) -> list[dict]:
        """
        Hardcoded BLS release schedule fallback.
        bls.gov blocks all automated access; these dates are sourced from the
        published BLS News Release calendar and are updated annually.
        CPI is typically released mid-month ~8:30 AM ET (~13:30 UTC).
        PPI is typically released the day before CPI ~8:30 AM ET (~13:30 UTC).
        IMPORTANT: Verify these dates before each year's trading commences.
        Source: https://www.bls.gov/schedule/news_release/cpi.htm (manual check)
        """
        # Month → (CPI day, PPI day) for each year
        _SCHEDULE: dict[int, dict[int, tuple[int, int]]] = {
            2025: {
                1: (15, 14), 2: (12, 11), 3: (12, 11), 4: (10, 9),
                5: (13, 12), 6: (11, 10), 7: (15, 14), 8: (12, 11),
                9: (10, 9),  10: (15, 14), 11: (13, 12), 12: (10, 9),
            },
            2026: {
                1: (14, 13), 2: (11, 10), 3: (11, 10), 4: (10, 9),
                5: (13, 12), 6: (11, 10), 7: (15, 14), 8: (12, 11),
                9: (11, 10), 10: (14, 13), 11: (13, 12), 12: (11, 10),
            },
        }
        schedule = _SCHEDULE.get(year, {})
        if not schedule:
            print(
                f"[macro_calendar] No hardcoded {release_type} dates for {year} - "
                "add to _hardcoded_bls_dates()",
                file=sys.stderr,
            )
            return []

        events = []
        now = datetime.now(timezone.utc)
        for month, (cpi_day, ppi_day) in schedule.items():
            day = cpi_day if release_type == "CPI" else ppi_day
            try:
                dt = datetime(year, month, day, 13, 30, tzinfo=timezone.utc)
                if dt < now:
                    continue
                month_name = dt.strftime("%B")
                events.append({
                    "date": dt,
                    "type": release_type,
                    "description": (
                        f"BLS {release_type} Release {month_name} {day}, {year} "
                        f"(hardcoded fallback - verify at bls.gov)"
                    ),
                })
            except ValueError:
                continue

        return events

    # ── Query API ─────────────────────────────────────────────────────────────

    def is_event_window_active(self) -> bool:
        """
        Return True if current time is within EVENT_PRE_WINDOW_HOURS before or
        EVENT_POST_WINDOW_HOURS after any scheduled event.
        If macro feed is degraded, return True conservatively (EVENT_RISK).
        """
        if not self._monitor.is_healthy("macro"):
            return True  # Conservative: treat as event risk when data missing

        now = datetime.now(timezone.utc)
        pre_window = timedelta(hours=config.EVENT_PRE_WINDOW_HOURS)
        post_window = timedelta(hours=config.EVENT_POST_WINDOW_HOURS)

        for event in self._events:
            event_dt = event["date"]
            if not event_dt.tzinfo:
                event_dt = event_dt.replace(tzinfo=timezone.utc)
            if (event_dt - pre_window) <= now <= (event_dt + post_window):
                return True
        return False

    def get_upcoming_events(self, days: int = 7) -> list[dict]:
        """
        Return all scheduled events between now and now + days.
        Each item: {"date": date, "type": str, "description": str, "days_away": int}
        Sorted by date ascending.
        """
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(days=days)
        results = []
        for event in self._events:
            event_dt = event["date"]
            if not event_dt.tzinfo:
                event_dt = event_dt.replace(tzinfo=timezone.utc)
            if now <= event_dt <= cutoff:
                days_away = (event_dt.date() - now.date()).days
                results.append({
                    "date": event_dt.date(),
                    "type": event["type"],
                    "description": event["description"],
                    "days_away": days_away,
                })
        results.sort(key=lambda e: e["date"])
        return results

    def get_next_event(self) -> Optional[dict]:
        """Return the next upcoming event or None."""
        now = datetime.now(timezone.utc)
        future = [
            e for e in self._events
            if (e["date"].replace(tzinfo=timezone.utc)
                if not e["date"].tzinfo else e["date"]) > now
        ]
        if not future:
            return None
        future.sort(key=lambda e: e["date"])
        next_e = future[0]
        return {
            "date": next_e["date"].isoformat(),
            "type": next_e["type"],
            "description": next_e["description"],
        }
