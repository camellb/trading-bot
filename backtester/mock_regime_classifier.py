"""
MockRegimeClassifier — wraps the real RegimeClassifier for backtesting.

Overrides the two methods that require live REST data (funding rate history
and OI history) with neutral synthetic values.  All technical calculations
(ADX, EMA slope, realised vol) use the real implementations against mock
candle data.

Neutral values chosen to never trigger crowding / blocking conditions:
  _get_funding_percentile → 50.0  (mid-range; well inside the 25–75 safe band)
  _get_oi_delta           → 0.1   (tiny positive; informational only in Layer C)

Usage:
    mock_rc = MockRegimeClassifier(mock_ws, mock_monitor)
    result  = mock_rc.classify("BTC-USDT-SWAP")
"""

from typing import Optional

from engine.regime_classifier import RegimeClassifier
from backtester.mock_ws_manager import MockWSManager, MockHealthMonitor


class MockRegimeClassifier(RegimeClassifier):
    """
    Real RegimeClassifier with neutral stubs for REST-backed data sources.

    All candle-based calculations (ADX, EMA slope, realised vol percentile)
    run exactly as in production — they read from MockWSManager which returns
    the correct historical slice at the current time pointer.

    Only funding percentile and OI delta are stubbed, because:
      1. Funding rate history is not available from public candle history.
      2. OKX rubik OI history endpoint is not available in the backtester.
    """

    def __init__(
        self,
        ws_manager: MockWSManager,
        health_monitor: MockHealthMonitor,
    ) -> None:
        # Pass deribit_feed=None — IV overlay not used in backtesting.
        super().__init__(ws_manager, health_monitor, deribit_feed=None)

    # ── Stubbed REST-dependent helpers ────────────────────────────────────────

    def _get_funding_percentile(self, pair: str) -> Optional[float]:
        """
        Return neutral 50th-percentile funding rate.
        Prevents crowding blocks (25th/75th thresholds) from firing.
        """
        return 50.0

    def _get_oi_delta(self, pair: str) -> Optional[float]:
        """
        Return small positive OI delta.
        Informational in Layer C — does not block trade entry.
        """
        return 0.1
