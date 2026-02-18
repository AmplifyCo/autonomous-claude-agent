#!/bin/bash
set -e

echo "🚀 Setting up Autonomous Claude Agent on Amazon Linux..."
echo ""

# ============================================
# System Requirements Check
# ============================================
echo "🔍 Checking system requirements..."
echo ""

# Check if running on Amazon Linux
if [ -f /etc/os-release ]; then
    . /etc/os-release
    if [[ "$ID" != "amzn" ]]; then
        echo "⚠️  WARNING: This script is designed for Amazon Linux"
        echo "   Detected: $PRETTY_NAME"
        echo "   Continuing anyway..."
        echo ""
    else
        echo "✅ Amazon Linux detected: $PRETTY_NAME"
    fi
fi

# Check available disk space (need at least 20GB, recommend 40GB)
AVAILABLE_SPACE=$(df / | tail -1 | awk '{print $4}')
AVAILABLE_GB=$((AVAILABLE_SPACE / 1024 / 1024))

echo "💾 Available disk space: ${AVAILABLE_GB}GB"

if [ $AVAILABLE_GB -lt 20 ]; then
    echo "❌ ERROR: Insufficient disk space!"
    echo "   Required: At least 20GB"
    echo "   Available: ${AVAILABLE_GB}GB"
    echo ""
    echo "Please increase your EC2 volume size and run this script again."
    exit 1
elif [ $AVAILABLE_GB -lt 40 ]; then
    echo "⚠️  WARNING: Disk space below recommended"
    echo "   Available: ${AVAILABLE_GB}GB (Recommended: 40GB)"
    echo "   Continuing anyway..."
    echo ""
else
    echo "✅ Sufficient disk space: ${AVAILABLE_GB}GB"
fi

# Check total RAM (need at least 1.7GB total for 2GB instances, recommend 4GB)
# Use MB for accuracy, convert to GB for display
TOTAL_RAM_MB=$(free -m | grep Mem | awk '{print $2}')
TOTAL_RAM_GB=$(echo "scale=1; $TOTAL_RAM_MB / 1024" | bc)

echo "🧠 Total RAM: ${TOTAL_RAM_GB}GB"

# Require at least 1700 MB to account for kernel/BIOS reservation on 2GB instances
if [ $TOTAL_RAM_MB -lt 1700 ]; then
    echo "❌ ERROR: Insufficient RAM!"
    echo "   Required: At least 1.7GB total"
    echo "   Detected: ${TOTAL_RAM_GB}GB total"
    echo ""
    echo "Please use a larger EC2 instance type (t3.small or larger)"
    exit 1
elif [ $TOTAL_RAM_MB -lt 4096 ]; then
    echo "⚠️  WARNING: RAM below recommended"
    echo "   Total: ${TOTAL_RAM_GB}GB (Recommended: 4GB)"
    echo "   Agent performance may be limited. Continuing anyway..."
    echo ""
else
    echo "✅ Sufficient RAM: ${TOTAL_RAM_GB}GB"
fi

# Check CPU cores (1 core minimum, 2+ recommended)
CPU_CORES=$(nproc)

echo "⚡ CPU cores: ${CPU_CORES}"

if [ $CPU_CORES -lt 1 ]; then
    echo "❌ ERROR: No CPU cores detected!"
    exit 1
elif [ $CPU_CORES -lt 2 ]; then
    echo "⚠️  WARNING: Only 1 CPU core detected"
    echo "   Recommended: 2+ cores for better performance"
    echo "   Continuing anyway..."
    echo ""
else
    echo "✅ Sufficient CPU: ${CPU_CORES} cores"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "System Requirements Summary:"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Disk Space: ${AVAILABLE_GB}GB (Minimum: 20GB, Recommended: 40GB)"
echo "  RAM: ${TOTAL_RAM_GB}GB total (Minimum: 1.7GB, Recommended: 4GB)"
echo "  CPU Cores: ${CPU_CORES} (Minimum: 1, Recommended: 2+)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "✅ All system requirements met! Proceeding with installation..."
echo ""

# Set up temp directory on main disk (not tmpfs which is often <1GB)
echo "📁 Setting up temp directory on main disk..."
export TMPDIR=~/tmp_pip
mkdir -p $TMPDIR
echo "   Using: $TMPDIR (instead of /tmp which is only $(df -h /tmp | tail -1 | awk '{print $2}'))"
echo ""

sleep 2

# Update system
echo "📦 Updating system packages..."
sudo yum update -y

# Install Python 3.11 and browser tools (Amazon Linux 2023)
echo "🐍 Installing Python 3.11 and browser tools..."
sudo yum install python3.11 python3.11-pip git -y

echo "🌐 Installing browser tools (w3m)..."
# Install w3m (always available)
# Use --skip-broken to avoid curl-minimal conflicts
sudo yum install w3m -y --skip-broken --exclude=curl

# curl-minimal (pre-installed) provides curl command, no need to install full curl
if ! command -v curl &> /dev/null; then
    echo "⚠️  curl not available, some features may not work"
else
    echo "✅ curl available (using curl-minimal)"
fi

# Try to install chromium (may not be available on all Amazon Linux versions)
if sudo yum install chromium -y 2>/dev/null; then
    echo "✅ Chromium installed"
else
    echo "⚠️  Chromium not available in repositories"
    echo "   Trying chromium-browser..."
    if sudo yum install chromium-browser -y 2>/dev/null; then
        echo "✅ Chromium-browser installed"
    else
        echo "⚠️  Chromium not available. Agent will use w3m for browsing."
        echo "   Browser automation (Selenium) will be disabled."
        echo "   Text-based browsing (w3m) will still work."
    fi
fi

# Install ChromeDriver for Selenium (only if chromium is installed)
if command -v chromium &> /dev/null || command -v chromium-browser &> /dev/null; then
    echo "📦 Installing ChromeDriver for Selenium..."
    CHROME_DRIVER_VERSION=$(curl -sS chromedriver.storage.googleapis.com/LATEST_RELEASE 2>/dev/null || echo "114.0.5735.90")
    wget -q -O /tmp/chromedriver.zip "https://chromedriver.storage.googleapis.com/${CHROME_DRIVER_VERSION}/chromedriver_linux64.zip" 2>/dev/null || {
        echo "⚠️  ChromeDriver download failed"
        echo "   Full browser automation may not work, but w3m text browsing will"
    }
    if [ -f /tmp/chromedriver.zip ]; then
        sudo unzip -q -o /tmp/chromedriver.zip -d /usr/local/bin/
        sudo chmod +x /usr/local/bin/chromedriver
        rm /tmp/chromedriver.zip
        echo "✅ ChromeDriver installed: $(chromedriver --version 2>/dev/null || echo 'installed')"
    fi
else
    echo "⚠️  Chromium not installed, skipping ChromeDriver"
    echo "   Agent will use text-based browsing (w3m) instead"
fi

# Clone repository (if not already cloned)
cd ~
if [ ! -d "digital-twin" ]; then
    echo "📥 Cloning repository..."

    # Use environment variable if provided, otherwise use default
    REPO_URL="${REPO_URL:-https://github.com/AmplifyCo/digital-twin.git}"

    echo "Repository: $REPO_URL"
    git clone "$REPO_URL" digital-twin
fi

cd digital-twin

# Create virtual environment
echo "🔧 Creating virtual environment..."
python3.11 -m venv venv
source venv/bin/activate

# Install dependencies
echo "📦 Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# Configure limited sudo access
echo "🔐 Configuring limited sudo access for agent..."
CURRENT_USER=$(whoami)
sudo tee /etc/sudoers.d/digital-twin > /dev/null << 'SUDOERS_EOF'
# Limited sudo access for autonomous agent
# Package management
ec2-user ALL=(ALL) NOPASSWD: /usr/bin/yum install *
ec2-user ALL=(ALL) NOPASSWD: /usr/bin/yum update *
ec2-user ALL=(ALL) NOPASSWD: /usr/bin/yum remove *
ec2-user ALL=(ALL) NOPASSWD: /usr/bin/apt-get install *
ec2-user ALL=(ALL) NOPASSWD: /usr/bin/apt-get update *
ec2-user ALL=(ALL) NOPASSWD: /usr/bin/apt install *
ec2-user ALL=(ALL) NOPASSWD: /usr/bin/apt update *
ec2-user ALL=(ALL) NOPASSWD: /usr/bin/pip install *

# Service management (only digital-twin service)
ec2-user ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart digital-twin
ec2-user ALL=(ALL) NOPASSWD: /usr/bin/systemctl status *
ec2-user ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop digital-twin
ec2-user ALL=(ALL) NOPASSWD: /usr/bin/systemctl start digital-twin

# Firewall management
ec2-user ALL=(ALL) NOPASSWD: /usr/bin/firewall-cmd *

# Log viewing
ec2-user ALL=(ALL) NOPASSWD: /usr/bin/journalctl *
SUDOERS_EOF

sudo chmod 440 /etc/sudoers.d/digital-twin
echo "✅ Limited sudo access configured"
echo ""
echo "Agent capabilities:"
echo "  ✅ Install packages (yum/apt/pip)"
echo "  ✅ Manage digital-twin service"
echo "  ✅ Configure firewall"
echo "  ✅ View system logs"
echo "  ❌ Cannot shutdown/reboot"
echo "  ❌ Cannot perform destructive operations"
echo ""

# Setup environment
if [ ! -f .env ]; then
    echo "⚙️  Creating .env file with placeholders..."
    cat > .env << 'ENV_EOF'
# ================================================
# CORE CONFIGURATION - REQUIRED
# ================================================

# Anthropic API Key (REQUIRED - Get from: https://console.anthropic.com/)
ANTHROPIC_API_KEY=your_anthropic_api_key_here

# Default models
DEFAULT_MODEL=claude-opus-4-6
SUBAGENT_MODEL=claude-sonnet-4-5

# ================================================
# AGENT SETTINGS
# ================================================

# Maximum iterations before stopping
MAX_ITERATIONS=50

# Thinking budget tokens
THINKING_BUDGET=5000

# Auto-execute mode (true/false)
AUTO_EXECUTE=true

# ================================================
# BRAIN/MEMORY SETTINGS
# ================================================

# Vector database path
VECTOR_DB_PATH=./data/chroma

# Memory storage path
MEMORY_PATH=./data/memory

# ================================================
# GIT INTEGRATION
# ================================================

# Auto-commit completed features
AUTO_COMMIT=true

# Git user info for commits
GIT_USER_NAME=Autonomous Agent
GIT_USER_EMAIL=agent@autonomous.ai

# ================================================
# TELEGRAM INTEGRATION (Optional but Recommended)
# ================================================

# Telegram Bot Token (Get from @BotFather on Telegram)
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here

# Telegram Chat ID (Get from @userinfobot on Telegram)
TELEGRAM_CHAT_ID=your_telegram_chat_id_here

# ================================================
# WEB DASHBOARD
# ================================================

# Dashboard port
DASHBOARD_PORT=18789

# ================================================
# LOGGING
# ================================================

# Log level (DEBUG, INFO, WARNING, ERROR)
LOG_LEVEL=INFO

# Log file path
LOG_FILE=./data/logs/agent.log

# ================================================
# AUTO-UPDATE SETTINGS
# ================================================

# Enable automatic security updates
AUTO_UPDATE_ENABLED=true

# Update schedule (daily, weekly)
AUTO_UPDATE_SCHEDULE=daily

# Security-only updates
AUTO_UPDATE_SECURITY_ONLY=true

# Auto-restart after updates
AUTO_UPDATE_RESTART=true
ENV_EOF

    echo "✅ Created .env file with placeholders"
    echo ""
    echo "⚠️  IMPORTANT: Edit .env file with your credentials before starting!"
    echo "   nano .env"
    echo ""
    echo "Required settings:"
    echo "  - ANTHROPIC_API_KEY (required - get from https://console.anthropic.com/)"
    echo "  - TELEGRAM_BOT_TOKEN (optional but recommended - get from @BotFather)"
    echo "  - TELEGRAM_CHAT_ID (optional but recommended - get from @userinfobot)"
    echo ""
fi

# Create directories
echo "📁 Creating data directories..."
mkdir -p data/chroma data/core_brain data/digital_clone_brain data/memory data/logs credentials

# ============================================
# Cloudflare Tunnel Setup (Optional)
# ============================================
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Cloudflare Tunnel Setup (for Telegram webhooks)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Cloudflare Tunnel provides a permanent HTTPS URL for your agent."
echo "This is required for Telegram webhooks (instant messaging)."
echo ""
read -p "Do you want to set up Cloudflare Tunnel? (y/n): " setup_tunnel

if [[ "$setup_tunnel" =~ ^[Yy]$ ]]; then
    # Install cloudflared
    echo ""
    echo "📦 Installing cloudflared..."
    if command -v cloudflared &> /dev/null; then
        echo "✅ cloudflared already installed: $(cloudflared --version)"
    else
        curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /tmp/cloudflared
        chmod +x /tmp/cloudflared
        sudo mv /tmp/cloudflared /usr/local/bin/cloudflared
        echo "✅ cloudflared installed: $(cloudflared --version)"
    fi

    # Check if already authenticated
    if [ ! -f ~/.cloudflared/cert.pem ]; then
        echo ""
        echo "🔐 Cloudflare Authentication Required"
        echo ""
        echo "This will open a browser window. Please:"
        echo "1. Login to your Cloudflare account"
        echo "2. Authorize the tunnel"
        echo "3. Return here when done"
        echo ""
        read -p "Press Enter to start authentication..."

        cloudflared tunnel login

        echo ""
        echo "✅ Authentication complete!"
    else
        echo "✅ Already authenticated with Cloudflare"
    fi

    # Prompt for domain
    echo ""
    echo "📋 Domain Configuration"
    echo ""
    read -p "Enter your domain name (e.g., amplify-pixels.com): " user_domain
    read -p "Enter subdomain for webhook (e.g., webhook): " subdomain

    TUNNEL_HOSTNAME="${subdomain}.${user_domain}"

    # Create or use existing tunnel
    TUNNEL_NAME="digital-twin"

    if cloudflared tunnel list 2>/dev/null | grep -q "$TUNNEL_NAME"; then
        echo "✅ Using existing tunnel: $TUNNEL_NAME"
        TUNNEL_ID=$(cloudflared tunnel list | grep "$TUNNEL_NAME" | awk '{print $1}')
    else
        echo "🚇 Creating new tunnel: $TUNNEL_NAME"
        TUNNEL_ID=$(cloudflared tunnel create "$TUNNEL_NAME" 2>&1 | grep -oP 'Created tunnel .* with id \K[a-f0-9-]+')
        echo "✅ Created tunnel: $TUNNEL_ID"
    fi

    # Create DNS route
    echo "🌐 Creating DNS route: $TUNNEL_HOSTNAME"
    cloudflared tunnel route dns "$TUNNEL_NAME" "$TUNNEL_HOSTNAME" || {
        echo "⚠️  DNS route may already exist, continuing..."
    }

    # Create tunnel config
    mkdir -p ~/.cloudflared
    cat > ~/.cloudflared/config.yml << EOF
tunnel: $TUNNEL_ID
credentials-file: $HOME/.cloudflared/$TUNNEL_ID.json

ingress:
  - hostname: $TUNNEL_HOSTNAME
    service: http://localhost:18789
  - service: http_status:404
EOF

    echo "✅ Tunnel configuration created"

    # Create cloudflared systemd service
    sudo tee /etc/systemd/system/cloudflared.service > /dev/null << EOF
[Unit]
Description=Cloudflare Tunnel
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
ExecStart=/usr/local/bin/cloudflared tunnel --config $HOME/.cloudflared/config.yml run $TUNNEL_NAME
Restart=always
RestartSec=10
StandardOutput=append:$CURRENT_DIR/data/logs/cloudflared.log
StandardError=append:$CURRENT_DIR/data/logs/cloudflared.log

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable cloudflared
    sudo systemctl start cloudflared

    echo "✅ Cloudflare Tunnel service started"

    # Save tunnel info
    cat > data/cloudflare_tunnel.json << EOF
{
  "tunnel_mode": "named",
  "tunnel_id": "$TUNNEL_ID",
  "tunnel_name": "$TUNNEL_NAME",
  "tunnel_url": "https://$TUNNEL_HOSTNAME",
  "webhook_url": "https://$TUNNEL_HOSTNAME/telegram/webhook",
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

    echo "💾 Tunnel info saved to data/cloudflare_tunnel.json"

    # Wait for tunnel to connect
    echo "⏳ Waiting for tunnel to connect..."
    sleep 5

    # Set up Telegram webhook if bot token is configured
    if grep -q "TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here" .env; then
        echo ""
        echo "⚠️  Telegram bot token not configured yet"
        echo "   After editing .env, run this to set webhook:"
        echo "   curl -X POST \"https://api.telegram.org/bot\$TELEGRAM_BOT_TOKEN/setWebhook\" \\"
        echo "     -H \"Content-Type: application/json\" \\"
        echo "     -d '{\"url\": \"https://$TUNNEL_HOSTNAME/telegram/webhook\"}'"
    else
        # Try to set webhook automatically
        TELEGRAM_BOT_TOKEN=$(grep TELEGRAM_BOT_TOKEN .env | cut -d '=' -f2 | tr -d ' "'"'"'')

        if [ ! -z "$TELEGRAM_BOT_TOKEN" ]; then
            echo ""
            echo "📱 Setting up Telegram webhook..."
            WEBHOOK_URL="https://$TUNNEL_HOSTNAME/telegram/webhook"

            RESPONSE=$(curl -s -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
                -H "Content-Type: application/json" \
                -d "{\"url\": \"$WEBHOOK_URL\"}")

            if echo "$RESPONSE" | grep -q '"ok":true'; then
                echo "✅ Telegram webhook configured!"
                echo "   Webhook: $WEBHOOK_URL"
            else
                echo "⚠️  Webhook setup failed. You can set it manually after starting the agent."
            fi
        fi
    fi

    echo ""
    echo "✅ Cloudflare Tunnel setup complete!"
    echo "   Your permanent URL: https://$TUNNEL_HOSTNAME"
    echo ""
else
    echo "⏭️  Skipping Cloudflare Tunnel setup"
    echo "   You can run deploy/cloudflare/setup-tunnel.sh later if needed"
    echo ""
fi

# Install as systemd service
echo "🔧 Installing systemd service..."
CURRENT_USER=$(whoami)
CURRENT_DIR=$(pwd)

# Create service file with current user and directory
cat > /tmp/digital-twin.service << EOF
[Unit]
Description=Digital Twin - Self-Building AI System with Dual Brain Architecture
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
sudo mv /tmp/digital-twin.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable digital-twin

# Configure automatic system updates
echo "🔄 Configuring automatic security updates..."
sudo yum install yum-cron -y
sudo systemctl enable yum-cron
sudo systemctl start yum-cron

# Configure yum-cron for security-only updates
sudo sed -i 's/update_cmd = default/update_cmd = security/' /etc/yum/yum-cron.conf 2>/dev/null || true
sudo sed -i 's/apply_updates = no/apply_updates = yes/' /etc/yum/yum-cron.conf 2>/dev/null || true

echo "✅ Automatic security updates enabled"
echo "   System will auto-install security patches daily"

# Configure firewall for web dashboard
echo "🔥 Configuring firewall..."
if command -v firewall-cmd &> /dev/null; then
    sudo systemctl start firewalld || true
    sudo firewall-cmd --permanent --add-port=18789/tcp || true
    sudo firewall-cmd --reload || true
else
    echo "⚠️  firewalld not found, skipping firewall configuration"
fi

# Cleanup installation files
echo ""
echo "🧹 Cleaning up installation files..."
# Clean yum cache
sudo yum clean all > /dev/null 2>&1

# Clean pip cache
pip cache purge > /dev/null 2>&1 || true

# Remove any temporary files
rm -f /tmp/chromedriver.zip 2>/dev/null || true
rm -f /tmp/digital-twin.service 2>/dev/null || true

# Clean up downloaded setup script if it exists
rm -f ~/amazon-linux-setup.sh 2>/dev/null || true

# Clean up temp directory
rm -rf ~/tmp_pip 2>/dev/null || true

# Get disk space after cleanup
FINAL_SPACE=$(df / | tail -1 | awk '{print $4}')
FINAL_GB=$((FINAL_SPACE / 1024 / 1024))
FREED_SPACE=$((FINAL_GB - AVAILABLE_GB))

if [ $FREED_SPACE -gt 0 ]; then
    echo "✅ Cleanup complete! Freed ${FREED_SPACE}GB of disk space"
else
    echo "✅ Cleanup complete!"
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
echo "   sudo systemctl start digital-twin"
echo ""
echo "3. Check status:"
echo "   sudo systemctl status digital-twin"
echo ""
echo "4. View logs:"
echo "   sudo journalctl -u digital-twin -f"
echo "   # Or: tail -f data/logs/agent.log"
echo ""
echo "5. Access web dashboard:"
if [ -f data/cloudflare_tunnel.json ]; then
    TUNNEL_URL=$(grep -oP '"tunnel_url":\s*"\K[^"]+' data/cloudflare_tunnel.json)
    echo "   $TUNNEL_URL (via Cloudflare Tunnel)"
else
    echo "   http://$(curl -s ifconfig.me):18789"
    echo "   ⚠️  Note: If Telegram webhooks fail, set up Cloudflare Tunnel:"
    echo "      bash deploy/cloudflare/setup-tunnel.sh"
fi
echo ""
echo "6. Control via Telegram (if configured):"
echo "   Send 'What's your status?' to your bot"
if [ -f data/cloudflare_tunnel.json ]; then
    echo "   ✅ Webhook URL configured for instant responses"
fi
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "🎉 The agent will run 24/7 as a systemd service!"
echo ""
echo "Features enabled:"
echo "  ✅ Auto-restart on failure"
echo "  ✅ Auto-start on boot"
echo "  ✅ Limited sudo for package installation"
echo "  ✅ Web browsing (w3m text mode + Chromium headless)"
echo "  ✅ Telegram notifications and commands"
echo "  ✅ Web dashboard on port 18789"
echo "  ✅ Automatic security updates (system + Python)"
echo "  ✅ Daily vulnerability scanning"
echo ""
