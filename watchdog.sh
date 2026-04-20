#!/usr/bin/env bash
# watchdog.sh — External process watchdog for the trading bot.
#
# Checks logs/.heartbeat file mtime every 2 minutes. If the heartbeat
# file hasn't been updated in 5+ minutes, the event loop is frozen.
# Force-restarts the bot via launchctl.
#
# This is the LAST line of defense — catches scenarios where the
# in-process watchdog thread itself has failed (e.g., Python GIL
# deadlock, corrupted interpreter state).
#
# Installed as a launchd agent: com.tradingbot.watchdog.plist

BOT_DIR="$HOME/Desktop/trading-bot"
HEARTBEAT="$BOT_DIR/logs/.heartbeat"
PLIST="$HOME/Library/LaunchAgents/com.tradingbot.plist"
MAX_AGE=360  # seconds — 6 minutes (heartbeat writes every 60s)
LOG="$BOT_DIR/logs/watchdog.log"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG"
}

# Don't run if the bot isn't supposed to be running
if ! launchctl list | grep -q "com.tradingbot$"; then
    exit 0
fi

# If heartbeat file doesn't exist, the bot hasn't started yet or
# is still in its first 60 seconds — skip this check
if [ ! -f "$HEARTBEAT" ]; then
    exit 0
fi

# Get file age in seconds
if [ "$(uname)" = "Darwin" ]; then
    FILE_MTIME=$(stat -f %m "$HEARTBEAT")
else
    FILE_MTIME=$(stat -c %Y "$HEARTBEAT")
fi
NOW=$(date +%s)
AGE=$(( NOW - FILE_MTIME ))

if [ "$AGE" -gt "$MAX_AGE" ]; then
    log "ALERT: heartbeat stale for ${AGE}s (threshold: ${MAX_AGE}s) — restarting bot"

    # Kill the frozen process
    PID=$(pgrep -f "trading-bot.*main.py" || true)
    if [ -n "$PID" ]; then
        log "Killing frozen bot PID $PID"
        kill -9 "$PID" 2>/dev/null || true
        sleep 2
    fi

    # Restart via launchctl
    launchctl unload "$PLIST" 2>/dev/null || true
    sleep 2
    launchctl load "$PLIST"
    log "Bot restarted via launchctl"
fi
