#!/usr/bin/env bash
# bot.sh — Manage the trading bot + dashboard launchd services
# Usage: ./bot.sh [start|stop|restart|status|logs|errors|dash-logs|dash-errors]

PLIST="$HOME/Library/LaunchAgents/com.tradingbot.plist"
DASH_PLIST="$HOME/Library/LaunchAgents/com.tradingbot.dashboard.plist"
LABEL="com.tradingbot"
DASH_LABEL="com.tradingbot.dashboard"
LOG_FILE="$HOME/Desktop/trading-bot/logs/bot.log"
ERR_FILE="$HOME/Desktop/trading-bot/logs/bot_error.log"
DASH_LOG="$HOME/Desktop/trading-bot/dashboard/logs/dashboard.log"
DASH_ERR="$HOME/Desktop/trading-bot/dashboard/logs/dashboard_error.log"

case "$1" in
  start)
    echo "Starting trading bot..."
    launchctl load "$PLIST" 2>/dev/null || launchctl start "$LABEL"
    echo "Starting dashboard..."
    launchctl load "$DASH_PLIST" 2>/dev/null || launchctl start "$DASH_LABEL"
    sleep 3
    launchctl list | grep -E "com.tradingbot" || echo "(neither service loaded)"
    ;;

  stop)
    echo "Stopping trading bot..."
    launchctl unload "$PLIST" 2>/dev/null || true
    echo "Stopping dashboard..."
    launchctl unload "$DASH_PLIST" 2>/dev/null || true
    sleep 2
    ps aux | grep "[m]ain.py" && pkill -f "trading-bot.*main.py" && echo "Bot force-killed." || echo "Bot stopped."
    ps aux | grep "[n]ext-server" && pkill -f "next-server" && echo "Dashboard force-killed." || echo "Dashboard stopped."
    ;;

  restart)
    echo "Restarting trading bot and dashboard..."
    launchctl unload "$PLIST"      2>/dev/null || true
    launchctl unload "$DASH_PLIST" 2>/dev/null || true
    sleep 2
    launchctl load "$PLIST"
    launchctl load "$DASH_PLIST"
    sleep 3
    echo "=== Services after restart ==="
    launchctl list | grep -E "com.tradingbot"
    ;;

  status)
    echo "=== launchd status ==="
    launchctl list | grep -E "com.tradingbot" || echo "(not loaded)"
    echo ""
    echo "=== bot process ==="
    ps aux | grep "[m]ain.py" || echo "(not running)"
    echo ""
    echo "=== dashboard process ==="
    ps aux | grep "[n]ext-server\|[n]ext start" || echo "(not running)"
    ;;

  logs)
    LINES="${2:-50}"
    echo "=== Last $LINES lines of bot.log ==="
    tail -"$LINES" "$LOG_FILE"
    ;;

  errors)
    LINES="${2:-50}"
    echo "=== Last $LINES lines of bot_error.log ==="
    tail -"$LINES" "$ERR_FILE"
    ;;

  dash-logs)
    LINES="${2:-50}"
    echo "=== Last $LINES lines of dashboard.log ==="
    tail -"$LINES" "$DASH_LOG"
    ;;

  dash-errors)
    LINES="${2:-50}"
    echo "=== Last $LINES lines of dashboard_error.log ==="
    tail -"$LINES" "$DASH_ERR"
    ;;

  *)
    echo "Usage: $0 {start|stop|restart|status|logs|errors|dash-logs|dash-errors}"
    echo ""
    echo "  start       — load and start bot + dashboard"
    echo "  stop        — stop both services"
    echo "  restart     — restart both services"
    echo "  status      — show launchd state + running processes"
    echo "  logs        — tail bot.log (default 50 lines)"
    echo "  errors      — tail bot_error.log"
    echo "  dash-logs   — tail dashboard.log"
    echo "  dash-errors — tail dashboard_error.log"
    echo ""
    echo "Examples:"
    echo "  ./bot.sh status"
    echo "  ./bot.sh logs 100"
    echo "  ./bot.sh restart"
    exit 1
    ;;
esac
