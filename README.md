# Nova — the AutoBot

> Your personal AI assistant that learns, remembers, and acts on your behalf.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Nova is a self-hosted, single-user AI assistant you run on your own server. It connects to you via Telegram, learns your preferences over time, and can take actions — send emails, post to X, manage your calendar, set reminders, browse the web, and more.

Unlike generic chatbots, Nova builds a memory of who you are. It remembers your communication style, preferences, and past conversations to provide increasingly personalized assistance.

---

## What Nova Can Do

- **Conversations** — Chat naturally via Telegram with context from past interactions
- **Email** — Read, compose, and reply to emails on your behalf
- **Calendar** — Check, create, and manage calendar events
- **Social Media** — Post to X (Twitter), including community posts
- **Reminders** — Set time-based reminders that fire via Telegram notifications
- **Web Browsing** — Browse pages with visual verification (screenshots)
- **Web Research** — Fetch and summarize web content
- **File Operations** — Read, write, and manage files
- **Shell Commands** — Execute scripts and terminal commands
- **Memory** — Learns and recalls your preferences, habits, and context

---

## Getting Started

### Prerequisites

- Python 3.10+
- An LLM API key — Nova supports multiple providers (see Configuration)
- Telegram Bot Token ([create one via BotFather](https://t.me/BotFather))

### Installation

```bash
git clone https://github.com/AmplifyCo/project-nova.git
cd project-nova

python -m venv venv
source venv/bin/activate

pip install -r requirements.txt
```

### Setup wizard

Run the interactive setup wizard. It guides you through everything — including a live conversation with Nova right in your terminal:

```bash
python setup.py
```

The wizard will:
1. Ask you to pick an LLM provider (SmolLM2 locally via Ollama, Claude, Gemini, OpenAI, or any compatible endpoint)
2. Test your connection immediately
3. Walk you through Telegram setup (required)
4. Let **Nova's setup persona** guide the rest — ask what you want to do, and it recommends which features to configure (email, WhatsApp, X, calendar, voice)

> **No API key? No problem.** Select SmolLM2 (option 1) to run the entire wizard locally using [Ollama](https://ollama.com) — then add a cloud key for Nova's day-to-day use.

### Manual configuration

If you prefer to edit `.env` directly, copy the example and fill in your values:

```bash
cp .env.example .env
nano .env    # or your editor of choice
```

Nova works with any supported LLM provider — configure whichever you have access to:

```bash
# LLM Provider — configure one or more
ANTHROPIC_API_KEY=your_key    # Claude (claude-sonnet, claude-haiku, ...)
GEMINI_API_KEY=your_key       # Gemini (gemini-2.0-flash, ...)
OPENAI_API_KEY=your_key       # OpenAI-compatible endpoints

# Telegram (required)
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

Nova routes tasks to the best available model automatically and falls back gracefully when a provider is unavailable.

### Run

```bash
python src/main.py
```

Nova starts and connects to Telegram. Message your bot to begin.

---

## Deployment (Amazon Linux / EC2)

```bash
# SSH into your instance
ssh -i your-key.pem ec2-user@your-instance-ip

# Clone, install, configure
git clone https://github.com/AmplifyCo/project-nova.git
cd project-nova
chmod +x deploy/ec2/setup.sh
./deploy/ec2/setup.sh

# Configure
nano .env

# Run as a service
sudo systemctl start digital-twin
sudo systemctl enable digital-twin
```

### Optional: Browser Support

For full web browsing with visual verification:

```bash
sudo yum install -y xorg-x11-server-Xvfb
pip install playwright
playwright install --with-deps chromium
```

---

## Adding Talents

Nova's capabilities are modular. Each talent can be enabled independently by adding the required credentials to `.env`. See `config/talents.yaml` for the full list of available and upcoming talents.

```bash
# Check talent status
python -m src.setup talents
```

---

## Architecture

Nova is framework-free — pure Python and asyncio with no heavyweight orchestration frameworks. It is built around three layers:

- **Heart** — the conversation manager that routes intent, selects the right model tier, and dispatches tool calls
- **Brain** — a set of intelligence principles that govern how Nova thinks and acts (plan before acting, verify before done, minimal impact, etc.)
- **Memory** — a vector-backed store of your conversations, preferences, and context, used to personalize every response

Background services handle self-monitoring (error detection + auto-fix), scheduled reminders, and periodic memory consolidation.

### Multi-LLM Routing

Nova supports multiple LLM providers through a unified model router. Tasks are matched to models based on latency, capability, and cost requirements:

| Task type | Default model tier |
|---|---|
| Fast replies, intent classification | Flash / Haiku class |
| Tool use, reasoning, code | Sonnet class |
| Complex research, synthesis | Opus / Pro class |

Configure your preferred providers in `.env`; Nova uses what's available.

---

## Security

- Single-user by design — only your Telegram chat ID can interact
- Credentials stored in `.env`, never committed
- Tool execution is governed by risk-based policies
- Side effects (emails, posts) are deduplicated to prevent double-sends
- All tool outputs are treated as untrusted data

---

## License

MIT — see [LICENSE](LICENSE) for details.

## Disclaimer

Nova is an autonomous AI assistant with real-world capabilities (sending emails, posting to social media, executing commands). Always review its configuration and monitor its behavior, especially when first deploying.

---

Vector memory powered by [LanceDB](https://lancedb.com/)
