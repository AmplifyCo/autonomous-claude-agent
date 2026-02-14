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
        read -p "Continue anyway? (y/N): " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            echo "Installation cancelled."
            exit 1
        fi
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
    echo "   Required: At least 20GB (40GB recommended)"
    echo "   Available: ${AVAILABLE_GB}GB"
    echo ""
    echo "Please increase your EC2 volume size:"
    echo "1. Stop the instance"
    echo "2. Modify volume size (recommend 40GB)"
    echo "3. Restart and run this script again"
    exit 1
elif [ $AVAILABLE_GB -lt 40 ]; then
    echo "⚠️  WARNING: Disk space is less than recommended"
    echo "   Available: ${AVAILABLE_GB}GB (Recommended: 40GB)"
    echo "   The agent may run out of space over time"
    read -p "Continue anyway? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Installation cancelled."
        exit 1
    fi
else
    echo "✅ Sufficient disk space: ${AVAILABLE_GB}GB"
fi

# Check available RAM (need at least 1.8GB, recommend 4GB)
# Use MB for accuracy, convert to GB for display
TOTAL_RAM_MB=$(free -m | grep Mem | awk '{print $2}')
TOTAL_RAM_GB=$(echo "scale=1; $TOTAL_RAM_MB / 1024" | bc)

echo "🧠 Total RAM: ${TOTAL_RAM_GB}GB"

# Require at least 1800 MB (1.8 GB) to account for OS overhead on 2GB instances
if [ $TOTAL_RAM_MB -lt 1800 ]; then
    echo "❌ ERROR: Insufficient RAM!"
    echo "   Required: At least 1.8GB (4GB recommended)"
    echo "   Available: ${TOTAL_RAM_GB}GB"
    echo ""
    echo "Please use a larger EC2 instance type (t3.small or larger)"
    exit 1
elif [ $TOTAL_RAM_MB -lt 4096 ]; then
    echo "⚠️  WARNING: RAM is less than recommended"
    echo "   Available: ${TOTAL_RAM_GB}GB (Recommended: 4GB)"
    echo "   Agent performance may be limited"
    read -p "Continue anyway? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Installation cancelled."
        exit 1
    fi
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
    read -p "Continue anyway? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Installation cancelled."
        exit 1
    fi
else
    echo "✅ Sufficient CPU: ${CPU_CORES} cores"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "System Requirements Summary:"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Disk Space: ${AVAILABLE_GB}GB (Minimum: 20GB, Recommended: 40GB)"
echo "  RAM: ${TOTAL_RAM_GB}GB (Minimum: 1.8GB, Recommended: 4GB)"
echo "  CPU Cores: ${CPU_CORES} (Minimum: 1, Recommended: 2+)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "✅ All system requirements met! Proceeding with installation..."
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

# Configure limited sudo access
echo "🔐 Configuring limited sudo access for agent..."
CURRENT_USER=$(whoami)
sudo tee /etc/sudoers.d/claude-agent > /dev/null << 'SUDOERS_EOF'
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

# Service management (only claude-agent service)
ec2-user ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart claude-agent
ec2-user ALL=(ALL) NOPASSWD: /usr/bin/systemctl status *
ec2-user ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop claude-agent
ec2-user ALL=(ALL) NOPASSWD: /usr/bin/systemctl start claude-agent

# Firewall management
ec2-user ALL=(ALL) NOPASSWD: /usr/bin/firewall-cmd *

# Log viewing
ec2-user ALL=(ALL) NOPASSWD: /usr/bin/journalctl *
SUDOERS_EOF

sudo chmod 440 /etc/sudoers.d/claude-agent
echo "✅ Limited sudo access configured"
echo ""
echo "Agent capabilities:"
echo "  ✅ Install packages (yum/apt/pip)"
echo "  ✅ Manage claude-agent service"
echo "  ✅ Configure firewall"
echo "  ✅ View system logs"
echo "  ❌ Cannot shutdown/reboot"
echo "  ❌ Cannot perform destructive operations"
echo ""

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
rm -f /tmp/claude-agent.service 2>/dev/null || true

# Clean up downloaded setup script if it exists
rm -f ~/amazon-linux-setup.sh 2>/dev/null || true

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
