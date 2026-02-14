#!/bin/bash
set -e

echo "🚀 Setting up Autonomous Claude Agent on Amazon Linux..."
echo ""

# Update system
echo "📦 Updating system packages..."
sudo yum update -y

# Install Python 3.11 (Amazon Linux 2023)
echo "🐍 Installing Python 3.11..."
sudo yum install python3.11 python3.11-pip git -y

# Clone repository (if not already cloned)
cd ~
if [ ! -d "autonomous-claude-agent" ]; then
    echo "📥 Cloning repository..."
    read -p "Enter GitHub repository URL: " REPO_URL
    git clone "$REPO_URL" autonomous-claude-agent
fi

cd autonomous-claude-agent

# Create virtual environment
echo "🔧 Creating virtual environment..."
python3.11 -m venv venv
source venv/bin/activate

# Install dependencies
echo "📦 Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# Setup environment
if [ ! -f .env ]; then
    echo "⚙️  Setting up environment..."
    cp .env.example .env
    echo ""
    echo "⚠️  IMPORTANT: Configure .env file with your credentials!"
    echo "   nano .env"
    echo ""
    echo "Required settings:"
    echo "  - ANTHROPIC_API_KEY (required)"
    echo "  - TELEGRAM_BOT_TOKEN (optional but recommended)"
    echo "  - TELEGRAM_CHAT_ID (optional but recommended)"
    echo ""
    read -p "Press Enter when .env is configured..."
fi

# Create directories
echo "📁 Creating data directories..."
mkdir -p data/chroma data/core_brain data/digital_clone_brain data/memory data/logs credentials

# Install as systemd service
echo "🔧 Installing systemd service..."
CURRENT_USER=$(whoami)
CURRENT_DIR=$(pwd)

# Create service file with current user and directory
cat > /tmp/claude-agent.service << EOF
[Unit]
Description=Autonomous Claude Agent - Self-Building AI System
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$CURRENT_DIR
Environment="PATH=$CURRENT_DIR/venv/bin"
Environment="PYTHONUNBUFFERED=1"

# Start agent in self-build mode
ExecStart=$CURRENT_DIR/venv/bin/python src/main.py

# Auto-restart on failure
Restart=always
RestartSec=10
StartLimitInterval=300
StartLimitBurst=5

# Logging
StandardOutput=append:$CURRENT_DIR/data/logs/agent.log
StandardError=append:$CURRENT_DIR/data/logs/error.log

# Resource limits
MemoryLimit=4G
CPUQuota=200%

[Install]
WantedBy=multi-user.target
EOF

# Install service
sudo mv /tmp/claude-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable claude-agent

# Configure firewall for web dashboard
echo "🔥 Configuring firewall..."
if command -v firewall-cmd &> /dev/null; then
    sudo systemctl start firewalld || true
    sudo firewall-cmd --permanent --add-port=18789/tcp || true
    sudo firewall-cmd --reload || true
else
    echo "⚠️  firewalld not found, skipping firewall configuration"
fi

echo ""
echo "✅ Setup complete!"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Next Steps:"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "1. Configure .env file:"
echo "   nano .env"
echo ""
echo "2. Start the agent:"
echo "   sudo systemctl start claude-agent"
echo ""
echo "3. Check status:"
echo "   sudo systemctl status claude-agent"
echo ""
echo "4. View logs:"
echo "   sudo journalctl -u claude-agent -f"
echo "   # Or: tail -f data/logs/agent.log"
echo ""
echo "5. Access web dashboard:"
echo "   http://$(curl -s ifconfig.me):18789"
echo ""
echo "6. Control via Telegram (if configured):"
echo "   Send /start to your bot"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "The agent will run 24/7 as a systemd service!"
echo "It will auto-restart on failure and start on boot."
echo ""
