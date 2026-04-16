"""
Order Manager — places orders via CCXT (OKX connector).

CCXT is used exclusively for order placement and account actions. Market data
WebSocket connections are the intentional, documented exception to this boundary.

In PAPER_MODE = True, simulates fills at mid-price with a conservative slippage
estimate rather than submitting real orders. OKX Demo Trading is used for paper
mode (separate demo account, separate credentials). The demo WS URL is also
different — see feeds/okx_ws.py.

API key safety contract:
  - Bot key must have TRADE permissions only.
  - Withdrawal must be disabled.
  - This is asserted on every startup via _verify_api_permissions().
  - PAPER_MODE always uses OKX Demo Trading credentials (OKX_DEMO_*).
  - Live mode uses OKX_API_KEY / OKX_API_SECRET / OKX_PASSPHRASE.

OKX requires THREE credentials (vs Binance two):
  apiKey     — API key
  secret     — API secret
  password   — API passphrase (OKX-specific, required for all signed requests)

CCXT order type mapping (OKX perpetual swaps):
  Entry:       'limit'
  Stop-loss:   'stop_market' with params={'stopPrice': price, 'reduceOnly': True}
  Take-profit: 'take_profit_market' with params={'stopPrice': price, 'reduceOnly': True}

CCXT symbol normalisation:
  config.TRADING_PAIRS uses OKX instrument IDs: "BTC-USDT-SWAP"
  CCXT unified symbol for OKX perpetuals:       "BTC/USDT:USDT"
  Use _to_ccxt_symbol() to convert before CCXT calls.
"""

import asyncio
import os
import sys
import time as _time
import uuid
from datetime import datetime, timezone
from typing import Optional

import ccxt

import config
from db import logger as db_logger
from feeds.feed_health_monitor import FeedHealthMonitor

# Conservative paper-mode slippage (0.05% of entry price)
_PAPER_SLIPPAGE_PCT = 0.0005

# Live order fill poll interval and timeout
_ORDER_POLL_INTERVAL_S = 2
_ORDER_TIMEOUT_S = 30

# Remaining base-asset quantity below which a position is considered flat
# OKX minimum order size is ~0.001 BTC; anything below this is a dust residual.
_FLAT_POSITION_THRESHOLD_QTY = 0.001


def _to_ccxt_symbol(okx_id: str) -> str:
    """
    Convert OKX instrument ID to CCXT unified symbol.
    'BTC-USDT-SWAP' → 'BTC/USDT:USDT'
    Required because config.TRADING_PAIRS stores OKX IDs but CCXT expects
    its own unified format for order placement.
    """
    parts = okx_id.split("-")    # ["BTC", "USDT", "SWAP"]
    base, quote = parts[0], parts[1]
    return f"{base}/{quote}:{quote}"


class OrderManager:
    """
    Manages order placement, cancellation, and account balance queries.

    Paper mode: connects to OKX Demo Trading (separate account + WS URL).
    Live mode:  places real limit orders on OKX USDT-M perpetual swaps,
                priced 1 tick inside the spread for fast maker fills.
    """

    def __init__(self, health_monitor: FeedHealthMonitor) -> None:
        self._monitor = health_monitor
        self._exchange: Optional[ccxt.okx] = None
        self._keys_configured: bool = False
        self._markets_cache: dict = {}   # lazy-loaded on first live order
        self._trading_halted: bool = False   # set on ambiguous order state; cleared by /resume
        self._notifier   = None   # Optional TelegramNotifier — wired by main.py
        self._ws_manager = None   # Optional OKXWebSocketManager — wired by main.py for mark-price
        # Balance cache — avoids repeated CCXT calls; live mode raises RuntimeError if stale
        self._last_known_balance: Optional[float] = None
        self._last_balance_ts: Optional[float] = None
        self._init_exchange()

    # ── Exchange initialisation ───────────────────────────────────────────────

    def _init_exchange(self) -> None:
        """
        Initialise CCXT OKX exchange.
        Paper mode → OKX Demo Trading credentials + set_sandbox_mode(True).
        Live mode  → OKX live credentials.

        OKX requires three credentials: apiKey, secret, password (passphrase).
        Demo Trading uses separate credentials (OKX_DEMO_*) and a separate
        account — it is NOT a sandbox flag on the main account.
        """
        if config.PAPER_MODE:
            api_key    = os.environ.get("OKX_DEMO_API_KEY", "")
            api_secret = os.environ.get("OKX_DEMO_API_SECRET", "")
            passphrase = os.environ.get("OKX_DEMO_PASSPHRASE", "")
            mode_label = "PAPER (OKX Demo Trading)"
        else:
            api_key    = os.environ.get("OKX_API_KEY", "")
            api_secret = os.environ.get("OKX_API_SECRET", "")
            passphrase = os.environ.get("OKX_PASSPHRASE", "")
            mode_label = "LIVE"

        self._keys_configured = bool(api_key and api_secret and passphrase)

        self._exchange = ccxt.okx({
            "apiKey":   api_key,
            "secret":   api_secret,
            "password": passphrase,       # OKX calls this "passphrase" but CCXT key is "password"
            "options":  {"defaultType": "swap"},
        })

        if config.PAPER_MODE:
            self._exchange.set_sandbox_mode(True)

        if not self._keys_configured:
            if not config.PAPER_MODE:
                raise RuntimeError(
                    "Live mode requires valid OKX credentials (OKX_API_KEY, "
                    "OKX_API_SECRET, OKX_PASSPHRASE). Bot cannot start."
                )
            print(
                f"[order_manager] WARNING: OKX credentials not configured ({mode_label}). "
                "Order placement and balance queries will use fallback values.",
                file=sys.stderr,
            )
            return

        print(f"[order_manager] OKX exchange initialised in {mode_label} mode")
        self._verify_api_permissions()

    def _verify_api_permissions(self) -> None:
        """
        Verify that the OKX API key does NOT have withdrawal permissions.

        OKX does not have a direct equivalent to Binance's sapiGetAccountApiRestrictions().
        Instead:
          1. Call privateGetAccountConfig() to get account configuration.
          2. Check the "perm" field in the response.
             OKX API keys have a perm string that may contain "withdraw" if
             withdrawal is enabled. A trade-only key will NOT contain "withdraw".
          3. If "withdraw" is present in perm → raise RuntimeError.

        Paper mode (Demo Trading): verifies key is functional via fetch_balance().

        Logs result to event_log regardless of outcome.
        """
        mode = "paper (OKX Demo)" if config.PAPER_MODE else "live"
        try:
            if config.PAPER_MODE:
                # Demo: verify credentials are functional
                self._exchange.fetch_balance()
                detail = f"OKX Demo Trading API key verified functional. Mode={mode}"
                withdrawal_enabled = False
                print(f"[order_manager] {detail}")
            else:
                # Live: check key permissions via OKX account config endpoint
                account_config = self._exchange.privateGetAccountConfig()
                # OKX response: {"code": "0", "data": [{"perm": "read_only,trade", ...}]}
                data_list = account_config.get("data", [{}])
                perm_str = str(data_list[0].get("perm", "") if data_list else "")
                withdrawal_enabled = "withdraw" in perm_str.lower()
                detail = (
                    f"OKX API key config: perm='{perm_str}', "
                    f"withdrawal_in_perm={withdrawal_enabled}"
                )
                print(f"[order_manager] {detail}")

                if withdrawal_enabled:
                    db_logger.log_event(
                        event_type="api_permission_check",
                        severity=10,
                        description=(
                            "FATAL: Bot OKX API key has withdrawal permissions. "
                            "Create a trade-only key before running the bot."
                        ),
                        source="order_manager",
                    )
                    raise RuntimeError(
                        "FATAL: Bot OKX API key has withdrawal permissions. "
                        "Create a trade-only key before running the bot."
                    )

            db_logger.log_event(
                event_type="api_permission_check",
                severity=1,
                description=detail,
                source="order_manager",
            )

        except RuntimeError:
            raise
        except Exception as exc:
            msg = f"OKX API permission check failed: {exc}"
            print(f"[order_manager] WARNING: {msg}", file=sys.stderr)
            db_logger.log_event(
                event_type="api_permission_check",
                severity=5,
                description=msg,
                source="order_manager",
            )

    # ── Portfolio value ───────────────────────────────────────────────────────

    def get_portfolio_value(self) -> float:
        """
        Fetch free USDT balance from OKX exchange via CCXT.

        Paper mode / no keys: returns config.STARTING_CAPITAL_USD.
        Live mode success: caches result + timestamp; returns value.
        Live mode failure:
          - Cache < 60s old → return cached value with warning.
          - Cache stale / absent → halt trading + raise RuntimeError.
            Caller (_execute_enter) must catch RuntimeError and abort the trade.
        """
        if not self._keys_configured:
            return config.STARTING_CAPITAL_USD
        try:
            balance = self._exchange.fetch_balance()
            usdt_free = balance.get("USDT", {}).get("free", None)
            if usdt_free is not None:
                val = float(usdt_free)
                self._last_known_balance = val
                self._last_balance_ts    = _time.time()
                return val
            print("[order_manager] USDT balance not found in OKX response", file=sys.stderr)
            # Treat missing field same as exception — fall through to cache logic
            raise RuntimeError("USDT balance field missing from OKX response")
        except RuntimeError:
            raise
        except Exception as exc:
            print(f"[order_manager] get_portfolio_value failed: {exc}", file=sys.stderr)

        # Live-mode cache fallback
        if not config.PAPER_MODE:
            cache_age = (
                _time.time() - self._last_balance_ts
                if self._last_balance_ts is not None else float("inf")
            )
            if self._last_known_balance is not None and cache_age < 60:
                print(
                    f"[order_manager] Using cached balance {self._last_known_balance:.2f} "
                    f"(age={cache_age:.0f}s)",
                    file=sys.stderr,
                )
                return self._last_known_balance
            # Cache absent or stale — cannot safely size a trade
            self._trading_halted = True
            self._notify_threadsafe(
                "🚨 <b>Balance query failed and cache stale</b>\n"
                "Cannot size new trades without current balance.\n"
                "Trading halted. /resume after OKX connectivity is restored."
            )
            raise RuntimeError(
                "get_portfolio_value: live balance unavailable and cache stale — trading halted"
            )

        # Paper mode: fall back to starting capital
        return config.STARTING_CAPITAL_USD

    def get_live_positions(self) -> list[dict]:
        """
        Fetch all open positions from OKX for the configured trading pairs.

        Returns a list of dicts with keys: pair, direction, contracts, entry_price.
        Returns [] only in paper mode or when keys are not configured.
        RAISES on any exchange error — callers must handle failure explicitly.
        An empty list returned here means OKX confirmed there are no open positions.
        An exception means exchange state is unknown.
        """
        if config.PAPER_MODE or not self._keys_configured:
            return []
        ccxt_symbols = [_to_ccxt_symbol(p) for p in config.TRADING_PAIRS]
        raw = self._exchange.fetch_positions(ccxt_symbols)  # propagates on error
        result = []
        for pos in raw:
            contracts = abs(float(pos.get("contracts") or pos.get("size") or 0))
            if contracts <= _FLAT_POSITION_THRESHOLD_QTY:
                continue
            ccxt_sym = pos.get("symbol", "")
            # Map CCXT symbol back to OKX instrument ID
            okx_pair = next(
                (p for p in config.TRADING_PAIRS if _to_ccxt_symbol(p) == ccxt_sym),
                ccxt_sym,
            )
            direction = "LONG" if pos.get("side", "long") == "long" else "SHORT"
            result.append({
                "pair":        okx_pair,
                "direction":   direction,
                "contracts":   contracts,
                "entry_price": float(pos.get("entryPrice") or 0),
            })
        return result

    def get_live_open_orders(self) -> "list[dict] | None":
        """
        Fetch all open (resting) orders for the configured trading pairs.

        Returns a list of CCXT order dicts in live mode.
        Returns [] in paper mode or when keys are not configured.
        Returns None on any exchange exception — caller must handle this
        as "state unknown" (not as "no orders").
        """
        if config.PAPER_MODE or not self._keys_configured:
            return []
        try:
            orders = []
            for pair in config.TRADING_PAIRS:
                orders.extend(self._exchange.fetch_open_orders(_to_ccxt_symbol(pair)))
            return orders
        except Exception as exc:
            print(f"[order_manager] get_live_open_orders failed: {exc}", file=sys.stderr)
            return None

    async def verify_and_set_leverage(self) -> bool:
        """
        Set isolated margin mode and leverage on all configured trading pairs.

        Returns True only when all pairs are verified at config.OKX_LEVERAGE.
        Returns False if any pair's leverage cannot be confirmed — caller should
        raise RuntimeError to abort live startup.

        set_margin_mode failures are logged as warnings only (OKX returns an
        error when mode is already set, which is not a real failure).
        set_leverage failures and verification mismatches are hard failures.
        """
        loop = asyncio.get_running_loop()

        def _sync_setup() -> bool:
            all_ok = True
            for pair in config.TRADING_PAIRS:
                symbol   = _to_ccxt_symbol(pair)
                pair_ok  = True

                # Set margin mode — warning-only (OKX errors if already set)
                try:
                    self._exchange.set_margin_mode(
                        config.OKX_MARGIN_MODE, symbol,
                        params={"tdMode": config.OKX_MARGIN_MODE},
                    )
                    print(
                        f"[order_manager] Margin mode set: {symbol} = {config.OKX_MARGIN_MODE}",
                        flush=True,
                    )
                except Exception as exc:
                    print(
                        f"[order_manager] WARNING: set_margin_mode {symbol}: {exc}",
                        file=sys.stderr,
                    )

                # Set leverage for both sides (OKX isolated requires per-side)
                for pos_side in ("long", "short"):
                    try:
                        self._exchange.set_leverage(
                            config.OKX_LEVERAGE, symbol,
                            params={"posSide": pos_side},
                        )
                        print(
                            f"[order_manager] Leverage set: {symbol} {pos_side} = {config.OKX_LEVERAGE}x",
                            flush=True,
                        )
                    except Exception as exc:
                        print(
                            f"[order_manager] FAIL: set_leverage {pos_side} {symbol}: {exc}",
                            file=sys.stderr,
                        )
                        pair_ok = False

                # Verify — confirmed leverage must equal config value
                try:
                    info      = self._exchange.fetch_leverage(symbol, params={})
                    long_lev  = info.get("longLeverage")  or info.get("leverage")
                    short_lev = info.get("shortLeverage") or info.get("leverage")
                    if long_lev is None or short_lev is None:
                        print(
                            f"[order_manager] FAIL: fetch_leverage returned no leverage for {symbol}",
                            file=sys.stderr,
                        )
                        pair_ok = False
                    else:
                        print(
                            f"[order_manager] Leverage verified: {symbol} "
                            f"long={long_lev}x short={short_lev}x",
                            flush=True,
                        )
                        if int(long_lev) != config.OKX_LEVERAGE:
                            print(
                                f"[order_manager] FAIL: Long leverage mismatch on {symbol}: "
                                f"got {long_lev}, want {config.OKX_LEVERAGE}",
                                file=sys.stderr,
                            )
                            pair_ok = False
                        if int(short_lev) != config.OKX_LEVERAGE:
                            print(
                                f"[order_manager] FAIL: Short leverage mismatch on {symbol}: "
                                f"got {short_lev}, want {config.OKX_LEVERAGE}",
                                file=sys.stderr,
                            )
                            pair_ok = False
                except Exception as exc:
                    print(
                        f"[order_manager] FAIL: Could not verify leverage for {symbol}: {exc}",
                        file=sys.stderr,
                    )
                    pair_ok = False

                if not pair_ok:
                    all_ok = False

            if not all_ok:
                return False
            return True

        return await loop.run_in_executor(None, _sync_setup)

    async def check_reconciliation_log(self) -> None:
        """
        Check for unresolved reconciliation records from previous sessions.

        Unresolved = status in ('fill_confirmed_pending_log', 'emergency_close_required').
        If any found: halt trading + send CRITICAL Telegram.
        Operator must manually review reconciliation_log table, then /resume.
        """
        from sqlalchemy import create_engine as _ce, text as _text
        import os as _os
        database_url = _os.environ.get("DATABASE_URL")
        if not database_url:
            return
        try:
            engine = _ce(database_url)
            with engine.connect() as conn:
                rows = conn.execute(
                    _text(
                        """
                        SELECT id, pair, direction, filled_at, status
                        FROM reconciliation_log
                        WHERE status IN (
                            'fill_confirmed_pending_log',
                            'emergency_close_required',
                            'price_recon_pending'
                        )
                        ORDER BY created_at DESC
                        LIMIT 20
                        """
                    )
                ).fetchall()
        except Exception as exc:
            print(f"[order_manager] check_reconciliation_log query failed: {exc}", file=sys.stderr)
            return

        if not rows:
            print("[order_manager] Reconciliation log: no unresolved records.", flush=True)
            return

        self._trading_halted = True
        summary = "\n".join(
            f"  id={r[0]} {r[1]} {r[2]} filled_at={r[3]} status={r[4]}"
            for r in rows
        )
        msg = (
            f"🚨 <b>Unresolved reconciliation records found</b>\n"
            f"{len(rows)} record(s) require manual review:\n"
            f"<pre>{summary}</pre>\n"
            f"Check reconciliation_log table in DB.\n"
            f"Send /resume after confirming all records are resolved."
        )
        print(f"[order_manager] CRITICAL: {msg}", file=sys.stderr)
        self._notify_threadsafe(msg)

    # ── Order placement ───────────────────────────────────────────────────────

    def resume_trading(self) -> None:
        """
        Clear the trading halt flag.
        Called by /resume Telegram command after manual OKX verification.
        """
        self._trading_halted = False
        db_logger.log_event(
            event_type="trading_resumed",
            severity=3,
            description="Trading manually resumed via /resume command",
            source="order_manager",
        )
        print("[order_manager] Trading resumed.", flush=True)

    def _notify_threadsafe(self, message: str) -> None:
        """
        Send a Telegram notification from a synchronous context (thread-pool executor).
        Schedules the send coroutine onto the event loop via run_coroutine_threadsafe.
        No-op if notifier or its loop is unavailable.
        """
        if self._notifier is not None and getattr(self._notifier, "_loop", None) is not None:
            asyncio.run_coroutine_threadsafe(
                self._notifier.send(message),
                self._notifier._loop,
            )

    def place_order(self, cycle_result: dict) -> dict:
        """
        Place an order based on a TRADE cycle_result dict.

        Returns dict with keys:
          order_id (str|None), trade_id (int), filled_price (float),
          filled_size_usd (float), paper (bool), timestamp (datetime),
          status ('filled'|'failed'), error (str|None)
        """
        if self._trading_halted:
            return {
                "order_id":        None,
                "trade_id":        -1,
                "filled_price":    0.0,
                "filled_size_usd": 0.0,
                "paper":           config.PAPER_MODE,
                "timestamp":       datetime.now(timezone.utc),
                "status":          "failed",
                "error":           "trading_halted_pending_reconciliation",
            }
        if config.PAPER_MODE:
            return self._place_paper_order(cycle_result)
        else:
            return self._place_live_order(cycle_result)

    def _place_paper_order(self, cycle_result: dict) -> dict:
        """
        Simulate a fill at mid-price + conservative slippage.
        Registers the trade in the DB and returns fill details.
        """
        signal = cycle_result["signal"]
        entry_price = cycle_result["entry_price"]
        pair = cycle_result["pair"]
        now = datetime.now(timezone.utc)

        # Paper slippage: 0.05% adverse
        if signal == "LONG":
            filled_price = round(entry_price * (1 + _PAPER_SLIPPAGE_PCT), 2)
        else:
            filled_price = round(entry_price * (1 - _PAPER_SLIPPAGE_PCT), 2)

        order_id   = f"PAPER-{uuid.uuid4().hex[:8].upper()}"
        filled_qty = cycle_result["order_size_usd"] / filled_price

        trade_data = {
            "timestamp_open":  now,
            "pair":            pair,
            "direction":       signal,
            "entry_price":     filled_price,
            "size_usd":        cycle_result["order_size_usd"],
            "stop_loss":       cycle_result["stop_loss"],
            "take_profit":     cycle_result["take_profit"],
            "regime_at_entry": cycle_result["regime"],
            "paper":           True,
            "filled_qty":      filled_qty,
            "client_order_id": None,   # paper trades have no real exchange order ID
        }
        trade_id = db_logger.log_trade_open(trade_data)

        if trade_id <= 0:
            msg = f"DB logging failed for paper order {pair} {signal}"
            print(f"[order_manager] ERROR: {msg}", file=sys.stderr)
            db_logger.log_event(
                event_type="db_failure_paper_order",
                severity=7,
                description=msg,
                source="order_manager",
            )
            return {
                "order_id":        order_id,
                "trade_id":        -1,
                "filled_price":    0.0,
                "filled_size_usd": 0.0,
                "paper":           True,
                "timestamp":       now,
                "status":          "failed",
                "error":           "db_logging_failed",
            }

        print(
            f"[order_manager] PAPER FILL: {pair} {signal} "
            f"@ {filled_price:.2f} (mid={entry_price:.2f}, "
            f"slippage={_PAPER_SLIPPAGE_PCT*100:.2f}%) "
            f"size=USD {cycle_result['order_size_usd']:.2f} "
            f"qty={filled_qty:.6f} "
            f"stop={cycle_result['stop_loss']:.2f} "
            f"tp={cycle_result['take_profit']:.2f} "
            f"trade_id={trade_id}",
            flush=True,
        )

        return {
            "order_id":        order_id,
            "trade_id":        trade_id,
            "filled_price":    filled_price,
            "filled_size_usd": cycle_result["order_size_usd"],
            "filled_qty":      filled_qty,
            "paper":           True,
            "timestamp":       now,
            "status":          "filled",
            "error":           None,
        }

    def _get_tick_size(self, ccxt_symbol: str) -> float:
        """
        Return the minimum price increment (tick size) for `ccxt_symbol`.

        Loaded once from CCXT market info and cached for the session.
        Falls back to 0.1 if unavailable — conservative, never zero.

        OKX market info supplies 'tickSz' (string) in the raw instrument data.
        Example: BTC/USDT:USDT → tickSz = "0.1"
        """
        if not self._markets_cache:
            try:
                self._markets_cache = self._exchange.load_markets()
                print(
                    f"[order_manager] Loaded {len(self._markets_cache)} markets "
                    "from OKX for tick-size lookup.",
                    flush=True,
                )
            except Exception as exc:
                print(
                    f"[order_manager] WARNING: load_markets failed: {exc} — "
                    "using tick_size fallback 0.1",
                    file=sys.stderr,
                )
                return 0.1

        market = self._markets_cache.get(ccxt_symbol, {})

        # Prefer OKX raw field 'tickSz' (always present on OKX instruments)
        tick_sz = market.get("info", {}).get("tickSz")
        if tick_sz is not None:
            return float(tick_sz)

        # Fallback: CCXT normalised price precision
        price_precision = market.get("precision", {}).get("price")
        if price_precision is not None:
            return float(price_precision)

        print(
            f"[order_manager] WARNING: tick size not found for {ccxt_symbol}, "
            "using fallback 0.1",
            file=sys.stderr,
        )
        return 0.1

    def _get_lot_step(self, ccxt_symbol: str) -> float:
        """
        Return the minimum order quantity step (lot size) for ccxt_symbol.
        Markets cache must already be populated (call _get_tick_size first).
        OKX raw field is 'lotSz'; falls back to CCXT precision.amount.
        """
        market = self._markets_cache.get(ccxt_symbol, {})
        lot_sz = market.get("info", {}).get("lotSz")
        if lot_sz is not None:
            return float(lot_sz)
        amount_prec = market.get("precision", {}).get("amount")
        if amount_prec is not None:
            return float(amount_prec)
        return 0.001  # conservative fallback (BTC minimum)

    def _place_live_order(self, cycle_result: dict) -> dict:
        """
        Place a real limit order on OKX perpetual swaps via CCXT.

        Limit price is set 1 tick inside the best bid/ask so the order posts
        as a maker (or crosses immediately if the market moves into it):
          LONG  → limit_price = best_ask - (tick_size × LIMIT_ORDER_SLIP_TICKS)
          SHORT → limit_price = best_bid + (tick_size × LIMIT_ORDER_SLIP_TICKS)

        Polls for fill for up to config.LIMIT_ORDER_TIMEOUT_S seconds; cancels
        if unfilled.  Logs whether each fill was maker or taker based on fee rate
        returned by OKX (maker ≤ 0.02%, taker = 0.05%).

        Places SL/TP orders after confirmed fill.

        CCXT symbol: config pair 'BTC-USDT-SWAP' → CCXT 'BTC/USDT:USDT'
        """
        import time

        signal         = cycle_result["signal"]
        pair           = cycle_result["pair"]
        ccxt_symbol    = _to_ccxt_symbol(pair)
        entry_price    = cycle_result["entry_price"]   # mid-price from signal engine
        order_size_usd = cycle_result["order_size_usd"]
        now            = datetime.now(timezone.utc)
        # Local copies of stop/TP so we can adjust if fill price moves
        stop_loss   = cycle_result["stop_loss"]
        take_profit = cycle_result["take_profit"]

        side = "buy" if signal == "LONG" else "sell"
        # amount computed below using fresh price (Step 1)

        try:
            # ── Step 1: Fetch live bid/ask + fresh price from OKX ────────────
            ticker    = self._exchange.fetch_ticker(ccxt_symbol)
            best_bid  = float(ticker.get("bid") or entry_price)
            best_ask  = float(ticker.get("ask") or entry_price)
            tick_size = self._get_tick_size(ccxt_symbol)

            # Use last-trade price for accurate notional sizing (bid/ask can
            # straddle a round number and over/understate position size).
            fresh_price = float(ticker.get("last") or ticker.get("close") or 0)
            if fresh_price <= 0:
                return {
                    "order_id":        None,
                    "trade_id":        -1,
                    "filled_price":    0.0,
                    "filled_size_usd": 0.0,
                    "paper":           False,
                    "timestamp":       now,
                    "status":          "failed",
                    "error":           "cannot_fetch_fresh_price",
                }

            # Apply lot-step precision so qty is a valid OKX order size
            lot_step = self._get_lot_step(ccxt_symbol)
            amount   = round(round(order_size_usd / fresh_price / lot_step) * lot_step, 8)
            if amount <= 0:
                return {
                    "order_id":        None,
                    "trade_id":        -1,
                    "filled_price":    0.0,
                    "filled_size_usd": 0.0,
                    "paper":           False,
                    "timestamp":       now,
                    "status":          "failed",
                    "error":           "computed_qty_zero",
                }

            # ── Step 2: Set limit price 1 tick inside the spread ─────────────
            slip = tick_size * config.LIMIT_ORDER_SLIP_TICKS
            if signal == "LONG":
                # Post 1 tick below best ask → likely maker; fills immediately
                # if ask moves down by 1 tick, or becomes taker if market is thin
                limit_price = round(best_ask - slip, 8)
            else:
                # Post 1 tick above best bid → likely maker
                limit_price = round(best_bid + slip, 8)

            print(
                f"[order_manager] LIVE LIMIT ORDER: {ccxt_symbol} {side} "
                f"{amount:.6f} @ {limit_price:.4f} "
                f"(fresh={fresh_price:.4f} bid={best_bid:.4f} ask={best_ask:.4f} "
                f"step={lot_step})",
                flush=True,
            )

            # ── Step 3: Place order ───────────────────────────────────────────
            # clientOrderId lets us reconcile even if create_limit_order() raises.
            client_order_id = f"tb_{pair.replace('-', '')[:10]}_{int(_time.time())}"
            order_id        = None
            filled_price    = None
            filled_qty      = 0.0
            fee_type        = "unknown"
            recon_id        = -1   # set after fill confirmed (FIX 2)

            try:
                order    = self._exchange.create_limit_order(
                    ccxt_symbol, side, amount, limit_price,
                    params={
                        "clOrdId": client_order_id,
                        "tdMode": config.OKX_MARGIN_MODE,
                    },
                )
                order_id = order["id"]
            except Exception as create_exc:
                # OKX may have accepted the order even though create raised.
                # Reconcile by clientOrderId before giving up.
                print(
                    f"[order_manager] WARNING: create_limit_order raised: {create_exc} "
                    "— reconciling by clientOrderId",
                    file=sys.stderr,
                )
                reconcile = self._reconcile_order(None, ccxt_symbol, client_order_id)
                if reconcile is None:
                    # Proven not filled
                    return {
                        "order_id":        None,
                        "trade_id":        -1,
                        "filled_price":    0.0,
                        "filled_size_usd": 0.0,
                        "paper":           False,
                        "timestamp":       now,
                        "status":          "failed",
                        "error":           str(create_exc),
                    }
                elif reconcile.get("status") == "ambiguous":
                    return {
                        "order_id":        None,
                        "trade_id":        -1,
                        "filled_price":    0.0,
                        "filled_size_usd": 0.0,
                        "paper":           False,
                        "timestamp":       now,
                        "status":          "failed",
                        "error":           "order_state_ambiguous",
                    }
                else:
                    # Order went through — treat as fill and continue
                    order_id     = reconcile.get("order_id", "exception_recovered")
                    filled_price = reconcile["filled_price"]
                    filled_qty   = reconcile["filled_qty"]

            # ── Step 4: Poll for fill (skipped if exception recovery set filled_price) ─
            if filled_price is None and order_id is not None:
                deadline = time.time() + config.LIMIT_ORDER_TIMEOUT_S
                while time.time() < deadline:
                    time.sleep(_ORDER_POLL_INTERVAL_S)
                    try:
                        fetched = self._exchange.fetch_order(order_id, ccxt_symbol)
                        if fetched["status"] == "closed":
                            filled_price = float(
                                fetched.get("average") or limit_price
                            )
                            filled_qty = float(fetched.get("filled") or 0)
                            if filled_qty <= 0:
                                detail = self._exchange.fetch_order(order_id, ccxt_symbol)
                                filled_qty = float(detail.get("filled") or amount)
                            # Detect maker vs taker from returned fee rate
                            fee_rate = None
                            fee_info = fetched.get("fee") or {}
                            if isinstance(fee_info, dict):
                                fee_rate = fee_info.get("rate")
                            if fee_rate is None:
                                fees_list = fetched.get("fees") or []
                                if fees_list and isinstance(fees_list[0], dict):
                                    fee_rate = fees_list[0].get("rate")
                            if fee_rate is not None:
                                fee_type = (
                                    "maker"
                                    if abs(float(fee_rate)) <= 0.00025
                                    else "taker"
                                )
                            break
                        elif fetched["status"] in ("canceled", "rejected"):
                            break
                    except Exception:
                        pass

                # ── Step 5: Reconcile if still unfilled after poll ────────────
                if filled_price is None:
                    reconcile = self._reconcile_order(order_id, ccxt_symbol, client_order_id)
                    if reconcile is None:
                        msg = (
                            f"Live limit order {order_id} not filled within "
                            f"{config.LIMIT_ORDER_TIMEOUT_S}s — cancelled"
                        )
                        db_logger.log_event(
                            event_type="order_timeout",
                            severity=5,
                            description=msg,
                            source="order_manager",
                        )
                        print(f"[order_manager] {msg}", file=sys.stderr)
                        return {
                            "order_id":        order_id,
                            "trade_id":        -1,
                            "filled_price":    0.0,
                            "filled_size_usd": 0.0,
                            "paper":           False,
                            "timestamp":       now,
                            "status":          "failed",
                            "error":           msg,
                        }
                    elif reconcile.get("status") == "ambiguous":
                        return {
                            "order_id":        order_id,
                            "trade_id":        -1,
                            "filled_price":    0.0,
                            "filled_size_usd": 0.0,
                            "paper":           False,
                            "timestamp":       now,
                            "status":          "failed",
                            "error":           "order_state_ambiguous",
                        }
                    else:
                        filled_price = reconcile["filled_price"]
                        filled_qty   = reconcile["filled_qty"]

            # ── Step 5.5: Durable reconciliation record + stop/TP revalidation ──
            # Write audit record immediately — before anything else can fail.
            actual_size_usd = (filled_qty or amount) * (filled_price or fresh_price)
            recon_id = db_logger.log_reconciliation_record({
                "pair":             pair,
                "direction":        signal,
                "entry_price":      filled_price or fresh_price,
                "filled_qty":       filled_qty or amount,
                "size_usd":         actual_size_usd,
                "status":           "fill_confirmed_pending_log",
                "client_order_id":  client_order_id,
                "filled_at":        datetime.now(timezone.utc),
            })

            # Revalidate stop/TP against actual fill price.
            # Claude's levels are based on briefing-time price; if price moved
            # between briefing and fill, the stop/TP may be on the wrong side.
            fp = filled_price or fresh_price
            if fp > 0:
                if signal == "LONG":
                    if stop_loss >= fp:
                        adj = round(fp * 0.98, 8)
                        print(
                            f"[order_manager] WARNING: Stop adjusted — fill moved: "
                            f"{stop_loss:.4f} → {adj:.4f}",
                            file=sys.stderr,
                        )
                        stop_loss = adj
                    if take_profit <= fp:
                        adj = round(fp * 1.04, 8)
                        print(
                            f"[order_manager] WARNING: TP adjusted — fill moved: "
                            f"{take_profit:.4f} → {adj:.4f}",
                            file=sys.stderr,
                        )
                        take_profit = adj
                else:  # SHORT
                    if stop_loss <= fp:
                        adj = round(fp * 1.02, 8)
                        print(
                            f"[order_manager] WARNING: Stop adjusted — fill moved: "
                            f"{stop_loss:.4f} → {adj:.4f}",
                            file=sys.stderr,
                        )
                        stop_loss = adj
                    if take_profit >= fp:
                        adj = round(fp * 0.96, 8)
                        print(
                            f"[order_manager] WARNING: TP adjusted — fill moved: "
                            f"{take_profit:.4f} → {adj:.4f}",
                            file=sys.stderr,
                        )
                        take_profit = adj

            # ── Step 6: Place SL / TP orders ─────────────────────────────────
            # Must happen BEFORE DB log — if SL/TP fails we emergency-close
            # the fill so no position exists on the exchange without a stop.
            # Use exchange-confirmed filled_qty, never the intended amount.
            close_side = "sell" if signal == "LONG" else "buy"
            close_qty  = filled_qty if filled_qty > 0 else amount
            try:
                self._exchange.create_order(
                    ccxt_symbol, "stop_market", close_side, close_qty, None,
                    {
                        "stopPrice": stop_loss,
                        "reduceOnly": True,
                        "tdMode": config.OKX_MARGIN_MODE,
                    },
                )
                self._exchange.create_order(
                    ccxt_symbol, "take_profit_market", close_side, close_qty, None,
                    {
                        "stopPrice": take_profit,
                        "reduceOnly": True,
                        "tdMode": config.OKX_MARGIN_MODE,
                    },
                )
            except Exception as exc:
                msg = (
                    f"SL/TP placement failed after fill — emergency close: "
                    f"{ccxt_symbol} {signal}: {exc}"
                )
                print(f"[order_manager] CRITICAL: {msg}", file=sys.stderr)
                db_logger.log_event(
                    event_type="emergency_close",
                    severity=9,
                    description=msg,
                    source="order_manager",
                )
                db_logger.update_reconciliation_record(
                    recon_id, {"status": "emergency_close_required", "notes": msg}
                )
                self._emergency_close(pair, signal, close_qty, f"SL/TP placement failed: {exc}")
                return {
                    "order_id":        order_id,
                    "trade_id":        -1,
                    "filled_price":    0.0,
                    "filled_size_usd": 0.0,
                    "paper":           False,
                    "timestamp":       now,
                    "status":          "failed",
                    "error":           msg,
                }

            # ── Step 7: Log trade open ────────────────────────────────────────
            # Logged AFTER SL/TP confirmed — trade only exists in DB with a stop.
            trade_data = {
                "timestamp_open":  now,
                "pair":            pair,
                "direction":       signal,
                "entry_price":     filled_price,
                "size_usd":        actual_size_usd,   # actual notional, not intended
                "stop_loss":       stop_loss,          # potentially adjusted
                "take_profit":     take_profit,        # potentially adjusted
                "regime_at_entry": cycle_result["regime"],
                "paper":           False,
                "filled_qty":      filled_qty,
                "client_order_id": client_order_id,   # persisted for ghost reconciliation
            }
            trade_id = db_logger.log_trade_open(trade_data)

            if trade_id <= 0:
                # DB failure after live fill with SL/TP on exchange.
                # Halt FIRST so no further trades can open while DB is broken,
                # then emergency-close the unlogged position.
                msg = (
                    f"DB logging failed for live fill {ccxt_symbol} {signal} "
                    f"@ {filled_price:.4f} — halting trading and emergency closing"
                )
                print(f"[order_manager] CRITICAL: {msg}", file=sys.stderr)
                self._trading_halted = True
                db_logger.log_event(
                    event_type="db_failure_live_order",
                    severity=9,
                    description=msg,
                    source="order_manager",
                )
                db_logger.update_reconciliation_record(
                    recon_id, {"status": "emergency_close_required", "notes": msg}
                )
                self._emergency_close(pair, signal, filled_qty or amount, "DB logging failed after fill")
                self._notify_threadsafe(
                    f"🚨 <b>TRADING HALTED — DB failure after live fill</b>\n"
                    f"Emergency close attempted.\n"
                    f"Verify DB health and OKX position before /resume"
                )
                return {
                    "order_id":        order_id,
                    "trade_id":        -1,
                    "filled_price":    0.0,
                    "filled_size_usd": 0.0,
                    "paper":           False,
                    "timestamp":       now,
                    "status":          "failed",
                    "error":           "db_logging_failed",
                }

            # Mark reconciliation record as fully logged
            db_logger.update_reconciliation_record(recon_id, {"status": "logged", "trade_id": trade_id})

            print(
                f"[order_manager] LIVE FILLED ({fee_type}): {ccxt_symbol} {signal} "
                f"@ {filled_price:.4f} qty={filled_qty:.6f} "
                f"size=USD {actual_size_usd:.2f} (intended {order_size_usd:.2f}) "
                f"trade_id={trade_id}",
                flush=True,
            )

            return {
                "order_id":        order_id,
                "trade_id":        trade_id,
                "filled_price":    filled_price,
                "filled_size_usd": actual_size_usd,
                "filled_qty":      filled_qty,
                "stop_loss":       stop_loss,       # may differ from cycle_result if adjusted
                "take_profit":     take_profit,     # may differ from cycle_result if adjusted
                "client_order_id": client_order_id, # for in-memory position dict consistency
                "paper":           False,
                "timestamp":       now,
                "status":          "filled",
                "error":           None,
            }

        except Exception as exc:
            msg = f"Live OKX order placement failed: {exc}"
            print(f"[order_manager] ERROR: {msg}", file=sys.stderr)
            db_logger.log_event(
                event_type="order_error",
                severity=7,
                description=msg,
                source="order_manager",
            )
            return {
                "order_id":        None,
                "trade_id":        -1,
                "filled_price":    0.0,
                "filled_size_usd": 0.0,
                "paper":           False,
                "timestamp":       now,
                "status":          "failed",
                "error":           msg,
            }

    # ── Position close ────────────────────────────────────────────────────────

    async def close_position(
        self,
        pair: str,
        direction: str,
        size_usd: float,
        entry_price: float,
        filled_qty: Optional[float] = None,
        trade_id: Optional[int] = None,
    ) -> dict:
        """
        Close an open position at market price.

        Paper mode: no exchange call needed — P&L calculated from prices.
        Live mode:  cancels pending SL/TP orders, places market close, then
                    polls up to 6 × 5 s to confirm the position is flat.
                    If still open after 30 s, halts trading and sends CRITICAL
                    alert — never silently logs a close that didn't happen.

        filled_qty: actual base-qty from the entry fill (preferred).
                    Falls back to size_usd / entry_price if not provided.

        Returns {"status": "filled"|"failed"|"position_not_flat", "error": str|None}.
        """
        import time as _t
        if config.PAPER_MODE or not self._keys_configured:
            # Paper: no exchange call — estimate exit from mark price
            est_price = entry_price or 0.0
            if self._ws_manager is not None:
                try:
                    ticker = self._ws_manager.get_latest_ticker(pair)
                    est_price = float(ticker.get("mark_price") or entry_price or 0)
                except Exception as exc:
                    print(f"[order_manager] paper close: mark price fetch failed: {exc}", file=sys.stderr)
            amount = filled_qty if (filled_qty and filled_qty > 0) else (
                size_usd / entry_price if entry_price else 0
            )
            return {"status": "filled", "exit_price": est_price, "filled_qty": amount, "error": None}

        ccxt_symbol  = _to_ccxt_symbol(pair)
        close_side   = "sell" if direction == "LONG" else "buy"
        # Prefer the exact fill qty from entry to avoid residuals
        amount       = filled_qty if (filled_qty and filled_qty > 0) else (size_usd / entry_price)
        loop         = asyncio.get_running_loop()
        client_id    = f"close_{pair.replace('-', '')[:10]}_{int(_t.time())}"
        order_id     = None

        # Persist close_client_order_id BEFORE submitting the order so ghost
        # recovery after a crash can match this exact close, not the entry fill.
        # On DB failure: halt trading and alert — ghost recovery will be heuristic-only.
        # Still proceed with the close so the position is not left open.
        if trade_id is not None and trade_id > 0:
            id_written = db_logger.set_close_client_order_id(trade_id, client_id)
            if not id_written:
                self._trading_halted = True
                alert = (
                    f"🚨 CRITICAL: DB write failed for close_client_order_id "
                    f"(trade_id={trade_id}, pair={pair}). "
                    f"Ghost recovery for this close will use heuristic matching only. "
                    f"Close order is still being submitted. "
                    f"Use /resume after verifying OKX state."
                )
                print(f"[order_manager] {alert}", file=sys.stderr, flush=True)
                if self._notifier is not None:
                    try:
                        await self._notifier.send(alert)
                    except Exception:
                        pass

        _submission_fill_price = 0.0  # fill price from create_order response if available
        try:
            await loop.run_in_executor(None, self.cancel_all_orders, pair)
            order = await loop.run_in_executor(
                None,
                lambda: self._exchange.create_order(
                    ccxt_symbol, "market", close_side, amount, None,
                    {
                        "reduceOnly": True,
                        "clOrdId":    client_id,
                        "tdMode":     config.OKX_MARGIN_MODE,
                    },
                ),
            )
            order_id = order.get("id")
            # OKX market orders often fill synchronously; capture price if present
            _submission_fill_price = float(order.get("average") or order.get("price") or 0)
            print(
                f"[order_manager] CLOSE ORDER PLACED: {ccxt_symbol} {direction} "
                f"qty={amount:.6f} order_id={order_id}",
                flush=True,
            )
        except Exception as exc:
            print(
                f"[order_manager] WARNING: close_position submission error: {exc} "
                "— checking position state",
                file=sys.stderr,
            )
            # Check if OKX accepted the order anyway before declaring failure
            try:
                positions = await loop.run_in_executor(
                    None, lambda: self._exchange.fetch_positions([ccxt_symbol])
                )
                has_position = any(
                    abs(float(p.get("contracts") or p.get("size") or 0)) > 0
                    for p in positions
                )
                if not has_position:
                    # Position is gone — close went through despite exception.
                    # Try to recover actual fill price from recent closed orders.
                    print(
                        f"[order_manager] Close confirmed flat (post-exception): {ccxt_symbol}",
                        flush=True,
                    )
                    actual_exit = None
                    try:
                        closed = self._exchange.fetch_closed_orders(
                            ccxt_symbol, limit=5,
                            params={"clOrdId": client_id},
                        )
                        for o in closed:
                            if (
                                o.get("clientOrderId") == client_id
                                or o.get("info", {}).get("clOrdId") == client_id
                            ):
                                price = float(o.get("average") or o.get("price") or 0)
                                if price > 0:
                                    actual_exit = price
                                    break
                    except Exception:
                        pass

                    if actual_exit is None or actual_exit <= 0:
                        # Fill price unrecoverable — use 0.0 as sentinel
                        print(
                            f"[order_manager] WARNING: Cannot recover exit price for "
                            f"{pair} close — flagging for manual reconciliation",
                            file=sys.stderr,
                        )
                        self._notify_threadsafe(
                            f"⚠️ Exit price unknown for {pair}\n"
                            f"Close confirmed flat but fill price not recoverable.\n"
                            f"P&L for this trade requires manual reconciliation."
                        )
                        actual_exit = 0.0

                    # Verify stale SL/TP orders are gone even in the exception path
                    orders_clear = self._verify_orders_cancelled(ccxt_symbol)
                    if not orders_clear:
                        self._trading_halted = True
                        self._notify_threadsafe(
                            f"🚨 <b>Orders not cleared after exception-path close</b>: {pair}\n"
                            f"Stale SL/TP orders may remain on OKX.\n"
                            f"Check manually and send /resume"
                        )

                    return {
                        "status":                       "filled",
                        "exit_price":                   actual_exit,
                        "filled_qty":                   amount,
                        "error":                        None,
                        "price_reconciliation_required": actual_exit == 0.0,
                    }
            except Exception:
                pass
            # State unknown — halt and alert
            self._trading_halted = True
            if self._notifier is not None:
                await self._notifier.send(
                    f"🚨 <b>Close state ambiguous</b>: {pair}\n"
                    f"Check OKX manually. Trading halted.\n/resume to continue"
                )
            return {"status": "failed", "error": "close_ambiguous"}

        # ── Confirm position is flat (6 × 5s polls = 30s window) ─────────────
        for attempt in range(6):
            await asyncio.sleep(5)
            try:
                positions = await loop.run_in_executor(
                    None,
                    lambda: self._exchange.fetch_positions([ccxt_symbol]),
                )
                has_position = any(
                    abs(float(p.get("contracts") or p.get("size") or 0)) > 0
                    for p in positions
                )
                if not has_position:
                    print(
                        f"[order_manager] FLAT confirmed: {ccxt_symbol} "
                        f"(attempt {attempt + 1})",
                        flush=True,
                    )
                    # Prefer price from create_order response (avoids extra API call);
                    # fall back to a separate fetch_order only if not immediately available.
                    if _submission_fill_price > 0:
                        actual_exit = _submission_fill_price
                    elif order_id:
                        actual_exit = await loop.run_in_executor(
                            None,
                            lambda: self._get_actual_fill_price(order_id, ccxt_symbol),
                        )
                    else:
                        actual_exit = 0.0

                    # Cancel protective orders and verify they are actually gone
                    try:
                        await loop.run_in_executor(
                            None, lambda: self._exchange.cancel_all_orders(ccxt_symbol)
                        )
                    except Exception as cancel_exc:
                        print(
                            f"[order_manager] WARNING: cancel_all_orders failed "
                            f"after close of {pair}: {cancel_exc}",
                            file=sys.stderr,
                        )
                    orders_clear = await loop.run_in_executor(
                        None, lambda: self._verify_orders_cancelled(ccxt_symbol)
                    )
                    if not orders_clear:
                        self._trading_halted = True
                        warn_msg = (
                            f"🚨 <b>Orders not cleared after close</b>: {pair}\n"
                            f"Stale SL/TP orders may remain on OKX.\n"
                            f"Check manually and send /resume"
                        )
                        print(f"[order_manager] CRITICAL: {warn_msg}", file=sys.stderr)
                        db_logger.log_event(
                            event_type="orders_not_cleared",
                            severity=9,
                            description=warn_msg,
                            source="order_manager",
                        )
                        if self._notifier is not None:
                            await self._notifier.send(warn_msg)
                        # Position IS closed — return filled with warning flag
                        return {
                            "status":     "filled",
                            "exit_price": actual_exit,
                            "filled_qty": amount,
                            "error":      None,
                            "warning":    "orders_not_cleared",
                        }

                    return {
                        "status":     "filled",
                        "exit_price": actual_exit,
                        "filled_qty": amount,
                        "error":      None,
                    }
            except Exception as exc:
                print(
                    f"[order_manager] Flat check error (attempt {attempt + 1}): {exc}",
                    file=sys.stderr,
                )

        # Position still open after 30s — halt and alert
        msg = (
            f"Position {ccxt_symbol} {direction} NOT FLAT after close order. "
            f"Verify OKX manually, then /resume to restart trading."
        )
        print(f"[order_manager] CRITICAL: {msg}", file=sys.stderr)
        self._trading_halted = True
        db_logger.log_event(
            event_type="position_not_flat",
            severity=10,
            description=msg,
            source="order_manager",
        )
        if self._notifier is not None:
            await self._notifier.send(
                f"🚨 <b>CRITICAL: Position not flat</b>\n"
                f"{msg}\nUse /resume after verifying OKX."
            )
        return {"status": "position_not_flat", "exit_price": 0.0, "filled_qty": amount, "error": msg}

    def _verify_orders_cancelled(self, ccxt_symbol: str) -> bool:
        """
        Verify that all open orders for ccxt_symbol are actually gone.

        Polls fetch_open_orders() up to 3 times with 5-second gaps.
        Retries cancel_all_orders() between attempts if orders remain.
        Returns True when confirmed empty, False if still present after all attempts.

        Sync — safe to call from thread-pool executor (_emergency_close) or via
        run_in_executor from async context (close_position).
        """
        import time as _t
        for attempt in range(3):
            _t.sleep(5)
            try:
                open_orders = self._exchange.fetch_open_orders(ccxt_symbol)
                if not open_orders:
                    return True
                # Orders still present — retry cancel before next poll
                try:
                    self._exchange.cancel_all_orders(ccxt_symbol)
                except Exception as cancel_exc:
                    print(
                        f"[order_manager] WARNING: Retry cancel failed "
                        f"(attempt {attempt + 1}): {cancel_exc}",
                        file=sys.stderr,
                    )
            except Exception as exc:
                print(
                    f"[order_manager] WARNING: Order verification attempt "
                    f"{attempt + 1} failed: {exc}",
                    file=sys.stderr,
                )
        return False

    def _get_actual_fill_price(self, order_id: str, ccxt_symbol: str) -> float:
        """
        Fetch the actual average fill price for a completed order.
        Returns 0.0 on any error so callers can decide whether to fall back.
        """
        try:
            order = self._exchange.fetch_order(order_id, ccxt_symbol)
            price = float(order.get("average") or order.get("price") or 0)
            return price
        except Exception as exc:
            print(
                f"[order_manager] _get_actual_fill_price({order_id}) error: {exc}",
                file=sys.stderr,
            )
            return 0.0

    def _emergency_close(
        self,
        pair: str,
        direction: str,
        qty: float,
        reason: str = "",
    ) -> None:
        """
        Emergency market close of a filled position when SL/TP placement or DB
        logging fails.  Prevents a live position from existing without a stop.

        Submits a reduce-only market close, then polls up to 30 seconds to
        confirm the position is flat.  If not flat, sets _trading_halted=True
        and sends a CRITICAL Telegram alert.

        Sync — called from thread-pool executor (_place_live_order).
        Telegram calls use run_coroutine_threadsafe to cross back to the event loop.
        """
        import time as _t
        ccxt_symbol = _to_ccxt_symbol(pair)
        close_side  = "sell" if direction == "LONG" else "buy"
        client_id   = f"emerg_{pair.replace('-', '')[:10]}_{int(_t.time())}"

        print(
            f"[order_manager] CRITICAL: EMERGENCY CLOSE {pair} {direction} "
            f"qty={qty} reason={reason}",
            file=sys.stderr,
        )
        db_logger.log_event(
            event_type="emergency_close",
            severity=10,
            description=f"EMERGENCY CLOSE: {pair} {direction} qty={qty} — {reason}",
            source="order_manager",
        )

        if config.PAPER_MODE or not self._keys_configured:
            # Paper mode: no real position, skip exchange call
            self._notify_threadsafe(
                f"⚠️ Emergency close executed (paper): {pair} {direction}\n"
                f"Reason: {reason}"
            )
            return

        # ── Step 1: Cancel all existing orders BEFORE closing ─────────────────
        # Removes stale SL/TP orders that could fire against a future position.
        try:
            self._exchange.cancel_all_orders(ccxt_symbol)
            print(
                f"[order_manager] Cancelled all orders for {pair} before emergency close",
                flush=True,
            )
        except Exception as exc:
            print(
                f"[order_manager] WARNING: Could not cancel orders before emergency close "
                f"for {pair}: {exc}",
                file=sys.stderr,
            )
            # Continue anyway — closing the position is more important

        # ── Step 2: Submit close order ────────────────────────────────────────
        try:
            self._exchange.create_order(
                ccxt_symbol, "market", close_side, qty, None,
                {
                    "reduceOnly": True,
                    "clOrdId":    client_id,
                    "tdMode":     config.OKX_MARGIN_MODE,
                },
            )
            print(
                f"[order_manager] Emergency close submitted: {ccxt_symbol} {close_side}",
                flush=True,
            )
        except Exception as exc:
            print(
                f"[order_manager] CRITICAL: Emergency close submission failed: {exc}",
                file=sys.stderr,
            )
            # Still attempt the flat check — OKX may have accepted it

        # ── Step 3: Confirm flat (6 × 5 s = 30 s) ────────────────────────────
        confirmed_flat = False
        for attempt in range(6):
            _t.sleep(5)
            try:
                positions  = self._exchange.fetch_positions([ccxt_symbol])
                remaining  = sum(
                    abs(float(p.get("contracts") or p.get("size") or 0))
                    for p in positions
                    if p.get("symbol") == ccxt_symbol or ccxt_symbol in str(p.get("info", ""))
                )
                if remaining < _FLAT_POSITION_THRESHOLD_QTY:
                    confirmed_flat = True
                    print(
                        f"[order_manager] Emergency close confirmed flat: {pair} "
                        f"(attempt {attempt + 1})",
                        flush=True,
                    )
                    break
            except Exception as flat_exc:
                print(
                    f"[order_manager] Emergency flat check error "
                    f"(attempt {attempt + 1}): {flat_exc}",
                    file=sys.stderr,
                )

        if not confirmed_flat:
            self._trading_halted = True
            msg = (
                f"🚨 <b>EMERGENCY CLOSE FAILED</b>\n"
                f"{pair} {direction} may still be open on OKX.\n"
                f"Check OKX manually. Trading halted.\n"
                f"Send /resume after confirming flat."
            )
            print(f"[order_manager] CRITICAL: {msg}", file=sys.stderr)
            db_logger.log_event(
                event_type="emergency_close_not_flat",
                severity=10,
                description=msg,
                source="order_manager",
            )
            self._notify_threadsafe(msg)
            return

        # ── Step 4: Verify all orders are cleared AFTER confirmed flat ──────────
        orders_clear = self._verify_orders_cancelled(ccxt_symbol)
        if not orders_clear:
            self._trading_halted = True
            self._notify_threadsafe(
                f"🚨 <b>Orders not cleared after emergency close</b>: {pair}\n"
                f"Check OKX manually. /resume after verifying."
            )
        else:
            print(
                f"[order_manager] Post-emergency-close orders confirmed clear: {pair}",
                flush=True,
            )

        # Confirmed flat — notify success
        self._notify_threadsafe(
            f"⚠️ Emergency close confirmed flat: {pair} {direction}\n"
            f"Reason: {reason}"
        )

    def _reconcile_order(
        self,
        order_id: Optional[str],
        ccxt_symbol: str,
        client_order_id: str,
    ) -> "dict | None":
        """
        Determine whether an order was filled after a timeout or create exception.

        order_id=None  → order_id unknown (create raised before returning id);
                          searches open/closed orders by clientOrderId.
        order_id=str   → searches by OKX order id first, then falls through.

        Returns:
          dict(filled_price, filled_qty, order_id) — fill confirmed
          None                                      — confirmed not filled
          dict(status="ambiguous")                  — state unknown; trading halted
        """
        # Step 1: Re-fetch by order_id (when known)
        if order_id is not None:
            try:
                fetched = self._exchange.fetch_order(order_id, ccxt_symbol)
                status  = fetched.get("status", "")
                if status == "closed":
                    fp = float(fetched.get("average") or 0)
                    fq = float(fetched.get("filled") or 0)
                    print(
                        f"[order_manager] Reconcile: {order_id} filled @ {fp:.4f}",
                        flush=True,
                    )
                    return {"filled_price": fp, "filled_qty": fq, "order_id": order_id}
                if status in ("canceled", "rejected"):
                    return None
                if status == "open":
                    try:
                        self._exchange.cancel_order(order_id, ccxt_symbol)
                    except Exception:
                        pass
                    return None
            except Exception as exc:
                print(
                    f"[order_manager] Reconcile fetch_order({order_id}) error: {exc}",
                    file=sys.stderr,
                )

        # Step 2: Search open orders (by order_id or clientOrderId)
        try:
            open_orders = self._exchange.fetch_open_orders(ccxt_symbol)
            for o in open_orders:
                match = (
                    (order_id is not None and o.get("id") == order_id)
                    or o.get("clientOrderId") == client_order_id
                    or o.get("info", {}).get("clOrdId") == client_order_id
                )
                if match:
                    # Found as still-open — cancel and return not filled
                    try:
                        self._exchange.cancel_order(o["id"], ccxt_symbol)
                    except Exception:
                        pass
                    return None
        except Exception as exc:
            print(
                f"[order_manager] Reconcile fetch_open_orders error: {exc}",
                file=sys.stderr,
            )

        # Step 3: Search closed orders by clientOrderId (needed when order_id=None)
        try:
            closed_orders = self._exchange.fetch_closed_orders(ccxt_symbol, limit=20)
            for o in closed_orders:
                match = (
                    (order_id is not None and o.get("id") == order_id)
                    or o.get("clientOrderId") == client_order_id
                    or o.get("info", {}).get("clOrdId") == client_order_id
                )
                if match and o.get("status") == "closed":
                    fp = float(o.get("average") or 0)
                    fq = float(o.get("filled") or 0)
                    if fp > 0 and fq > 0:
                        print(
                            f"[order_manager] Reconcile: {client_order_id} filled "
                            f"(from closed orders) @ {fp:.4f}",
                            flush=True,
                        )
                        return {
                            "filled_price": fp,
                            "filled_qty":   fq,
                            "order_id":     o["id"],
                        }
        except Exception as exc:
            print(
                f"[order_manager] Reconcile fetch_closed_orders error: {exc}",
                file=sys.stderr,
            )

        # Step 4: Check positions as last resort
        try:
            positions    = self._exchange.fetch_positions([ccxt_symbol])
            has_position = any(
                abs(float(p.get("contracts") or p.get("size") or 0)) > 0
                for p in positions
            )
            if not has_position:
                return None  # No position → order was not filled
        except Exception as exc:
            print(
                f"[order_manager] Reconcile fetch_positions error: {exc}",
                file=sys.stderr,
            )

        # Step 5: All checks exhausted — state is ambiguous
        label = order_id or client_order_id
        msg   = (
            f"Order {label} state AMBIGUOUS after all reconciliation attempts. "
            f"Verify OKX manually, then /resume to restart trading."
        )
        print(f"[order_manager] CRITICAL: {msg}", file=sys.stderr)
        self._trading_halted = True
        db_logger.log_event(
            event_type="order_state_ambiguous",
            severity=10,
            description=msg,
            source="order_manager",
        )
        self._notify_threadsafe(
            f"🚨 <b>CRITICAL: Order state ambiguous</b>\n"
            f"{msg}\nUse /resume after verifying OKX."
        )
        return {"status": "ambiguous"}

    # ── Order management ──────────────────────────────────────────────────────

    def cancel_all_orders(self, pair: str) -> None:
        """
        Cancel all open orders for the given pair via CCXT.
        Used by kill switch and position_monitor on live close.
        No-op in paper mode (no real orders to cancel).
        """
        if config.PAPER_MODE or not self._keys_configured:
            return
        ccxt_symbol = _to_ccxt_symbol(pair)
        try:
            self._exchange.cancel_all_orders(ccxt_symbol)
            print(f"[order_manager] Cancelled all open orders for {ccxt_symbol}", flush=True)
        except Exception as exc:
            print(
                f"[order_manager] cancel_all_orders({ccxt_symbol}) failed: {exc}",
                file=sys.stderr,
            )

    # ── Health check ─────────────────────────────────────────────────────────

    async def check_api_health(self) -> bool:
        """
        Ping the OKX REST endpoint. Returns True if healthy.
        Updates the health monitor heartbeat (feed name: 'exchange_rest').
        """
        if not self._keys_configured:
            return False
        try:
            self._exchange.fetch_time()
            self._monitor.report_healthy("exchange_rest")
            return True
        except Exception as exc:
            self._monitor.report_degraded("exchange_rest", str(exc))
            return False
