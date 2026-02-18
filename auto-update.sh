#!/bin/bash
# Auto-update script for Digital Twin
# Checks for git updates, pulls if available, and restarts the bot

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$SCRIPT_DIR/logs/auto-update.log"
LOCK_FILE="$SCRIPT_DIR/.auto-update.lock"

# Create logs directory if it doesn't exist
mkdir -p "$SCRIPT_DIR/logs"

# Function to log messages
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# Prevent multiple instances
if [ -f "$LOCK_FILE" ]; then
    PID=$(cat "$LOCK_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        log "⚠️  Auto-update already running (PID: $PID)"
        exit 1
    fi
fi
echo $$ > "$LOCK_FILE"
trap "rm -f $LOCK_FILE" EXIT

log "🔍 Checking for updates..."

cd "$SCRIPT_DIR" || exit 1

# Fetch latest changes from remote
git fetch origin main 2>&1 | tee -a "$LOG_FILE"

# Check if there are updates
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    log "✅ Already up to date (commit: ${LOCAL:0:7})"
    exit 0
fi

log "📥 New updates available!"
log "   Current: ${LOCAL:0:7}"
log "   Latest:  ${REMOTE:0:7}"

# Check if there are local uncommitted changes
if ! git diff-index --quiet HEAD --; then
    log "⚠️  Warning: Local uncommitted changes detected"
    log "   Stashing changes before update..."
    git stash save "Auto-update stash $(date '+%Y-%m-%d %H:%M:%S')" 2>&1 | tee -a "$LOG_FILE"
fi

# Pull updates
log "📦 Pulling updates..."
if git pull origin main 2>&1 | tee -a "$LOG_FILE"; then
    log "✅ Updates pulled successfully"

    # Update dependencies if requirements.txt changed
    if git diff --name-only "$LOCAL" "$REMOTE" | grep -q "requirements.txt"; then
        log "📦 requirements.txt changed, updating dependencies..."
        python3 -m pip install -r requirements.txt 2>&1 | tee -a "$LOG_FILE"
    fi

    # Refresh global dt-setup if it changed and is installed globally
    GLOBAL_DT="/usr/local/bin/dt-setup"
    if [ -f "$GLOBAL_DT" ] && git diff --name-only "$LOCAL" "$REMOTE" | grep -q "^dt-setup$"; then
        log "🔧 dt-setup changed, updating global copy..."
        sudo cp "$SCRIPT_DIR/dt-setup" "$GLOBAL_DT" && sudo chmod +x "$GLOBAL_DT"
        log "✅ Global dt-setup updated"
    fi

    # Restart the bot
    log "🔄 Restarting Digital Twin bot..."

    # Try systemd first
    if systemctl is-enabled --quiet digital-twin 2>/dev/null; then
        sudo systemctl restart digital-twin
        log "✅ Bot restarted (systemd service)"
    elif pgrep -f "python.*src.main" > /dev/null; then
        # Kill existing process
        pkill -f "python.*src.main"
        sleep 2

        # Start new process
        cd "$SCRIPT_DIR"
        nohup python3 -m src.main > /dev/null 2>&1 &
        log "✅ Bot restarted (background process)"
    else
        log "⚠️  Bot not running, skipping restart"
    fi

    log "🎉 Auto-update completed successfully!"
else
    log "❌ Failed to pull updates"
    exit 1
fi
