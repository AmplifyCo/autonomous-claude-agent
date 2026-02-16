# Quick Deploy Guide - EC2 Amazon Linux

This guide shows the **simplified deployment process** with automated Cloudflare Tunnel and Telegram webhook setup.

## Prerequisites

1. **EC2 Instance** (t3.small or larger)
   - Amazon Linux 2023
   - 2GB RAM minimum (1.7GB usable)
   - 20GB disk minimum (40GB recommended)
   - Port 18789 open in Security Group (optional, for direct access)

2. **Your Credentials** (have these ready)
   - Anthropic API Key ([Get it here](https://console.anthropic.com/))
   - Telegram Bot Token ([Get from @BotFather](https://t.me/BotFather))
   - Telegram Chat ID ([Get from @userinfobot](https://t.me/userinfobot))
   - Cloudflare Domain (if using Telegram webhooks)

---

## One-Line Deploy (5 Minutes)

SSH into your EC2 instance and run:

```bash
curl -fsSL https://raw.githubusercontent.com/AmplifyCo/autonomous-claude-agent/main/deploy/ec2/amazon-linux-setup.sh | bash
```

### What This Does Automatically:

1. ✅ **System checks** (RAM, disk, CPU)
2. ✅ **Install dependencies** (Python 3.11, browser tools)
3. ✅ **Clone repository**
4. ✅ **Set up virtual environment**
5. ✅ **Configure limited sudo access**
6. ✅ **Create .env file** with placeholders
7. ✅ **Set up Cloudflare Tunnel** (optional, prompted)
   - Installs cloudflared
   - Authenticates with Cloudflare (one-time)
   - Creates named tunnel
   - Sets up DNS route with YOUR domain
   - Configures webhook endpoint
8. ✅ **Install systemd service**
9. ✅ **Configure auto-updates**

**Total time: ~5 minutes** (vs. 30+ minutes manual setup)

---

## During Installation

### Step 1: Cloudflare Tunnel Setup (Optional)

When prompted:
```
Do you want to set up Cloudflare Tunnel? (y/n):
```

- Type **`y`** if you have a Cloudflare domain and want Telegram webhooks
- Type **`n`** if you want to skip (you can run `deploy/cloudflare/setup-tunnel.sh` later)

### Step 2: Cloudflare Authentication

If you chose `y`, the script will prompt:
```
Press Enter to start authentication...
```

This opens your browser. You'll:
1. Login to Cloudflare
2. Authorize the tunnel
3. Return to the terminal

### Step 3: Domain Configuration

Enter your domain details:
```
Enter your domain name (e.g., amplify-pixels.com): your-domain.com
Enter subdomain for webhook (e.g., webhook): webhook
```

Example:
- Domain: `amplify-pixels.com`
- Subdomain: `webhook`
- Result: `https://webhook.amplify-pixels.com`

The script will automatically:
- Create DNS CNAME record
- Configure the tunnel
- Set up Telegram webhook

---

## After Installation

### 1. Configure .env File

```bash
cd ~/autonomous-claude-agent
nano .env
```

**Required:**
```bash
ANTHROPIC_API_KEY=sk-ant-xxxxx
TELEGRAM_BOT_TOKEN=123456:ABCdef...
TELEGRAM_CHAT_ID=987654321
```

### 2. Start the Agent

```bash
sudo systemctl start claude-agent
```

### 3. Verify Everything Works

```bash
# Check agent status
sudo systemctl status claude-agent

# Check Cloudflare Tunnel status (if configured)
sudo systemctl status cloudflared

# View agent logs
sudo journalctl -u claude-agent -f

# Test Telegram bot
# Send "What's your status?" to your bot on Telegram
```

---

## Permanent URLs

### With Cloudflare Tunnel:
- **Dashboard**: `https://webhook.your-domain.com`
- **Telegram Webhook**: `https://webhook.your-domain.com/telegram/webhook`
- **Survives restarts**: Yes, permanent
- **HTTPS**: Yes, automatic

### Without Cloudflare Tunnel:
- **Dashboard**: `http://EC2-PUBLIC-IP:18789`
- **Telegram Webhook**: Won't work (Telegram requires HTTPS)
- **Survives restarts**: No, IP may change
- **HTTPS**: No

**Recommendation**: Use Cloudflare Tunnel for production.

---

## Telegram Commands

Once configured, you can chat with your agent via Telegram:

**Natural Language:**
- "What's your status?"
- "Check for git updates"
- "Pull latest from git"
- "Show system health"
- "What's happening?"

**The agent will:**
- Parse your intent using Claude API
- Execute the action
- Respond instantly via webhook

---

## Manual Setup (If Needed Later)

If you skipped Cloudflare Tunnel during installation:

```bash
cd ~/autonomous-claude-agent
bash deploy/cloudflare/setup-tunnel.sh
```

This runs the same automated setup for just the tunnel.

---

## What's Different from Before?

### Before (Manual Setup):
1. ❌ Install cloudflared manually
2. ❌ Authenticate with Cloudflare manually
3. ❌ Create tunnel manually
4. ❌ Configure config.yml manually
5. ❌ Set up systemd service manually
6. ❌ Create DNS route manually
7. ❌ Test URL manually
8. ❌ Set Telegram webhook manually
9. ❌ Debug issues manually
10. ❌ **Total time: 30-45 minutes**

### Now (Automated):
1. ✅ Run one command
2. ✅ Answer 3 prompts (y/n, domain, subdomain)
3. ✅ Edit .env file
4. ✅ Start agent
5. ✅ **Total time: 5 minutes**

---

## Troubleshooting

### Tunnel Status Shows "Down"

```bash
sudo systemctl status cloudflared
sudo journalctl -u cloudflared -n 50
```

Common fix:
```bash
sudo systemctl restart cloudflared
```

### Webhook Not Working

Check webhook status:
```bash
TELEGRAM_BOT_TOKEN="your_token_here"
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getWebhookInfo" | python3 -m json.tool
```

Manually set webhook:
```bash
TUNNEL_URL="https://webhook.your-domain.com"
curl -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -H "Content-Type: application/json" \
  -d "{\"url\": \"$TUNNEL_URL/telegram/webhook\"}"
```

### Agent Not Responding

```bash
# Check agent logs
sudo journalctl -u claude-agent -n 100

# Restart agent
sudo systemctl restart claude-agent
```

---

## Next Steps

After successful deployment:

1. **Test Telegram chat** - Send "What's your status?"
2. **Access dashboard** - Visit your permanent URL
3. **Monitor logs** - `sudo journalctl -u claude-agent -f`
4. **Read the main guide** - `README.md` for advanced features

---

## Cost Optimization

- **EC2**: t3.small ($0.0208/hour ≈ $15/month)
- **Cloudflare Tunnel**: Free
- **Data transfer**: Free tier covers most usage
- **Anthropic API**: Pay-per-use (Sonnet ~$3/M tokens)

**Total monthly cost: ~$15-30** depending on API usage.

---

## Support

- **Issues**: https://github.com/AmplifyCo/autonomous-claude-agent/issues
- **Logs**: `~/autonomous-claude-agent/data/logs/`
- **Config**: `~/autonomous-claude-agent/.env`

---

**That's it! Your autonomous agent is now running 24/7 with instant Telegram webhooks.** 🚀
