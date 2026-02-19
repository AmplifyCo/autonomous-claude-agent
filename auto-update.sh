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

# Clean untracked files that conflict with incoming changes
# (e.g., builder agent created files that are now in git)
CONFLICTS=$(git merge-tree $(git merge-base HEAD origin/main) HEAD origin/main 2>/dev/null | grep "^+" | head -5)
UNTRACKED_CONFLICTS=$(git pull origin main --dry-run 2>&1 | grep "untracked working tree files" -A 100 | grep "^\s" | xargs)
if [ -n "$UNTRACKED_CONFLICTS" ]; then
    log "⚠️  Untracked files conflict with incoming changes, cleaning..."
    for f in $UNTRACKED_CONFLICTS; do
        if [ -f "$f" ]; then
            log "   Removing conflicting untracked file: $f"
            rm -f "$f"
        fi
    done
fi

# Pull updates (use pipefail to catch git errors through tee)
set -o pipefail
log "📦 Pulling updates..."
if git pull origin main 2>&1 | tee -a "$LOG_FILE"; then
    log "✅ Updates pulled successfully"

    # Update dependencies if requirements.txt changed
    # Always check/install dependencies to ensure environment is correct
    log "📦 Checking/Installing dependencies..."
    log "📦 Checking/Installing dependencies..."
    
    # Verify/Create venv
    if [ ! -f "$SCRIPT_DIR/venv/bin/pip" ] || ! "$SCRIPT_DIR/venv/bin/python3" --version >/dev/null 2>&1; then
        log "⚠️  Virtual environment missing or broken. Recreating..."
        rm -rf "$SCRIPT_DIR/venv"
        python3 -m venv "$SCRIPT_DIR/venv"
        log "✅ Virtual environment created"
    fi

    # Install dependencies
    "$SCRIPT_DIR/venv/bin/pip" install -r requirements.txt 2>&1 | tee -a "$LOG_FILE"
    
    # Ensure rights (if run as root/sudo but user owns dir)
    # Ensure correct permissions (recursive)
    # This prevents root-owned files from breaking the service
    fix_permissions() {
        # Only run if root
        if [ "$(id -u)" -eq 0 ]; then
            TARGET_USER="${SUDO_USER:-ec2-user}"
            if id "$TARGET_USER" &>/dev/null; then
                log "🔧 Fixing permissions for user: $TARGET_USER"
                chown -R "$TARGET_USER:$TARGET_USER" "$SCRIPT_DIR"
                # Ensure directories are executable/writable by owner
                find "$SCRIPT_DIR" -type d -exec chmod 755 {} +
                # Ensure logs are writable
                if [ -d "$SCRIPT_DIR/data/logs" ]; then
                    chmod 775 "$SCRIPT_DIR/data/logs"
                fi
            else
                log "⚠️  User $TARGET_USER not found, skipping chown"
            fi
        fi
    }
    trap fix_permissions EXIT

    # Update systemd service if changed
    SERVICE_FILE="digital-twin.service"
    INSTALLED_SERVICE="/etc/systemd/system/$SERVICE_FILE"
    if [ -f "$SERVICE_FILE" ] && [ -f "$INSTALLED_SERVICE" ]; then
        if ! cmp -s "$SERVICE_FILE" "$INSTALLED_SERVICE"; then
            log "🔧 Service file changed, updating systemd..."
            sudo cp "$SERVICE_FILE" "$INSTALLED_SERVICE"
            sudo systemctl daemon-reload
            log "✅ Systemd service updated"
        fi
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
