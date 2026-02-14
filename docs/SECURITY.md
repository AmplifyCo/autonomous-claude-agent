# Security Model - Limited Elevated Access

## Overview

The autonomous agent runs with **limited sudo access** on EC2, following the principle of least privilege. This allows the agent to install dependencies and manage its own service while preventing destructive operations.

## Allowed Sudo Commands

The agent can execute these specific sudo commands:

### Package Management
```bash
sudo yum install <package>
sudo yum update <package>
sudo yum remove <package>
sudo apt-get install <package>
sudo apt-get update
sudo pip install <package>
```

### Service Management (claude-agent only)
```bash
sudo systemctl start claude-agent
sudo systemctl stop claude-agent
sudo systemctl restart claude-agent
sudo systemctl status <any-service>  # Read-only
```

### Firewall Management
```bash
sudo firewall-cmd <args>
```

### Log Viewing
```bash
sudo journalctl <args>
```

## Blocked Commands

These commands are explicitly blocked for safety:

```bash
# Destructive file operations
sudo rm -rf /
sudo dd

# System power management
sudo shutdown
sudo reboot
sudo poweroff
sudo halt

# Filesystem formatting
format
mkfs
```

## Configuration Files

### `/etc/sudoers.d/claude-agent`
Created automatically during EC2 setup. Defines exactly which sudo commands the `ec2-user` can run without password.

### `config/agent.yaml`
```yaml
safety:
  allow_sudo: true  # Enable limited sudo
  allowed_sudo_commands:
    - sudo yum install
    - sudo systemctl restart claude-agent
    # ... etc
  blocked_commands:
    - sudo rm
    - sudo shutdown
    # ... etc
```

## How It Works

1. **BashTool Security Check:**
   - All bash commands go through `BashTool.execute()`
   - First checks against `blocked_commands` (always denied)
   - If command starts with `sudo`:
     - Checks if `allow_sudo=true` in config
     - Checks if command matches `allowed_sudo_commands` patterns
     - Only allows if both conditions met

2. **Sudoers File:**
   - System-level enforcement via `/etc/sudoers.d/claude-agent`
   - Even if BashTool check is bypassed, OS blocks unauthorized sudo

3. **Defense in Depth:**
   - Application layer: BashTool validation
   - OS layer: sudoers file restriction
   - User layer: Agent runs as non-root `ec2-user`

## Use Cases

### ✅ Allowed: Install Dependencies
```python
# Agent can install packages needed for features it builds
await bash_tool.execute("sudo yum install redis -y")
await bash_tool.execute("sudo pip install anthropic")
```

### ✅ Allowed: Restart Own Service
```python
# Agent can restart itself after code changes
await bash_tool.execute("sudo systemctl restart claude-agent")
```

### ✅ Allowed: View Logs
```python
# Agent can check its own logs for debugging
await bash_tool.execute("sudo journalctl -u claude-agent --since '1 hour ago'")
```

### ❌ Blocked: Destructive Operations
```python
# These will be rejected by BashTool and sudoers
await bash_tool.execute("sudo rm -rf /var")  # BLOCKED
await bash_tool.execute("sudo shutdown -h now")  # BLOCKED
await bash_tool.execute("sudo reboot")  # BLOCKED
```

## Browser Capabilities

With limited sudo, the agent can:

1. **Install browser dependencies:**
   ```bash
   sudo yum install chromium w3m
   ```

2. **Use text-based browsing (w3m):**
   ```python
   result = await browser_tool.execute(
       url="https://docs.anthropic.com",
       mode="text"
   )
   ```

3. **Use headless Chromium (Selenium):**
   ```python
   result = await browser_tool.execute(
       url="https://example.com",
       mode="full",
       javascript=True
   )
   ```

## Security Recommendations

1. **Review sudoers file after deployment:**
   ```bash
   sudo cat /etc/sudoers.d/claude-agent
   ```

2. **Monitor sudo usage:**
   ```bash
   sudo journalctl | grep sudo
   ```

3. **Audit agent logs regularly:**
   ```bash
   sudo journalctl -u claude-agent -f
   ```

4. **Restrict network access (AWS Security Group):**
   - Only allow necessary inbound ports (18789 for dashboard)
   - Consider VPN for production

5. **Rotate API keys periodically:**
   - Update `ANTHROPIC_API_KEY` in `.env`
   - Restart agent: `sudo systemctl restart claude-agent`

## Disabling Sudo (Most Restrictive)

If you want to run without any sudo access:

1. **Edit `config/agent.yaml`:**
   ```yaml
   safety:
     allow_sudo: false  # Disable all sudo
   ```

2. **Remove sudoers file:**
   ```bash
   sudo rm /etc/sudoers.d/claude-agent
   ```

3. **Pre-install all dependencies:**
   - Install everything needed during initial setup
   - Agent cannot install new packages at runtime

## Granting More Access (Not Recommended)

To allow additional sudo commands (use with caution):

1. **Edit `config/agent.yaml`:**
   ```yaml
   safety:
     allowed_sudo_commands:
       - sudo yum install
       - sudo your-new-command  # Add here
   ```

2. **Update sudoers file:**
   ```bash
   sudo nano /etc/sudoers.d/claude-agent
   # Add line:
   ec2-user ALL=(ALL) NOPASSWD: /usr/bin/your-new-command *
   ```

⚠️ **Warning:** Each additional sudo permission increases security risk. Only grant what's absolutely necessary.

## Incident Response

If you suspect unauthorized sudo usage:

1. **Check sudo logs:**
   ```bash
   sudo journalctl | grep -i sudo | tail -100
   ```

2. **Review agent logs:**
   ```bash
   tail -100 data/logs/agent.log
   ```

3. **Disable agent immediately:**
   ```bash
   sudo systemctl stop claude-agent
   ```

4. **Revoke sudo access:**
   ```bash
   sudo rm /etc/sudoers.d/claude-agent
   ```

5. **Investigate and patch before restarting**
