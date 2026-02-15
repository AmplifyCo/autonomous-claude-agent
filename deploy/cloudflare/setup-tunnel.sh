#!/bin/bash
set -e

echo "🌐 Setting up Cloudflare Tunnel for Telegram Webhook..."
echo ""

# Check if running on EC2
if [ ! -f /sys/hypervisor/uuid ] || ! grep -q ec2 /sys/hypervisor/uuid 2>/dev/null; then
    echo "⚠️  WARNING: This script is designed for EC2 instances"
    echo "   Continuing anyway..."
    echo ""
fi

# Variables
TUNNEL_NAME="claude-agent"
SERVICE_URL="http://localhost:18789"
CLOUDFLARED_DIR="$HOME/.cloudflared"

# ============================================
# Step 1: Install cloudflared
# ============================================
echo "📦 Installing cloudflared..."

if command -v cloudflared &> /dev/null; then
    echo "✅ cloudflared already installed: $(cloudflared --version)"
else
    # Download and install
    curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /tmp/cloudflared
    chmod +x /tmp/cloudflared
    sudo mv /tmp/cloudflared /usr/local/bin/cloudflared

    echo "✅ cloudflared installed: $(cloudflared --version)"
fi

echo ""

# ============================================
# Step 2: Login to Cloudflare
# ============================================
echo "🔐 Cloudflare Authentication"
echo ""

if [ ! -f "$CLOUDFLARED_DIR/cert.pem" ]; then
    echo "⚠️  You need to authenticate with Cloudflare (one-time setup)"
    echo ""
    echo "This will open a browser window. Steps:"
    echo "1. Click the link below (or copy to browser)"
    echo "2. Login with Google/GitHub or create free Cloudflare account"
    echo "3. Authorize the tunnel"
    echo "4. Come back here when done"
    echo ""

    # Start login process
    cloudflared tunnel login

    echo ""
    echo "✅ Authentication complete!"
else
    echo "✅ Already authenticated with Cloudflare"
fi

echo ""

# ============================================
# Step 3: Create Tunnel
# ============================================
echo "🚇 Creating Cloudflare Tunnel..."

# Check if tunnel already exists
if cloudflared tunnel list 2>/dev/null | grep -q "$TUNNEL_NAME"; then
    echo "✅ Tunnel '$TUNNEL_NAME' already exists"
    TUNNEL_ID=$(cloudflared tunnel list | grep "$TUNNEL_NAME" | awk '{print $1}')
else
    # Create new tunnel
    TUNNEL_ID=$(cloudflared tunnel create "$TUNNEL_NAME" | grep -oP 'Created tunnel .* with id \K[a-f0-9-]+')
    echo "✅ Created tunnel: $TUNNEL_ID"
fi

echo "   Tunnel ID: $TUNNEL_ID"
echo ""

# ============================================
# Step 4: Configure Tunnel
# ============================================
echo "⚙️  Configuring tunnel..."

# Create config directory
mkdir -p "$CLOUDFLARED_DIR"

# Create tunnel config
cat > "$CLOUDFLARED_DIR/config.yml" << EOF
tunnel: $TUNNEL_ID
credentials-file: $CLOUDFLARED_DIR/$TUNNEL_ID.json

# Ingress rules
ingress:
  # Route all traffic to local dashboard
  - service: $SERVICE_URL
    originRequest:
      noTLSVerify: true
EOF

echo "✅ Tunnel configuration created"
echo ""

# ============================================
# Step 5: Get Tunnel URL
# ============================================
echo "🔗 Getting tunnel URL..."

# Start tunnel in background temporarily to get URL
cloudflared tunnel --config "$CLOUDFLARED_DIR/config.yml" run "$TUNNEL_NAME" &
TEMP_PID=$!

# Wait for tunnel to start and get URL
sleep 5

# Get the tunnel URL from Cloudflare API
TUNNEL_URL=$(cloudflared tunnel info "$TUNNEL_ID" 2>/dev/null | grep -oP 'https://[^/]+' | head -1)

# If API method doesn't work, construct URL from tunnel ID
if [ -z "$TUNNEL_URL" ]; then
    TUNNEL_URL="https://$TUNNEL_ID.cfargotunnel.com"
fi

echo "✅ Tunnel URL: $TUNNEL_URL"
echo ""

# Stop temporary tunnel
kill $TEMP_PID 2>/dev/null || true
sleep 2

# ============================================
# Step 6: Install as systemd service
# ============================================
echo "🔧 Installing as systemd service..."

# Create systemd service file
sudo tee /etc/systemd/system/cloudflared.service > /dev/null << EOF
[Unit]
Description=Cloudflare Tunnel
After=network.target

[Service]
Type=simple
User=$USER
ExecStart=/usr/local/bin/cloudflared tunnel --config $CLOUDFLARED_DIR/config.yml run $TUNNEL_NAME
Restart=always
RestartSec=10
StandardOutput=append:/home/$USER/autonomous-claude-agent/data/logs/cloudflared.log
StandardError=append:/home/$USER/autonomous-claude-agent/data/logs/cloudflared.log

[Install]
WantedBy=multi-user.target
EOF

# Reload systemd
sudo systemctl daemon-reload

# Enable and start service
sudo systemctl enable cloudflared
sudo systemctl start cloudflared

echo "✅ Cloudflare Tunnel service installed and started"
echo ""

# ============================================
# Step 7: Save tunnel URL for agent
# ============================================
echo "💾 Saving tunnel URL..."

# Create tunnel info file for agent to read
mkdir -p ~/autonomous-claude-agent/data
cat > ~/autonomous-claude-agent/data/cloudflare_tunnel.json << EOF
{
  "tunnel_id": "$TUNNEL_ID",
  "tunnel_name": "$TUNNEL_NAME",
  "tunnel_url": "$TUNNEL_URL",
  "webhook_url": "$TUNNEL_URL/telegram/webhook",
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

echo "✅ Tunnel info saved to data/cloudflare_tunnel.json"
echo ""

# ============================================
# Step 8: Setup Telegram Webhook
# ============================================
echo "📱 Setting up Telegram webhook..."
echo ""

# Check if .env exists and has Telegram token
if [ -f ~/autonomous-claude-agent/.env ]; then
    TELEGRAM_BOT_TOKEN=$(grep TELEGRAM_BOT_TOKEN ~/autonomous-claude-agent/.env | cut -d '=' -f2 | tr -d ' "'"'"'')

    if [ ! -z "$TELEGRAM_BOT_TOKEN" ] && [ "$TELEGRAM_BOT_TOKEN" != "your_telegram_bot_token_here" ]; then
        echo "Setting up webhook with Telegram..."

        # Set webhook
        WEBHOOK_URL="$TUNNEL_URL/telegram/webhook"
        RESPONSE=$(curl -s -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
            -H "Content-Type: application/json" \
            -d "{\"url\": \"$WEBHOOK_URL\"}")

        if echo "$RESPONSE" | grep -q '"ok":true'; then
            echo "✅ Telegram webhook configured successfully!"
            echo "   Webhook URL: $WEBHOOK_URL"
        else
            echo "❌ Failed to set webhook:"
            echo "$RESPONSE" | jq '.' 2>/dev/null || echo "$RESPONSE"
        fi
    else
        echo "⚠️  Telegram bot token not configured in .env"
        echo "   Configure it and run: curl -X POST \"https://api.telegram.org/botYOUR_TOKEN/setWebhook\" -d \"url=$TUNNEL_URL/telegram/webhook\""
    fi
else
    echo "⚠️  .env file not found"
fi

echo ""

# ============================================
# Step 9: Test Setup
# ============================================
echo "🧪 Testing setup..."

# Check if tunnel is running
sleep 2
if sudo systemctl is-active --quiet cloudflared; then
    echo "✅ Cloudflare Tunnel service is running"
else
    echo "❌ Cloudflare Tunnel service is not running"
    echo "   Check logs: sudo journalctl -u cloudflared -n 50"
fi

# Test if tunnel is accessible
if curl -s -o /dev/null -w "%{http_code}" "$TUNNEL_URL" | grep -q "200\|404\|500"; then
    echo "✅ Tunnel is accessible from internet"
else
    echo "⚠️  Tunnel may not be accessible yet (DNS propagation takes 30-60s)"
fi

echo ""

# ============================================
# Summary
# ============================================
echo "✅ Setup complete!"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Cloudflare Tunnel Information:"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Tunnel Name: $TUNNEL_NAME"
echo "  Tunnel ID: $TUNNEL_ID"
echo "  Public URL: $TUNNEL_URL"
echo "  Webhook URL: $TUNNEL_URL/telegram/webhook"
echo ""
echo "This URL is PERMANENT and survives restarts!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Service Management:"
echo "  Status:  sudo systemctl status cloudflared"
echo "  Logs:    sudo journalctl -u cloudflared -f"
echo "  Restart: sudo systemctl restart cloudflared"
echo "  Stop:    sudo systemctl stop cloudflared"
echo ""
echo "Next Steps:"
echo "1. Access dashboard: $TUNNEL_URL"
echo "2. Test Telegram: Send a message to your bot"
echo "3. Restart agent to use tunnel:"
echo "   sudo systemctl restart claude-agent"
echo ""
