"""
Layer C — Crypto Confirmation.

Validates the directional signal against derivatives market structure using:
  - Funding rate percentile (live from ticker WS + historical REST, cached 4h)
  - OI delta on 15m (live from ticker WS or REST fallback if field absent)
  - Mark/spot basis (Binance mark price WebSocket vs index price)

If OI or funding fields are absent from the ticker payload, sources from the
nearest authoritative Binance REST endpoint and caches consistently. Logs any
schema mismatch. Does not block trading if REST fallback succeeds.

Confirmation logic per signal direction:
  LONG:
    - Block if funding_pct >= FUNDING_EXTREME_PERCENTILE (longs are overcrowded)
    - Block if basis_pct > BASIS_PREMIUM_MAX_PCT (mark too far above index)
  SHORT:
    - Block if funding_pct <= FUNDING_SHORTS_CROWDED_PERCENTILE (shorts overcrowded)
    - Block if basis_pct < BASIS_DISCOUNT_MAX_PCT (mark too far below index)

OI delta is informational context — not a hard block.
Returns confirmed (bool), size_multiplier (always 1.0 at this layer), reason.
"""

import sys
from typing import TYPE_CHECKING, Optional

import config
from feeds.okx_ws import OKXWebSocketManager
from feeds.feed_health_monitor import FeedHealthMonitor

if TYPE_CHECKING:
    from engine.regime_classifier import RegimeClassifier


class CryptoConfirmation:
    """
    Layer C: validates directional signal against derivatives market structure.

    Takes a RegimeClassifier instance to reuse its cached funding and OI data
    (avoids duplicate REST calls for data already fetched by Layer A).

    Call evaluate(pair, signal) to get confirmation dict with keys:
      confirmed (bool), size_multiplier (float), reason (str),
      funding_pct (float|None), oi_delta (float|None), basis_pct (float|None)
    """

    def __init__(
        self,
        ws_manager: OKXWebSocketManager,
        health_monitor: FeedHealthMonitor,
        regime_classifier: "RegimeClassifier",
    ) -> None:
        self._ws = ws_manager
        self._monitor = health_monitor
        self._rc = regime_classifier

    # ── Basis calculation ─────────────────────────────────────────────────────

    def _get_basis_pct(self, pair: str) -> Optional[float]:
        """
        Return (mark_price - index_price) / index_price * 100.
        Positive = mark trading at a premium to spot index.
        Negative = mark trading at a discount.
        Returns None if ticker data unavailable.
        """
        ticker = self._ws.get_latest_ticker(pair)
        if not ticker:
            return None
        mark = ticker.get("mark_price")
        index = ticker.get("index_price")
        if mark is None or index is None or index == 0:
            return None
        return (mark - index) / index * 100.0

    # ── Main evaluation ───────────────────────────────────────────────────────

    def evaluate(self, pair: str, signal: str) -> dict:
        """
        Evaluate Layer C for the given directional signal.

        signal: 'LONG', 'SHORT', or 'NEUTRAL'

        Returns dict:
          confirmed (bool)
          size_multiplier (float) — always 1.0; size adjustments are in Layer E
          reason (str)
          funding_pct (float|None)
          oi_delta (float|None)
          basis_pct (float|None)
        """
        result: dict = {
            "confirmed": False,
            "size_multiplier": 1.0,
            "reason": "",
            "funding_pct": None,
            "oi_delta": None,
            "basis_pct": None,
        }

        if signal == "NEUTRAL":
            result["reason"] = "signal is NEUTRAL — Layer C not applicable"
            return result

        # ── Gather indicators ─────────────────────────────────────────────────
        # Reuse RegimeClassifier's cached funding/OI data (same REST source)
        funding_pct = self._rc._get_funding_percentile(pair)
        oi_delta = self._rc._get_oi_delta(pair)
        basis_pct = self._get_basis_pct(pair)

        result["funding_pct"] = funding_pct
        result["oi_delta"] = oi_delta
        result["basis_pct"] = basis_pct

        blocks: list[str] = []
        notes: list[str] = []

        # ── Funding rate check ────────────────────────────────────────────────
        if funding_pct is not None:
            if signal == "LONG" and funding_pct >= config.FUNDING_EXTREME_PERCENTILE:
                blocks.append(
                    f"longs overcrowded (funding_pct={funding_pct:.1f} >= "
                    f"{config.FUNDING_EXTREME_PERCENTILE})"
                )
            elif signal == "SHORT" and funding_pct <= config.FUNDING_SHORTS_CROWDED_PERCENTILE:
                blocks.append(
                    f"shorts overcrowded (funding_pct={funding_pct:.1f} <= "
                    f"{config.FUNDING_SHORTS_CROWDED_PERCENTILE})"
                )

        # ── Basis check ───────────────────────────────────────────────────────
        if basis_pct is not None:
            if signal == "LONG" and basis_pct > config.BASIS_PREMIUM_MAX_PCT:
                blocks.append(
                    f"basis premium too high for LONG "
                    f"(basis_pct={basis_pct:.3f}% > {config.BASIS_PREMIUM_MAX_PCT}%)"
                )
            elif signal == "SHORT" and basis_pct < config.BASIS_DISCOUNT_MAX_PCT:
                blocks.append(
                    f"basis discount too deep for SHORT "
                    f"(basis_pct={basis_pct:.3f}% < {config.BASIS_DISCOUNT_MAX_PCT}%)"
                )

        # ── OI delta (informational — not a hard block) ───────────────────────
        if oi_delta is not None:
            if oi_delta > 0:
                notes.append(f"oi_delta={oi_delta:+.3f}% (fresh participation)")
            else:
                notes.append(f"oi_delta={oi_delta:+.3f}% (covering/reducing)")

        # ── Build result ──────────────────────────────────────────────────────
        fp_str = f"{funding_pct:.1f}" if funding_pct is not None else "N/A"
        basis_str = f"{basis_pct:.3f}%" if basis_pct is not None else "N/A"
        note_str = f" [{'; '.join(notes)}]" if notes else ""

        if blocks:
            result["confirmed"] = False
            result["reason"] = (
                f"Layer C BLOCK ({signal}): {'; '.join(blocks)}{note_str}"
            )
        else:
            result["confirmed"] = True
            result["reason"] = (
                f"Layer C OK ({signal}): funding_pct={fp_str}, "
                f"basis={basis_str}{note_str}"
            )

        return result
