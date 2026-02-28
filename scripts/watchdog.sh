#!/bin/sh
# Ghost Ops Sentinel Watchdog — checks if sentinel is still running
# If the heartbeat file is >4 hours old, restart the daemon

HEARTBEAT="$HOME/.ghost-ops-heartbeat"
PLIST="$HOME/Library/LaunchAgents/com.dubsopenhub.ghost-ops.plist"

if [ ! -f "$HEARTBEAT" ]; then
    # No heartbeat file yet — sentinel hasn't run, restart daemon
    launchctl unload "$PLIST" 2>/dev/null
    sleep 2
    launchctl load "$PLIST"
    exit 0
fi

# Check if heartbeat is older than 4 hours (14400 seconds)
NOW=$(date +%s)
LAST=$(stat -f %m "$HEARTBEAT" 2>/dev/null || echo 0)
AGE=$(( NOW - LAST ))

if [ "$AGE" -gt 14400 ]; then
    logger -t ghost-ops-watchdog "Heartbeat is ${AGE}s old (>4h), restarting daemon"
    launchctl unload "$PLIST" 2>/dev/null
    sleep 2
    launchctl load "$PLIST"
fi
