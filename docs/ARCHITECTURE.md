# Nova - the AutoBot | Architecture Overview

```text
                        ┌─────────────────────┐
                        │    User / Telegram   │
                        └──────────┬──────────┘
                                   │
                        ┌──────────▼──────────┐
                        │   Transport Layer    │  Telegram, Email, WhatsApp (future)
                        │   (Thin Channels)    │  Just send/receive — zero logic
                        └──────────┬──────────┘
                                   │
  ┌────────────────────────────────▼────────────────────────────────┐
  │                          HEART                                  │
  │              (CoreEngine + ConversationManager)                  │
  │                                                                 │
  │  • Intent classification (Haiku)     • Circuit breaker          │
  │  • Model routing (Local→Haiku→Sonnet→Opus)                     │
  │  • Per-session locking               • Trace ID (observability) │
  │  • Context Thalamus (token budgeting, history summarization)    │
  │  • Rate limiting + input sanitization (12 security layers)      │
  │  • Fallback: Claude API → SmolLM2 local model                  │
  └──────────┬─────────────────────────────┬───────────────────────┘
             │                             │
   ┌─────────▼─────────┐       ┌──────────▼──────────┐
   │      BRAIN         │       │   NERVOUS SYSTEM     │
   │  (CoreBrain +      │       │  (ExecutionGovernor)  │
   │   Principles)      │       │                       │
   │                    │       │  • Policy Gate         │
   │  • 5 intelligence  │       │    (risk: read/write/  │
   │    principles      │       │     irreversible)      │
   │  • Bot identity    │       │  • State Machine       │
   │  • Prompt version  │       │    (IDLE→THINKING→     │
   │    control         │       │     EXECUTING→DONE)    │
   │  • Injection guard │       │  • Durable Outbox      │
   │    for tool output │       │    (no double-sends)   │
   │  • Security rules  │       │    (constitutional)│       │    (poison events)     │
   └─────────┬─────────┘       └──────────┬────────────┘
             │                             │
             └──────────┬──────────────────┘
                        │
             ┌──────────▼──────────┐
             │      TALENTS        │
             │   (Tools + Registry) │
             │                      │
             │  • Email (IMAP/SMTP) │  • Bash (5-layer sandbox)
             │  • Calendar (CalDAV) │  • File read/write
             │  • X/Twitter (OAuth) │  • Web search/browse
             │  • Reminders (JSON)  │  • [Extensible: LinkedIn, Slack...]
             │                      │
             │  Universal timeout (60s) │ Parallel execution (asyncio.gather)
             │  Performance tracking    │ Auto-disable after 5 consecutive failures
             └──────────┬──────────┘
                        │
        ┌───────────────┼───────────────┐
        │               │               │
  ┌─────▼─────┐  ┌─────▼──────┐  ┌─────▼──────────┐
  │  MEMORY   │  │ SELF-HEAL  │  │ SELF-LEARNING  │
  │           │  │            │  │                 │
  │ Digital   │  │ Error      │  │ Learn from      │
  │ CloneBrain│  │ Detector   │  │ conversations   │
  │           │  │ → Auto     │  │ (Haiku extracts │
  │ Collective│  │   Fixer    │  │  facts/prefs)   │
  │ • Identity│  │ → Telegram │  │                 │
  │ • Prefs   │  │   alerts   │  │ Tool success/   │
  │ • Contacts│  │            │  │ failure tracking │
  │           │  │ Scans logs │  │                 │
  │ Isolated  │  │ every 5min │  │ Memory with     │
  │ • per-    │  │            │  │ confidence +    │
  │   talent  │  │ Auto-fixes:│  │ source tracking │
  │   context │  │ imports,   │  │                 │
  │           │  │ ChromaDB  │  │ git errors │  │ Consolidation   │
  │ (vectors) │  │            │  │ (prune old      │
  │ WAL mode  │  └────────────┘  └─────────────────┘
  │ async I/O │
  └───────────┘
```

## Key Design Principles

| Principle | Implementation |
|-----------|----------------|
| **Framework-free** | No LangChain/LangGraph/CrewAI — pure Python + asyncio + Anthropic SDK |
| **Human-inspired naming** | Heart, Brain, Nervous System, Memory, Talents — maps to biological metaphor |
| **Defense in depth** | 12 security layers: rate limit, input sanitization, semantic validation, output redaction, policy gate, tool sandbox |
| **Graceful degradation** | Claude API → SmolLM2 fallback, circuit breaker (3 failures → skip API for 2 min), tool auto-disable after 5 failures |
| **Channel-agnostic** | Transport layers are thin — all intelligence lives in Heart. Adding WhatsApp/Discord = new transport, zero logic changes |
| **Memory isolation** | Each talent (email, telegram, X) has isolated memory. Collective consciousness (identity, preferences, contacts) is shared |
| **Self-evolving** | Self-Healing monitors + auto-fixes. Self-Learning extracts facts from conversations. Memory consolidation prunes automatically |

## Data Flow: "Remind me to call the dentist tomorrow at 2:30pm"

1. **Telegram** → **Heart** (intent: action, model: Sonnet)
2. → **Brain** (system prompt + principles + memory context)
3. → **Nervous System** (policy: WRITE, allowed)
4. → **Talent**: `ReminderTool.set_reminder()`
5. → `data/reminders.json`
6. → **User**: "I'll remind you tomorrow at 2:30 PM"
7. → [30s later] **Scheduler** fires → Telegram notification

## Tech Stack

| Layer | Technology |
|-------|------------|
| **LLM (primary)** | Claude API (Opus/Sonnet/Haiku) |
| **LLM (fallback)** | SmolLM2 via Ollama (local) |
| **Vector DB** | ChromaDB (persistent, WAL mode) |
| **Embeddings** | all-MiniLM-L6-v2 (ChromaDB default) |
| **Transport** | python-telegram-bot (webhooks via Cloudflare Tunnel) |
| **Persistence** | JSON files (reminders, outbox, DLQ) + ChromaDB |
| **Deployment** | EC2 + systemd + Cloudflare Tunnel |
| **Concurrency** | asyncio (event loop, gather, semaphores, locks) |

## Background Tasks (always running)

| Task | Interval | Purpose |
|------|----------|---------|
| **ReminderScheduler** | 30s | Fire due reminders via Telegram |
| **SelfHealingMonitor** | 5min | Scan logs, detect errors, auto-fix |
| **MemoryConsolidator** | 6hr | Prune old conversation turns (>30 days) |
| **AutoUpdater** | configurable | Security patches + vulnerability scanning |
| **Dashboard** | always | Web UI for monitoring |