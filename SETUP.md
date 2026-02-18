# Digital Twin Setup Guide

Quick start guide for setting up your Digital Twin bot with email and calendar integration.

## Prerequisites

- Python 3.8+
- Anthropic API key
- Email account (Gmail, Outlook, Yahoo, or custom IMAP/SMTP)
- Calendar access (Google Calendar, Outlook, iCloud, or custom CalDAV)

## Quick Setup (EC2 or Local)

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Install dt-setup Command (One-Time)

**Using make (recommended):**
```bash
make install
```

**Or using the install script:**
```bash
sudo ./install.sh
```

This installs `dt-setup` to `/usr/local/bin` so you can run it like `python`, `git`, `npm` - from anywhere, no `./` needed.

### 3. Run Configuration Wizard

```bash
dt-setup              # Full interactive wizard
dt-setup email        # Configure email only
dt-setup telegram     # Configure Telegram only
dt-setup core         # Configure API keys only
```

The interactive wizard will:
- Auto-detect your email provider (Gmail, Outlook, etc.)
- Auto-populate IMAP/SMTP/CalDAV settings
- Prompt for your credentials
- Update your `.env` file automatically

### 4. Start the Bot

```bash
python -m src.main
```

Check logs for successful tool registration:
```
📧 Email tool registered
📅 Calendar tool registered
```

---

**That's it!** From now on, just run `dt-setup` whenever you need to update credentials.

## Manual Setup

If you prefer to manually edit `.env`:

1. Copy the example file:
   ```bash
   cp .env.example .env
   ```

2. Edit `.env` and add your credentials:
   ```bash
   nano .env
   ```

3. Add email/calendar sections (see `.env.example` for templates)

## Configuration Options

### Simple Commands (After Installation)

```bash
dt-setup                   # Full wizard (all tools)
dt-setup digital-twin      # Full wizard (alias)
dt-setup core              # API keys only
dt-setup telegram          # Telegram only
dt-setup email             # Email only
dt-setup calendar          # Calendar only
dt-setup help              # Show help
```

Works from **any directory**, just like `git`, `python`, `npm`.

### Advanced: Python Direct Usage

If you haven't installed with `make install`, you can still use:

```bash
python configure.py --email-only              # Email only
python configure.py --calendar-only           # Calendar only
python configure.py --core-only               # API keys only
python configure.py --telegram-only           # Telegram only
python configure.py --env-file /path/to/.env  # Custom .env location
```

## Supported Providers

### Auto-Configured Providers

The wizard automatically configures these providers:

| Provider | Email Domain | IMAP/SMTP | CalDAV |
|----------|-------------|-----------|--------|
| **Gmail** | @gmail.com | ✅ | ✅ |
| **Outlook** | @outlook.com, @hotmail.com, @live.com | ✅ | ✅ |
| **Yahoo** | @yahoo.com | ✅ | ❌ |
| **iCloud** | @icloud.com | ✅ | ✅ |

### App Passwords Required

Most providers require app-specific passwords (not your regular password):

#### Gmail
1. Enable 2FA: https://myaccount.google.com/security
2. Create App Password: https://myaccount.google.com/apppasswords
3. Use 16-character app password

#### Outlook/Microsoft
1. Enable 2FA: https://account.microsoft.com/security
2. Create App Password in security settings
3. Use app password

#### iCloud
1. Go to: https://appleid.apple.com/account/manage
2. Generate app-specific password
3. Use app-specific password

## Testing Your Setup

Once configured, test the tools:

### Email Commands
- "Check my emails"
- "Read my unread messages"
- "Send email to john@example.com with subject 'Meeting' and body 'See you at 2pm'"
- "Reply to email [email_id] with 'Thanks!'"

### Calendar Commands
- "What's on my calendar today?"
- "Show me this week's appointments"
- "Create appointment for tomorrow at 2pm titled 'Team meeting'"
- "List my events for the next 7 days"

## Troubleshooting

### Tools Not Appearing

**Check logs:**
```bash
tail -f logs/agent.log
```

**Look for:**
- `📧 Email tool registered` (email working)
- `📅 Calendar tool registered` (calendar working)
- `Email tool not registered (missing credentials in .env)` (missing config)

### Authentication Failures

**Common issues:**
1. Using regular password instead of app password ❌
2. 2FA not enabled
3. Wrong IMAP/SMTP server
4. Incorrect CalDAV URL format

**Solutions:**
1. Generate app-specific password
2. Enable 2FA on your account
3. Run `python configure.py` to auto-detect correct servers
4. Check `.env.example` for correct URL formats

### Gmail CalDAV URL

Make sure to replace `YOUR_EMAIL` with your actual email:
```bash
# ❌ Wrong
CALDAV_URL=https://apidata.googleusercontent.com/caldav/v2/YOUR_EMAIL@gmail.com/events

# ✅ Correct
CALDAV_URL=https://apidata.googleusercontent.com/caldav/v2/john.doe@gmail.com/events
```

### Permission Errors

If running on EC2, make sure `.env` has correct permissions:
```bash
chmod 600 .env  # Only owner can read/write
```

## Security Notes

- **Never commit `.env` to git** (already in `.gitignore`)
- Use app-specific passwords, never your main account password
- `.env` is protected by Layer 14 security (bot cannot modify it)
- Store API keys and passwords securely
- On EC2: Use IAM roles when possible instead of hardcoded credentials

## Need Help?

1. Check logs: `tail -f logs/agent.log`
2. Review `.env.example` for configuration templates
3. Run configuration wizard: `python configure.py`
4. Verify credentials at provider websites
