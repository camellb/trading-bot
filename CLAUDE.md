# Trading Bot — Project Context for Claude Code

## Read this first, every time

This file is the single source of truth. It overrides everything else.
Read it fully before touching any code.

---

## What this is

A personal autonomous crypto trading bot on a Mac Mini M4.
Trades BTC, ETH, and SOL perpetual futures on OKX.
Python 3.11 backend. Next.js dashboard.
Runs 24/7 as a launchd service. Control: ./bot.sh start|stop|restart|status|logs|errors

Core philosophy: acts like a professional fund manager.
Watches everything continuously. Acts when something meaningful happens.
Holds positions for days not minutes. Learns from every decision.
Claude makes all trading decisions — not rules, not layers, not thresholds.

---

## Architecture

### Two brains

Brain 1 — Gemini Flash Scanner (engine/scanner.py)
  Always running. Five parallel monitoring coroutines:
    _monitor_price_action()     1H candles, key level breaks, large candles
    _monitor_news()             Real-time Gemini triage every 90s
    _monitor_macro_calendar()   Pre-event warnings 2h ahead
    _monitor_funding_extremes() Crowding detection
    _monitor_iv_spikes()        Deribit DVOL spike detection
  When notable event detected: compiles full briefing → queues to strategist
  Does NOT make trading decisions.
  Startup: polls are_core_feeds_healthy() every 5s (max 60s) before first briefing.
  Skips briefings if core feeds are not healthy.
  Logs news events to macro_context.log_news_event() for sentiment scoring.

Brain 2 — Claude Sonnet Strategist (engine/strategist.py)
  Decisions processed via asyncio.PriorityQueue — never dropped.
  Priority: 1=critical news/urgent review, 3=urgent/thesis review, 5=routine.
  Receives comprehensive briefing: RSI, MACD, ATR, EMA(20/50), volume ratio,
  4H candles, order book depth/imbalance, funding rates, Deribit IV,
  Fear & Greed index, news headlines, macro context, portfolio state,
  Obsidian memory (what-works, what-doesnt-work, current-thesis).
  Makes ALL portfolio decisions: ENTER / EXIT / WAIT.
  ADJUST action is DISABLED — sends Telegram warning, takes no action.
  Hard limits enforced at execution time (not from queued briefing snapshot):
    - Fresh DB query for deployed capital immediately before ENTER
    - One position per symbol enforced from fresh state
    - Total deployed ≤ 50% capital from fresh balance
    - Stop/TP direction validated (long: stop < entry < tp)
    - Feed health checked before any trade

---

## Execution safety model

### ENTER flow (order_manager._place_live_order)

1. Generate deterministic clientOrderId: wldk_{pair}_{timestamp}
2. Validate stop/TP direction before submission
3. Submit limit order to OKX with clientOrderId
4. On timeout/error: _reconcile_order() checks:
   - fetch_order(order_id)
   - fetch_open_orders(symbol)
   - fetch_positions(symbol)
   If ambiguous: halt trading, send CRITICAL Telegram, return failed
5. Verify fill from exchange — fetch actual filled_qty
6. Place stop-loss reduce-only order on OKX
   If stop placement fails: _emergency_close() + CRITICAL alert
7. Log to DB with filled_qty
   If trade_id <= 0 (DB failed): _emergency_close() + CRITICAL alert
8. register_position() in PositionMonitor immediately
9. Write to Obsidian vault
10. Send Telegram trade notification

### EXIT flow (order_manager.close_position — async)

1. Use _to_ccxt_symbol() for all OKX calls (BTC/USDT:USDT not BTC-USDT-SWAP)
2. Use actual filled_qty from DB, not recomputed from size_usd/entry_price
3. Submit reduce-only market close
4. Poll fetch_positions() 6x5s to confirm flat
   If not flat after 30s: halt trading + CRITICAL Telegram alert
5. Only after confirmed flat: update DB + remove from PositionMonitor + Obsidian

### Trading halt

order_manager._trading_halted = True when:
- Order state ambiguous after all reconciliation attempts
- Position not flat after close confirmation timeout
- DB logging failed after live fill

Resume: send /resume in Telegram (after manual OKX check)
All new ENTER/EXIT blocked while halted.

### PAPER_MODE

Skips steps 4-5 in EXIT (no real position to confirm flat)
Skips step 6-7 in ENTER confirmation (no real OKX stop order)
All other logic identical to live mode.

---

## Multi-source sentiment system (engine/macro_context.py)

Five weighted scorers → stable composite sentiment:
  price_momentum  25%  EMA alignment + volume per pair
  derivatives     25%  Funding rate contrarian signal
  fear_greed      20%  Alternative.me index (contrarian, daily)
  macro_regime    20%  BTC dominance, event risk, funding trend
  news_catalyst   10%  Rolling 24H scored event buffer

Composite → RISK_ON (>0.25) / NEUTRAL / RISK_OFF (<-0.25)
Confidence = source agreement (5/5 = 1.0, split = 0.5)

News events logged via log_news_event() as Scanner processes notable+ headlines
Events expire after 24H — rolling window not snapshot
Only flips sentiment when multiple sources agree

Risk multiplier from composite:
  > 0.25  → 1.0  (full size)
  > -0.25 → 0.85 (slight caution)
  > -0.5  → 0.65 (reduced)
  ≤ -0.5  → 0.4  (very cautious)

---

## Persistent memory — Obsidian vault

Path: ~/Documents/trading-bot-memory/
  trades/           {date}-{pair}-{dir}-{trade_id}-OPEN.md (unique per trade_id)
  market-context/   daily briefs
  strategy/         what-works.md, what-doesnt-work.md, current-thesis.md
  patterns/         setup types, mistake log
  performance/      weekly and monthly reviews

Claude reads strategy files before every single decision.
Weekly self-improvement updates all three strategy files.

---

## Key files — do not modify without good reason

feeds/okx_ws.py
  _KLINE_INTERVALS includes "1H"
  After kline reconnect: backfills missed candles from REST before marking healthy
  get_closed_candles(), get_latest_ticker(), get_orderbook()
  Candle dict keys: open, high, low, close, volume (strings, cast to float)

feeds/deribit_feed.py        DVOL IV, overlay feed not core
feeds/news_feed.py           RSS + Nitter + CryptoPanic + Gemini filter
feeds/macro_calendar.py      FOMC + BLS events
feeds/feed_health_monitor.py are_core_feeds_healthy() — gating both entries and stops
feeds/telegram_notifier.py   Telegram + /apply /skip /status /resume /help
db/models.py                 Schema — trades has thesis, trigger_event, filled_qty
db/logger.py                 load_open_trades() for position reload
execution/order_manager.py   Atomic entry, clientOrderId, reconciliation, halt logic
execution/position_monitor.py register_position(), _closing set, health gate, async close
engine/macro_context.py      5-source sentiment, log_news_event(), set_ws_manager()
engine/portfolio_advisor.py  Weekly capital advisory
engine/self_improvement.py   Weekly analysis + Obsidian memory update
engine/memory.py             Obsidian vault — trade_id in filenames
backtester/                  Isolated — never affects live bot

---

## Database schema

trades: thesis TEXT, trigger_event TEXT, filled_qty FLOAT
ticks: conviction_score FLOAT, conviction_label VARCHAR(20), iv FLOAT, iv_spike BOOLEAN
sentiment_scores: full 5-source breakdown per generation
news_event_log: rolling 24H news event buffer

Open positions: timestamp_close IS NULL — no status column ever

---

## Telegram commands

/apply    Apply pending self-improvement config suggestions
/skip     Skip pending suggestions
/status   Full vitals: balance, P&L, positions with thesis, feeds, uptime
/resume   Resume trading after halt (use after manual OKX verification)
/help     List all commands

---

## Telegram notification policy

Trade entered:          reasoning, size, confidence, expected hold
Trade exited:           P&L, exit reason, post-mortem
ADJUST requested:       warning — disabled, no action
Invalid stop/TP:        warning with specific reason
Trading halted:         🚨 CRITICAL with reason
Position not flat:      🚨 CRITICAL — manual intervention required
Order ambiguous:        🚨 CRITICAL — check OKX manually
DB fail after fill:     🚨 CRITICAL — emergency close attempted
Feed degraded:          after 60s continuous degradation
Daily brief:            08:30 MYT — macro + portfolio + thesis
Weekly summary:         Monday 08:30 MYT
Self-improvement:       Sunday 08:30 MYT

---

## Scheduled jobs (MYT = UTC+8)

Daily macro brief + summary  08:30 MYT (00:30 UTC)
Weekly summary + advisory    Monday 08:30 MYT
Self-improvement + memory    Sunday 08:30 MYT
Monthly report               1st of month 08:30 MYT

---

## OKX configuration

PAPER_MODE = True until M8 go-live
TRADING_PAIRS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
CCXT for all order placement — always _to_ccxt_symbol() for conversions
WebSocket for all market data
1H candles: 199 bars backfilled on startup, reconnect backfill for gaps

---

## Non-negotiable rules

1.  Claude is the decision maker. Not rules. Not layers. Not thresholds.
2.  Every position must have a stop loss. Validated before submission.
3.  Never deploy more than 50% of capital. Checked from FRESH DB state.
4.  One position per symbol. Enforced from fresh state before ENTER.
5.  Stop/TP direction must be valid. Invalid levels rejected and alerted.
6.  Never mark a trade closed before exchange confirms flat position.
7.  Always use close_position() — never call exchange.create_order() directly.
8.  Always use _to_ccxt_symbol() for any OKX API call.
9.  Use actual filled_qty from fill response — never recompute from size/price.
10. New fills registered in PositionMonitor immediately after DB write.
11. ADJUST action is disabled — Claude should not use it.
12. Never trade on stale/degraded feeds.
13. Decision queue — urgent triggers prioritised, never dropped.
14. On ambiguous order state: halt trading, alert, wait for /resume.
15. In live mode: missing credentials are fatal at startup.
16. Paper mode only changes order execution. Everything else identical.
17. Never reinstate the 15-minute loop or Layer A-F engine.
18. Never modify backtester/ for live bot changes.

---

## Module map

trading-bot/
├── config.py
├── main.py
├── .env
├── bot.sh
├── logs/
├── feeds/
│   ├── okx_ws.py                reconnect backfill for missed candles
│   ├── book_manager.py
│   ├── news_feed.py
│   ├── macro_calendar.py
│   ├── feed_health_monitor.py
│   ├── telegram_notifier.py     /resume command added
│   └── deribit_feed.py
├── engine/
│   ├── scanner.py               health-gated, priority queue, logs news events
│   ├── strategist.py            fresh risk gates, stop validation, ADJUST disabled
│   ├── memory.py                trade_id in filenames
│   ├── macro_context.py         5-source sentiment, news event buffer
│   ├── portfolio_advisor.py
│   ├── self_improvement.py
│   └── risk_engine.py           backtester utilities only
├── execution/
│   ├── order_manager.py         clientOrderId, reconciliation, halt, flat confirm
│   └── position_monitor.py      register_position, _closing set, async close
├── db/
│   ├── models.py                filled_qty column on trades
│   └── logger.py
├── backtester/
└── dashboard/
    └── app/
        ├── page.tsx
        ├── globals.css
        └── api/
            ├── brain/
            └── ...

---

## M8 go-live checklist

Config values to review before PAPER_MODE = False:
  PAPER_MODE, EXCHANGE, TRADING_PAIRS
  STARTING_CAPITAL_USD, DAILY_LOSS_CAP_USD
  PORTFOLIO_SCALE_ENABLED, PORTFOLIO_MIN_TRADE_USD, PORTFOLIO_MAX_TRADE_USD
  PORTFOLIO_DAILY_CAP_PCT, MAX_SIMULTANEOUS_POSITIONS
  ORDER_TYPE, LIMIT_ORDER_TIMEOUT_S, LIMIT_ORDER_SLIP_TICKS
  TICKER_STALE_THRESHOLD_S, MARKPRICE_STALE_THRESHOLD_S, KLINE_STALE_THRESHOLD_MIN
  CLAUDE_MODEL, GEMINI_MODEL, OBSIDIAN_VAULT_PATH

Pre-go-live steps:
1.  Run clean paper week — review Claude's decisions daily
2.  Check Obsidian vault — is memory accumulating correctly?
3.  Sunday self-improvement — strategy memory updating?
4.  Confirm thesis-driven exits firing (4H position reviews)
5.  Confirm sentiment system producing consistent scores
6.  Confirm /resume command works
7.  Confirm CRITICAL alerts fire correctly in paper mode tests
8.  Reset DB: TRUNCATE trades, positions, daily_pnl, ticks RESTART IDENTITY
9.  Set PAPER_MODE = False
10. Fund OKX live with $500 USDT
11. Confirm API key: trade only, no withdrawal
12. Test /status and /resume before first live trade
13. Monitor first 5 live trades manually before leaving unattended

---

## Structured trade output (added 2026-04-16)

Every ENTER decision from Claude now includes required structured fields.
These are stored in the trades table and used for performance attribution.

### New fields on every ENTER

playbook: swing | momentum | mean_reversion | news_catalyst | macro
  swing:          multi-day trend trade, 2-7 days, technical setup
  momentum:       breakout/continuation, hours to 2 days, volume driven
  mean_reversion: oversold/overbought bounce, hours to 1 day
  news_catalyst:  event-driven, holds until catalyst resolves
  macro:          macro theme trade, 3-14 days, fundamental driven

time_horizon_days: expected hold duration in days
catalyst:          specific event or setup creating the opportunity
invalidation:      specific condition that proves the thesis wrong
primary_signal:    single most important factor driving the decision
risk_reward:       calculated (tp - entry) / (entry - sl)
market_condition:  trending | ranging | volatile | low_volatility

### New fields on every EXIT

exit_type: thesis_broken | target_reached | time_decay | risk_management
what_happened: how the trade played out vs original thesis

### Performance attribution system

engine/self_improvement.py now runs 5 SQL queries every Sunday:
  1. Per-playbook: win rate, avg P&L, planned vs actual RR, hold hours
  2. Per-market-condition: win rate, avg P&L
  3. Overall slippage: planned RR vs actual RR degradation %
  4. Exit-type breakdown: count and avg P&L per exit reason
  5. Time-horizon accuracy: planned vs actual hold days

Results formatted and injected into Claude's weekly suggestion prompt.
Claude can now say: "momentum trades losing consistently — reduce use"
rather than guessing from qualitative notes.

Weekly Telegram includes second message with attribution:
  ✅ playbook performing (>60% win, positive avg P&L)
  ⚠️ playbook underperforming (<40% win, negative avg P&L)

### The self-improvement loop (fully closed)

Claude decides → structured fields stored in DB
      ↓
Trade executes → filled_qty, actual exit price stored
      ↓
Sunday: _compute_performance_attribution() measures outcomes
      ↓
Claude sees quantitative data: which playbooks work, slippage %
      ↓
_get_suggestions() grounded in measured outcomes not theory
      ↓
/apply updates config → Obsidian memory updated
      ↓
Claude reads updated memory before every next decision

---

## Execution safety — final state (2026-04-16)

Three Codex review passes completed. All critical and high severity
issues resolved. Summary of all safety guarantees now in place:

### Entry path guarantees

1. clientOrderId generated before every live order submission
2. create_limit_order() wrapped in try/except — reconciles by clientOrderId on failure
3. Reconciliation checks: fetch_order → fetch_open_orders → fetch_positions
4. Ambiguous state: _trading_halted = True + CRITICAL Telegram + requires /resume
5. Fill confirmed from exchange — filled_qty stored from actual fill response
6. SL/TP orders use actual filled_qty not intended size
7. SL/TP placement failure: _emergency_close() + confirmed flat + halt on uncertainty
8. DB logging failure after live fill: _emergency_close() + CRITICAL alert
9. register_position() called immediately after DB write confirmed
10. Feed health rechecked at execution time (not just at briefing build time)
11. Hard risk kernel enforced from fresh DB state immediately before order submission:
    - Daily loss cap check
    - Max simultaneous positions
    - One position per symbol
    - Min/max trade size
    - 50% capital deployment cap

### Exit path guarantees

1. clientOrderId generated for every live close
2. close_position() wraps create_order() in try/except
3. On submission failure: checks position state directly (market close approach)
4. Ambiguous close state: _trading_halted = True + CRITICAL Telegram
5. Polls fetch_positions() 6x5s to confirm flat (30 second window)
6. Not flat after 30s: _trading_halted = True + CRITICAL Telegram
7. Returns actual fill price from exchange (create_order response or fetch_order)
8. DB update only after confirmed flat
9. _positions entry not deleted until DB write confirmed
10. log_trade_close() returns True/False — failure is explicit not silent
11. DB failure: position flagged db_close_failed, monitoring continues

### Emergency close guarantees

_emergency_close() called when SL/TP placement or DB logging fails:
1. Submits reduce-only market close with clientOrderId
2. Polls fetch_positions() 6x5s
3. _trading_halted = True if not flat after 30s
4. CRITICAL Telegram + severity=10 DB event log entry
5. /resume required after manual OKX verification

### Portfolio state guarantees

- daily_pnl (date, paper) composite unique constraint
- Paper and live P&L rows fully separated
- _get_todays_pnl() always filters by config.PAPER_MODE
- Kill switch reads paper-correct P&L

### Telegram commands

/apply    Apply pending config suggestions
/skip     Skip pending suggestions
/status   Full vitals
/resume   Resume trading after halt — use after manual OKX verification
/help     All commands

### Codex review verdict (after 3 passes)

Pass 1: 7 critical/high fixes
Pass 2: 5 critical/high fixes
Pass 3: 6 critical/high fixes (all confirmed implemented)
Current status: execution path safe for live trading
Remaining architectural improvements: separate playbooks, exact telemetry