#!/usr/bin/env python3
"""
Nova Setup Wizard
-----------------
Interactive CLI setup for new users.

  1. Pick an LLM provider and enter your API key
  2. Test the connection — Nova speaks to you right here in the terminal
  3. Set up Telegram (required)
  4. Nova's setup persona guides you through optional features:
     email, WhatsApp/Twilio, X, calendar, voice

Run:  python setup.py
"""

import os
import sys
from pathlib import Path
from getpass import getpass


# ── Terminal colors (no dependencies needed) ─────────────────────────
class C:
    RST     = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    CYAN    = "\033[96m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    RED     = "\033[91m"
    BLUE    = "\033[94m"


def _print_banner():
    print(f"""
{C.CYAN}{C.BOLD}  ███╗   ██╗ ██████╗ ██╗   ██╗  █████╗
  ████╗  ██║██╔═══██╗██║   ██║██╔══██╗
  ██╔██╗ ██║██║   ██║██║   ██║███████║
  ██║╚██╗██║██║   ██║╚██╗ ██╔╝██╔══██║
  ██║ ╚████║╚██████╔╝ ╚████╔╝ ██║  ██║
  ╚═╝  ╚═══╝ ╚═════╝   ╚═══╝  ╚═╝  ╚═╝{C.RST}
  {C.BOLD}the AutoBot{C.RST}  ·  Setup Wizard
""")


def _section(title):
    print(f"\n{C.BOLD}{C.BLUE}{'─' * 52}{C.RST}")
    print(f"{C.BOLD}  {title}{C.RST}")
    print(f"{C.BOLD}{C.BLUE}{'─' * 52}{C.RST}\n")


def _ok(msg):   print(f"  {C.GREEN}✓{C.RST}  {msg}")
def _info(msg): print(f"  {C.DIM}{msg}{C.RST}")
def _warn(msg): print(f"  {C.YELLOW}⚠{C.RST}  {msg}")
def _err(msg):  print(f"  {C.RED}✗{C.RST}  {msg}")


def _ask(prompt, secret=False, default=None, allow_empty=False):
    """Prompt for user input; optionally hide the value (API keys)."""
    suffix = f" {C.DIM}[{default}]{C.RST}" if default else ""
    full = f"  {C.CYAN}›{C.RST} {prompt}{suffix}: "
    while True:
        try:
            val = (getpass(full) if secret else input(full)).strip()
        except (KeyboardInterrupt, EOFError):
            print()
            sys.exit(0)
        if not val and default:
            return default
        if not val and allow_empty:
            return ""
        if val:
            return val
        _warn("This field is required — please enter a value.")


# ── Provider catalogue ───────────────────────────────────────────────
PROVIDERS = [
    {
        "name":         "SmolLM2  (local, no API key needed — via Ollama)",
        "env_key":      None,                   # no key required
        "litellm_id":   "ollama/smollm2",
        "get_key_url":  "https://ollama.com/library/smollm2",
        "key_hint":     None,
        "api_base":     "http://localhost:11434",
        "local":        True,
        "prereq":       "Ollama must be running: ollama serve  (then: ollama pull smollm2)",
    },
    {
        "name":         "Anthropic (Claude)",
        "env_key":      "ANTHROPIC_API_KEY",
        "litellm_id":   "anthropic/claude-haiku-4-5-20251001",
        "get_key_url":  "https://console.anthropic.com/",
        "key_hint":     "Starts with sk-ant-...",
        "local":        False,
    },
    {
        "name":         "Google (Gemini)",
        "env_key":      "GEMINI_API_KEY",
        "litellm_id":   "gemini/gemini-2.0-flash",
        "get_key_url":  "https://aistudio.google.com/app/apikey",
        "key_hint":     "Starts with AIza...",
        "local":        False,
    },
    {
        "name":         "OpenAI",
        "env_key":      "OPENAI_API_KEY",
        "litellm_id":   "openai/gpt-4o-mini",
        "get_key_url":  "https://platform.openai.com/api-keys",
        "key_hint":     "Starts with sk-...",
        "local":        False,
    },
    {
        "name":         "Other  (any OpenAI-compatible endpoint)",
        "env_key":      "OPENAI_API_KEY",
        "litellm_id":   None,                   # filled in during setup
        "get_key_url":  None,
        "key_hint":     "Your API key (or any string if the endpoint needs none)",
        "local":        False,
    },
]


# ── Feature catalogue ────────────────────────────────────────────────
FEATURES = {
    "email": {
        "title":   "Email (Gmail / Outlook / Yahoo)",
        "tagline": "Nova reads, writes, and replies to emails on your behalf.",
        "fields": [
            {"key": "EMAIL_ADDRESS",    "label": "Email address",
             "hint": "your@gmail.com", "secret": False},
            {"key": "EMAIL_PASSWORD",   "label": "App password (NOT your login password)",
             "hint": "Gmail: myaccount.google.com/apppasswords  (2FA must be on)", "secret": True},
            {"key": "EMAIL_IMAP_SERVER","label": "IMAP server",
             "hint": "Gmail→imap.gmail.com  Outlook→outlook.office365.com  Yahoo→imap.mail.yahoo.com", "secret": False},
            {"key": "EMAIL_SMTP_SERVER","label": "SMTP server",
             "hint": "Gmail→smtp.gmail.com  Outlook→smtp.office365.com  Yahoo→smtp.mail.yahoo.com", "secret": False},
        ],
    },
    "twilio": {
        "title":   "Twilio  (WhatsApp · SMS · Voice calls)",
        "tagline": "Nova can message you on WhatsApp, send SMS, and make or receive calls.",
        "fields": [
            {"key": "TWILIO_ACCOUNT_SID",      "label": "Account SID",
             "hint": "console.twilio.com → Account Info panel", "secret": False},
            {"key": "TWILIO_AUTH_TOKEN",        "label": "Auth Token",
             "hint": "Same page — click the eye icon to reveal", "secret": True},
            {"key": "TWILIO_PHONE_NUMBER",      "label": "Twilio phone number  (+E.164 format)",
             "hint": "e.g. +15551234567", "secret": False},
            {"key": "TWILIO_WHATSAPP_NUMBER",   "label": "WhatsApp sandbox number  (optional, press Enter to skip)",
             "hint": "Twilio Sandbox default: whatsapp:+14155238886", "secret": False},
        ],
    },
    "x": {
        "title":   "X / Twitter",
        "tagline": "Nova can search X, post tweets, and read community posts.",
        "fields": [
            {"key": "X_API_KEY",            "label": "API Key (Consumer Key)",
             "hint": "developer.x.com → Your App → Keys and Tokens", "secret": True},
            {"key": "X_API_SECRET",         "label": "API Secret (Consumer Secret)",
             "hint": "Same page as above", "secret": True},
            {"key": "X_ACCESS_TOKEN",       "label": "Access Token",
             "hint": "Same page → Authentication Tokens section", "secret": True},
            {"key": "X_ACCESS_TOKEN_SECRET","label": "Access Token Secret",
             "hint": "Same page as above", "secret": True},
        ],
    },
    "calendar": {
        "title":   "Calendar  (Google / Outlook / iCloud)",
        "tagline": "Nova can check your schedule and create or update events.",
        "fields": [
            {"key": "CALDAV_URL",      "label": "CalDAV URL",
             "hint": "Google: https://apidata.googleusercontent.com/caldav/v2/YOUR_EMAIL/events", "secret": False},
            {"key": "CALDAV_USERNAME", "label": "Username (your email)",
             "hint": "your@gmail.com", "secret": False},
            {"key": "CALDAV_PASSWORD", "label": "App password",
             "hint": "Same Gmail app password used for email (if Google)", "secret": True},
        ],
    },
    "elevenlabs": {
        "title":   "ElevenLabs  (Natural voice for calls)",
        "tagline": "Nova speaks with a realistic voice when making or receiving phone calls.",
        "fields": [
            {"key": "ELEVENLABS_API_KEY", "label": "ElevenLabs API key",
             "hint": "elevenlabs.io → Profile → API Key", "secret": True},
            {"key": "ELEVENLABS_VOICE_ID","label": "Voice ID  (press Enter for default)",
             "hint": "elevenlabs.io → Voices → copy any voice ID you like", "secret": False},
        ],
    },
}


# ── Setup assistant system prompt ────────────────────────────────────
_SYSTEM_PROMPT = """\
You are Nova's friendly setup assistant running in a user's terminal. \
Your job is to guide them through configuring optional features for their Nova AI assistant.

Nova is a self-hosted personal AI. It connects via Telegram and can:
• Send/read email       (feature key: email)
• WhatsApp/SMS/calls    (feature key: twilio)
• Post to X / search X (feature key: x)
• Manage calendar       (feature key: calendar)
• Speak with a voice    (feature key: elevenlabs)

Conversation style:
- Warm and concise. 2-3 sentences per turn unless giving step-by-step instructions.
- Ask one question at a time. Don't repeat yourself.
- Recommend features based on what the user tells you they want to do.

Signal protocol (Python parses these — include exactly as shown):
- When the user agrees to set up a feature: end your message with [SETUP:feature_key]
  (feature_key is one of: email, twilio, x, calendar, elevenlabs)
- When setup is complete and user wants to finish: end with [DONE]
- Otherwise: no signal needed.

You will be told which features are already configured — do not suggest those again.
Do NOT ask the user to type credentials — the script handles that securely."""


# ── LLM helpers ──────────────────────────────────────────────────────

def _test_connection(provider, api_key, api_base=None):
    """Ping the LLM with a minimal call. Returns (success, message)."""
    try:
        import litellm
        litellm.suppress_debug_info = True

        if provider.get("env_key") and api_key and api_key != "local":
            os.environ[provider["env_key"]] = api_key
        if api_base:
            os.environ["OPENAI_API_BASE"] = api_base

        resp = litellm.completion(
            model=provider["litellm_id"],
            messages=[{"role": "user", "content": "Reply with one word: Ready"}],
            max_tokens=5,
            timeout=20,
            **({"api_base": api_base} if api_base else {}),
        )
        return True, resp.choices[0].message.content.strip()
    except Exception as exc:
        return False, str(exc)


def _chat(provider, api_key, history, configured, owner_name, api_base=None):
    """Send the conversation history to the LLM and return its reply."""
    try:
        import litellm
        litellm.suppress_debug_info = True
        if provider.get("env_key") and api_key and api_key != "local":
            os.environ[provider["env_key"]] = api_key

        system = _SYSTEM_PROMPT
        if configured:
            system += f"\n\nAlready configured: {', '.join(configured)}. Do not suggest these again."
        if owner_name and owner_name != "User":
            system += f"\n\nThe user's name is {owner_name}."

        messages = [{"role": "system", "content": system}] + history

        resp = litellm.completion(
            model=provider["litellm_id"],
            messages=messages,
            max_tokens=400,
            timeout=30,
            **({"api_base": api_base} if api_base else {}),
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        return f"(I had trouble reaching the API: {exc}. Type a feature name or 'done' to finish.)"


# ── Collect credentials for one feature ─────────────────────────────

def _collect_feature(key, env_data):
    feat = FEATURES[key]
    _section(f"Setting up: {feat['title']}")
    print(f"  {feat['tagline']}\n")
    for field in feat["fields"]:
        _info(field["hint"])
        val = _ask(field["label"], secret=field["secret"], allow_empty=True)
        if val:
            env_data[field["key"]] = val
    _ok(f"{feat['title']} configured.")


# ── .env writer ──────────────────────────────────────────────────────

def _write_env(env_data):
    """Merge env_data into .env, preserving existing values and comments."""
    env_path = Path(".env")

    # Load current .env values
    existing = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k, _, v = stripped.partition("=")
                existing[k.strip()] = v.strip()

    existing.update({k: v for k, v in env_data.items() if v})

    # Rebuild from .env.example template (preserves structure + comments)
    template = Path(".env.example")
    if template.exists():
        out_lines = []
        seen = set()
        for line in template.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k, _, _ = stripped.partition("=")
                k = k.strip()
                seen.add(k)
                out_lines.append(f"{k}={existing[k]}" if existing.get(k) else line)
            else:
                out_lines.append(line)
        extras = {k: v for k, v in existing.items() if k not in seen}
        if extras:
            out_lines.append("\n# Added by setup wizard")
            for k, v in extras.items():
                out_lines.append(f"{k}={v}")
        env_path.write_text("\n".join(out_lines) + "\n")
    else:
        lines = [f"{k}={v}" for k, v in existing.items()]
        env_path.write_text("\n".join(lines) + "\n")

    _ok(".env saved.")


# ── Data directories ─────────────────────────────────────────────────

def _create_dirs():
    for d in ["data/lancedb", "data/core_brain", "data/digital_clone_brain",
              "data/memory", "data/logs", "credentials"]:
        Path(d).mkdir(parents=True, exist_ok=True)
    _ok("Data directories ready.")


# ── Main wizard ──────────────────────────────────────────────────────

def main():
    _print_banner()

    env_data = {}
    configured = []   # features set up this session

    # Check for existing .env
    if Path(".env").exists():
        _warn(".env already exists.")
        choice = _ask("Add more features to it? (yes/no)", default="yes")
        if choice.lower().startswith("n"):
            sys.exit(0)
        print()

    # ── Owner name ────────────────────────────────────────────────────
    _section("Welcome")
    print("  This wizard takes about 5 minutes.\n")
    owner_name = _ask("What's your first name?  (Nova will use this)", default="User")
    env_data["OWNER_NAME"] = owner_name
    env_data["BOT_NAME"] = "Nova"

    # ── LLM provider ─────────────────────────────────────────────────
    _section("Step 1 — Choose your AI provider")
    print("  Nova works with any of these providers.\n"
          "  Pick whichever you already have a key for.\n")

    for i, p in enumerate(PROVIDERS, 1):
        tag = f"  {C.GREEN}(no API key required){C.RST}" if p.get("local") else ""
        print(f"  {C.BOLD}{i}.{C.RST} {p['name']}{tag}")
        if p.get("get_key_url"):
            print(f"     {C.DIM}{p['get_key_url']}{C.RST}")
    print()

    n_providers = len(PROVIDERS)
    while True:
        raw = _ask(f"Enter number (1–{n_providers})", default="2")
        try:
            idx = int(raw) - 1
            if 0 <= idx < n_providers:
                provider = dict(PROVIDERS[idx])   # copy so we can mutate
                break
        except ValueError:
            pass
        _warn(f"Please enter a number between 1 and {n_providers}.")

    print(f"\n  {C.BOLD}{provider['name']}{C.RST} selected.\n")

    # ── Local model (SmolLM2 / Ollama) — no API key needed ──────────
    api_base = provider.get("api_base")
    api_key  = "local"   # placeholder for local; litellm ignores it

    if provider.get("local"):
        if "prereq" in provider:
            _info(provider["prereq"])
            print()

    # ── Custom OpenAI-compatible endpoint ────────────────────────────
    elif provider["litellm_id"] is None:
        api_base = _ask("Base URL of your endpoint (e.g. http://localhost:11434/v1)")
        model_id = _ask("Model name (e.g. llama3.2, mistral, smollm2)")
        provider["litellm_id"] = f"ollama/{model_id}" if "11434" in api_base else model_id
        env_data["OPENAI_API_BASE"] = api_base

    # ── Cloud provider — collect and test API key ────────────────────
    if not provider.get("local") and provider["env_key"]:
        _info(provider["key_hint"])
        api_key = _ask(f"{provider['name']} API key", secret=True)

    # Test connection
    print("\n  Testing connection…", end="", flush=True)
    conn_ok, conn_msg = _test_connection(provider, api_key, api_base)
    if conn_ok:
        print(f"  {C.GREEN}✓ Connected!{C.RST}")
        if provider["env_key"]:
            env_data[provider["env_key"]] = api_key
    else:
        print(f"  {C.RED}✗ Failed{C.RST}")
        _err(f"{conn_msg[:150]}")
        if provider.get("local"):
            _warn("Make sure Ollama is running: ollama serve")
            _warn("And the model is pulled:     ollama pull smollm2")
        else:
            retry = _ask("Try a different key? (yes/no)", default="yes")
            if retry.lower().startswith("y"):
                api_key = _ask(f"{provider['name']} API key", secret=True)
                conn_ok, conn_msg = _test_connection(provider, api_key, api_base)
                if conn_ok:
                    _ok("Connected!")
                else:
                    _warn("Still failing — saving key anyway. Fix it in .env later.")
            if provider["env_key"]:
                env_data[provider["env_key"]] = api_key

    # Optional second provider (Gemini is free and speeds up Nova's runtime)
    if provider.get("env_key") == "ANTHROPIC_API_KEY":
        print()
        _info("Tip: a free Gemini key speeds up intent parsing and reduces Claude API costs.")
        add_g = _ask("Add a free Gemini key? (aistudio.google.com/app/apikey) (yes/no)", default="no")
        if add_g.lower().startswith("y"):
            gk = _ask("Gemini API key", secret=True)
            env_data["GEMINI_API_KEY"] = gk

    # ── Telegram (required) ──────────────────────────────────────────
    _section("Step 2 — Telegram  (required)")
    print("  Telegram is how you talk to Nova every day.\n")
    print(f"  {C.BOLD}Quick setup:{C.RST}")
    print(f"  1. Open Telegram  →  search {C.CYAN}@BotFather{C.RST}")
    print(f"  2. Send {C.CYAN}/newbot{C.RST}  →  follow prompts  →  copy the Bot Token")
    print(f"  3. Open {C.CYAN}@userinfobot{C.RST}  →  it shows your Chat ID\n")

    env_data["TELEGRAM_BOT_TOKEN"] = _ask("Telegram Bot Token", secret=True)
    env_data["TELEGRAM_CHAT_ID"]   = _ask("Your Telegram Chat ID")
    _ok("Telegram configured.")
    configured.append("telegram")

    # Save what we have so far
    _create_dirs()
    _write_env(env_data)

    # ── LLM-powered optional features ────────────────────────────────
    _section("Optional Features")
    print(f"  Nova will guide you through the rest. Chat naturally.\n"
          f"  {C.DIM}Type 'done' at any point to finish.{C.RST}\n")
    input(f"  {C.CYAN}Press Enter to start the conversation…{C.RST}")
    print()

    history = [
        {
            "role": "user",
            "content": (
                f"Hi, I'm {owner_name}. I just set up Telegram. "
                "Can you guide me through the optional features? "
                "Start by asking what I want to do with Nova — "
                "that way you can recommend what's most useful for me."
            ),
        }
    ]

    while True:
        # ── Nova's turn ───────────────────────────────────────────────
        reply = _chat(provider, api_key, history, configured, owner_name, api_base)

        # Parse signals before printing
        signal = None
        for feat_key in FEATURES:
            marker = f"[SETUP:{feat_key}]"
            if marker in reply:
                signal = ("setup", feat_key)
                reply = reply.replace(marker, "").strip()
                break
        if "[DONE]" in reply:
            signal = ("done",)
            reply = reply.replace("[DONE]", "").strip()

        # Print Nova's reply
        print(f"\n  {C.CYAN}{C.BOLD}Nova{C.RST}")
        for line in reply.splitlines():
            print(f"  {line}")

        history.append({"role": "assistant", "content": reply})

        # Handle setup signal
        if signal and signal[0] == "setup":
            feat_key = signal[1]
            print()
            yes = _ask(f"Set up {FEATURES[feat_key]['title']} now? (yes/skip)", default="yes")
            if yes.lower().startswith("y"):
                _collect_feature(feat_key, env_data)
                _write_env(env_data)
                configured.append(feat_key)
                history.append({
                    "role": "user",
                    "content": f"Done, {FEATURES[feat_key]['title']} is set up. What else can Nova do?",
                })
            else:
                history.append({
                    "role": "user",
                    "content": f"I'll skip {FEATURES[feat_key]['title']} for now. What other features are there?",
                })
            continue

        if signal and signal[0] == "done":
            break

        # ── User's turn ───────────────────────────────────────────────
        print()
        try:
            user_input = input(f"  {C.BOLD}You{C.RST}   ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            break

        if not user_input or user_input.lower() in ("done", "skip", "exit", "quit", "q"):
            break

        history.append({"role": "user", "content": user_input})

    # ── Summary ──────────────────────────────────────────────────────
    _section("Setup Complete!")

    done_features = [f for f in ["telegram"] + list(FEATURES) if f in configured or f == "telegram"]
    print(f"  {C.BOLD}What's configured:{C.RST}")
    for f in done_features:
        title = FEATURES[f]["title"] if f in FEATURES else "Telegram"
        print(f"  {C.GREEN}✓{C.RST}  {title}")

    skipped = [k for k in FEATURES if k not in configured]
    if skipped:
        print(f"\n  {C.DIM}Not set up (add anytime by re-running this wizard or editing .env):{C.RST}")
        for k in skipped:
            print(f"  {C.DIM}·  {FEATURES[k]['title']}{C.RST}")

    print(f"""
  {C.BOLD}Start Nova:{C.RST}

    {C.CYAN}python src/main.py{C.RST}

  Then open Telegram and say hi to your bot.
""")

    start = _ask("Launch Nova right now? (yes/no)", default="yes")
    if start.lower().startswith("y"):
        print(f"\n  Launching Nova…\n")
        os.execv(sys.executable, [sys.executable, "src/main.py"])


if __name__ == "__main__":
    main()
