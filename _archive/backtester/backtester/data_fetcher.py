"""
HistoricalDataFetcher — fetches OHLCV candle history from OKX REST API.

Endpoint: GET /api/v5/market/history-candles
  Returns closed historical candles for any supported interval.
  Max 100 candles per request. Newest-first ordering.
  Pagination via `after` param (return data older than this timestamp ms).

Supports up to ~3 months of 15m data (≈8700 candles for 90 days).

Usage:
    from datetime import date
    fetcher = HistoricalDataFetcher()
    candles = fetcher.fetch_historical_candles(
        "BTC-USDT-SWAP", "15m",
        date(2025, 1, 1), date(2025, 2, 1)
    )
    # Returns list of dicts: {open_time, open, high, low, close, volume, closed}
    # Ordered oldest-first.
"""

import time
import sys
from datetime import date, datetime, timezone

import requests

_REST_BASE = "https://www.okx.com"
_PAGE_LIMIT = 100        # OKX max per request
_RATE_LIMIT_S = 0.12     # pause between requests (conservative)
_TIMEOUT_S = 15


class HistoricalDataFetcher:
    """
    Fetches historical OHLCV data from OKX REST API for backtesting.

    All returned candles have `closed=True` (in-progress candles are excluded).
    Data is returned oldest-first so it can be fed directly to MockWSManager.
    """

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "trading-bot-backtest/1.0"})

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch_historical_candles(
        self,
        pair: str,
        interval: str,
        start_date: date,
        end_date: date,
    ) -> list[dict]:
        """
        Fetch all closed candles for `pair`/`interval` in [start_date, end_date].

        Paginates through the OKX history-candles endpoint, oldest-first on return.
        Each candle dict has keys: open_time (int ms), open, high, low, close,
        volume (float), closed (True).

        Args:
            pair:       OKX instrument ID, e.g. "BTC-USDT-SWAP"
            interval:   bar size string, e.g. "15m", "1H"
            start_date: first day to include (inclusive)
            end_date:   last day to include (inclusive)
        """
        start_ms = int(
            datetime(start_date.year, start_date.month, start_date.day,
                     tzinfo=timezone.utc).timestamp() * 1000
        )
        # end_date inclusive → use start of next day as exclusive upper bound
        end_dt = datetime(end_date.year, end_date.month, end_date.day,
                          23, 59, 59, tzinfo=timezone.utc)
        end_ms = int(end_dt.timestamp() * 1000)

        all_candles: list[dict] = []
        # Pagination cursor: fetch candles older than this timestamp
        after_ms = end_ms + 1
        pages = 0

        print(
            f"[data_fetcher] Fetching {pair} {interval} "
            f"from {start_date} to {end_date}..."
        )

        while True:
            candles, oldest_ts = self._fetch_page(pair, interval, after_ms)
            pages += 1

            if not candles:
                print(
                    f"[data_fetcher] {pair} {interval}: empty page at after={after_ms}, "
                    f"stopping."
                )
                break

            # Keep only candles within our date range
            for c in candles:
                if start_ms <= c["open_time"] <= end_ms:
                    all_candles.append(c)

            # Stop if oldest candle in this page is already before start_date
            if oldest_ts is not None and oldest_ts < start_ms:
                break

            # No more data (got fewer than a full page)
            if len(candles) < _PAGE_LIMIT:
                break

            # Move cursor back for next page
            after_ms = oldest_ts - 1
            time.sleep(_RATE_LIMIT_S)

        # Sort oldest-first (OKX returns newest-first per page)
        all_candles.sort(key=lambda c: c["open_time"])

        print(
            f"[data_fetcher] {pair} {interval}: {len(all_candles)} closed candles "
            f"in {pages} pages."
        )
        return all_candles

    def prefetch_all_pairs(
        self,
        pairs: list[str],
        interval: str,
        start_date: date,
        end_date: date,
    ) -> dict[str, list[dict]]:
        """
        Fetch historical candles for all pairs and return as a nested dict.

        Returns: {pair: [candle_dicts]}  (each list is oldest-first)
        """
        result: dict[str, list[dict]] = {}
        for pair in pairs:
            result[pair] = self.fetch_historical_candles(
                pair, interval, start_date, end_date
            )
            if pair != pairs[-1]:
                time.sleep(_RATE_LIMIT_S)
        return result

    # ── Internal ──────────────────────────────────────────────────────────────

    def _fetch_page(
        self, pair: str, interval: str, after_ms: int
    ) -> tuple[list[dict], int | None]:
        """
        Fetch one page of historical candles from OKX.

        Returns (candles_newest_first, oldest_ts_in_page).
        On error returns ([], None).

        OKX history-candles endpoint behaviour:
          after param: return candles with open_time < after_ms
          limit param: max 100 candles per request
          response: newest-first within the page
        """
        params = {
            "instId": pair,
            "bar":    interval,
            "limit":  str(_PAGE_LIMIT),
            "after":  str(after_ms),
        }
        try:
            resp = self._session.get(
                f"{_REST_BASE}/api/v5/market/history-candles",
                params=params,
                timeout=_TIMEOUT_S,
            )
            resp.raise_for_status()
            raw = resp.json()
        except Exception as exc:
            print(
                f"[data_fetcher] HTTP error for {pair} {interval} "
                f"after={after_ms}: {exc}",
                file=sys.stderr,
            )
            return [], None

        entries = raw.get("data", [])
        if not entries:
            return [], None

        candles = []
        for entry in entries:
            # entry: [ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm]
            if entry[8] != "1":          # skip in-progress candles
                continue
            candles.append({
                "open_time": int(entry[0]),
                "open":      float(entry[1]),
                "high":      float(entry[2]),
                "low":       float(entry[3]),
                "close":     float(entry[4]),
                "volume":    float(entry[6]),  # volCcy = base asset volume
                "closed":    True,
            })

        oldest_ts = int(entries[-1][0]) if entries else None
        return candles, oldest_ts
