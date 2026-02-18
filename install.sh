#!/bin/bash
# Install Digital Twin setup command globally
# Copies dt-setup to /usr/local/bin so you can run it like git, python, etc.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETUP_SCRIPT="$SCRIPT_DIR/dt-setup"
INSTALL_DIR="/usr/local/bin"
COMMAND_NAME="dt-setup"

echo "📦 Installing dt-setup command..."

# Check if dt-setup script exists
if [ ! -f "$SETUP_SCRIPT" ]; then
    echo "❌ Error: dt-setup script not found at $SETUP_SCRIPT"
    exit 1
fi

# Make script executable
chmod +x "$SETUP_SCRIPT"
chmod +x "$SCRIPT_DIR/configure.py"

# Check if /usr/local/bin exists
if [ ! -d "$INSTALL_DIR" ]; then
    echo "⚠️  $INSTALL_DIR does not exist. Creating it..."
    sudo mkdir -p "$INSTALL_DIR"
fi

# Remove old version if exists
if [ -f "$INSTALL_DIR/$COMMAND_NAME" ]; then
    echo "⚠️  Removing old version..."
    sudo rm -f "$INSTALL_DIR/$COMMAND_NAME"
fi

# Copy script to /usr/local/bin
echo "📋 Copying to $INSTALL_DIR/$COMMAND_NAME"
sudo cp "$SETUP_SCRIPT" "$INSTALL_DIR/$COMMAND_NAME"
sudo chmod +x "$INSTALL_DIR/$COMMAND_NAME"

echo "✅ Installation complete!"
echo ""
echo "You can now run from anywhere (like python, git, npm):"
echo "  dt-setup              # Full wizard"
echo "  dt-setup email        # Configure email only"
echo "  dt-setup telegram     # Configure Telegram only"
echo "  dt-setup core         # Configure API keys only"
echo ""
