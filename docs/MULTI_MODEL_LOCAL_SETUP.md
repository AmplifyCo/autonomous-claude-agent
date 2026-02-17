# Multi-Model Local Setup Guide

## Overview

Run **two specialized local models** on CPU for different tasks:

| Model | Size | Purpose | Speed | Use Cases |
|-------|------|---------|-------|-----------|
| **SmolLM2-1.7B** | 1.7B | Status, Reports, Monitoring | ⚡⚡ 20-40 tok/s | System checks, simple queries, fallback |
| **DeepSeek-R1-1.5B** | 1.5B | Code Changes, Debugging | ⚡⚡ 15-30 tok/s | Quick fixes, code edits, debugging |

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              CLOUD MODELS (Primary)                         │
├─────────────────────────────────────────────────────────────┤
│  Opus 4.6    → Complex planning, feature building           │
│  Sonnet 4.5  → Implementation, conversation                │
│  Haiku 4.5   → Intent parsing, simple tasks                 │
└─────────────────────────────────────────────────────────────┘
                          ↓
                    (if needed or fails)
                          ↓
┌─────────────────────────────────────────────────────────────┐
│              LOCAL MODELS (CPU)                             │
├─────────────────────────────────────────────────────────────┤
│  SmolLM2-1.7B        → Status, reports, monitoring         │
│                       → Fallback for API failures           │
│                                                             │
│  DeepSeek-R1-1.5B    → Quick code changes                  │
│                       → Debugging, small fixes              │
└─────────────────────────────────────────────────────────────┘
```

## Why Two Models?

**SmolLM2-1.7B** - General purpose:
- ✅ Fast status checks
- ✅ System monitoring & reports
- ✅ Simple conversation
- ✅ API fallback (rate limits, outages)
- ❌ Not specialized for code

**DeepSeek-R1-Distill-Qwen-1.5B** - Coding specialist:
- ✅ Reasoning-based code generation
- ✅ Quick bug fixes
- ✅ Code refactoring
- ✅ Debugging assistance
- ❌ Not optimized for general chat

## Configuration

### Option 1: Both Models (Recommended)

```bash
# .env configuration

# General model (SmolLM2)
LOCAL_MODEL_ENABLED=true
LOCAL_MODEL_NAME=HuggingFaceTB/SmolLM2-1.7B-Instruct
LOCAL_MODEL_ENDPOINT=http://localhost:8000
LOCAL_MODEL_FOR=trivial,simple

# Coding model (DeepSeek-R1)
LOCAL_CODER_ENABLED=true
LOCAL_CODER_NAME=deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B
LOCAL_CODER_ENDPOINT=http://localhost:8001
```

**Benefits:**
- Status checks → SmolLM2 (instant, free)
- Code fixes → DeepSeek-R1 (specialized)
- API failures → SmolLM2 fallback
- Total: ~6GB RAM, both very fast on CPU

### Option 2: SmolLM2 Only (Simpler)

```bash
LOCAL_MODEL_ENABLED=true
LOCAL_MODEL_NAME=HuggingFaceTB/SmolLM2-1.7B-Instruct
LOCAL_MODEL_ENDPOINT=

LOCAL_CODER_ENABLED=false
```

**Benefits:**
- Simpler setup (one model)
- ~4GB RAM
- Good for status checks and fallback
- Can still handle simple code tasks (not specialized)

### Option 3: DeepSeek-R1 Only (Code-focused)

```bash
LOCAL_MODEL_ENABLED=false

LOCAL_CODER_ENABLED=true
LOCAL_CODER_NAME=deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B
LOCAL_CODER_ENDPOINT=
```

**Benefits:**
- Focused on code tasks
- ~3GB RAM
- Best for development workflow
- No general fallback

## Setup Instructions

### Step 1: Install Dependencies

```bash
# Install transformers and torch
pip install transformers torch

# Optional: Install vLLM for faster inference
pip install vllm
```

### Step 2: Run Model Servers

**Option A: Direct Inference (Simple)**

No server needed - models load on first use:

```bash
# Just enable in .env
LOCAL_MODEL_ENABLED=true
LOCAL_CODER_ENABLED=true

# Models download on first request (~3-4GB each)
```

**Option B: vLLM Servers (Faster)**

Run separate servers for each model:

```bash
# Terminal 1: SmolLM2 server
python -m vllm.entrypoints.openai.api_server \
  --model HuggingFaceTB/SmolLM2-1.7B-Instruct \
  --host 0.0.0.0 \
  --port 8000 \
  --dtype float32

# Terminal 2: DeepSeek-R1 server
python -m vllm.entrypoints.openai.api_server \
  --model deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B \
  --host 0.0.0.0 \
  --port 8001 \
  --dtype float32

# Update .env with endpoints
LOCAL_MODEL_ENDPOINT=http://localhost:8000
LOCAL_CODER_ENDPOINT=http://localhost:8001
```

**Option C: Systemd Services (Production)**

Create persistent services:

```bash
# /etc/systemd/system/smollm2.service
[Unit]
Description=SmolLM2 Inference Server
After=network.target

[Service]
Type=simple
User=your-user
WorkingDirectory=/home/your-user
ExecStart=/usr/bin/python -m vllm.entrypoints.openai.api_server \
  --model HuggingFaceTB/SmolLM2-1.7B-Instruct \
  --host 0.0.0.0 --port 8000 --dtype float32
Restart=always

[Install]
WantedBy=multi-user.target

# Enable and start
sudo systemctl enable smollm2
sudo systemctl start smollm2
```

```bash
# /etc/systemd/system/deepseek-r1.service
[Unit]
Description=DeepSeek-R1 Code Inference Server
After=network.target

[Service]
Type=simple
User=your-user
WorkingDirectory=/home/your-user
ExecStart=/usr/bin/python -m vllm.entrypoints.openai.api_server \
  --model deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B \
  --host 0.0.0.0 --port 8001 --dtype float32
Restart=always

[Install]
WantedBy=multi-user.target

# Enable and start
sudo systemctl enable deepseek-r1
sudo systemctl start deepseek-r1
```

### Step 3: Update Agent Configuration

```bash
# Edit .env
nano .env

# Add configuration (see options above)

# Restart agent
sudo systemctl restart claude-agent
```

## Usage Examples

### Telegram Interactions

**Status Check (SmolLM2):**
```
User: "What's your status?"
→ Router: TRIVIAL task
→ Model: SmolLM2-1.7B (local, port 8000)
→ Response: "✅ Agent running. Uptime: 3h 45m. All systems operational."
```

**Code Fix (DeepSeek-R1):**
```
User: "Fix the bug in auth.py line 42"
→ Router: CODE task
→ Model: DeepSeek-R1-1.5B (local, port 8001)
→ Action: Analyzes code, suggests fix
→ Response: "Found the issue - missing null check. Here's the fix: [code]"
```

**Conversation (Cloud):**
```
User: "How can I improve my architecture?"
→ Router: MODERATE task (needs deep understanding)
→ Model: Sonnet 4.5 (cloud)
→ Response: [Detailed architectural advice]
```

**API Failure (SmolLM2 Fallback):**
```
User: "What's happening?"
→ Try: Haiku 4.5 (cloud)
→ Error: 429 Rate limit
→ Fallback: SmolLM2-1.7B (local)
→ Response: "⚠️ Rate limit - Using local model. Currently monitoring 5 processes..."
```

## Model Selection Logic

```python
# Router decision tree

if task_type == "status" or task_type == "report":
    if local_model_enabled:
        → SmolLM2-1.7B (local, instant)
    else:
        → Haiku 4.5 (cloud)

elif task_type == "code_edit" or task_type == "debug":
    if local_coder_enabled:
        → DeepSeek-R1-1.5B (local, specialized)
    else:
        → Sonnet 4.5 (cloud)

elif task_type == "conversation":
    → Sonnet 4.5 (cloud, quality)

elif task_type == "complex" or task_type == "build":
    → Opus 4.6 (cloud, architect)

# Fallback for API errors
except APIError:
    if local_model_enabled:
        → SmolLM2-1.7B (fallback)
    else:
        raise error
```

## Performance Comparison

### CPU Inference Speed

```
Model                   Size    Speed       RAM     Best For
─────────────────────────────────────────────────────────────
SmolLM2-360M            360M    ⚡⚡⚡ 50-100   ~2GB    Ultra-fast status
SmolLM2-1.7B            1.7B    ⚡⚡ 20-40     ~4GB    General (recommended)
DeepSeek-R1-1.5B        1.5B    ⚡⚡ 15-30     ~3GB    Coding (recommended)
Phi-3-mini              3.8B    ⚡ 10-20      ~8GB    Alternative general
Mistral-7B              7B      🐌 2-5        ~14GB   Too slow for CPU
```

### Resource Usage

**SmolLM2 Only:**
- RAM: ~4GB
- CPU: 20-40% during inference
- Disk: ~3.4GB model file

**DeepSeek-R1 Only:**
- RAM: ~3GB
- CPU: 15-30% during inference
- Disk: ~3GB model file

**Both Models:**
- RAM: ~6-7GB total
- CPU: 30-50% during concurrent use
- Disk: ~6.5GB total

## Monitoring

### Check Model Status

```bash
# Check SmolLM2 server
curl http://localhost:8000/health

# Check DeepSeek-R1 server
curl http://localhost:8001/health

# Monitor agent logs
tail -f data/logs/agent.log | grep -E "local|SmolLM|DeepSeek"
```

### Test Models

```bash
# Test SmolLM2
curl -X POST http://localhost:8000/v1/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "HuggingFaceTB/SmolLM2-1.7B-Instruct", "prompt": "What is your status?", "max_tokens": 50}'

# Test DeepSeek-R1
curl -X POST http://localhost:8001/v1/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B", "prompt": "Fix this Python function:", "max_tokens": 100}'
```

## Troubleshooting

### Models Not Loading

```bash
# Check transformers version
pip install --upgrade transformers torch

# Test model download
python -c "from transformers import AutoTokenizer; AutoTokenizer.from_pretrained('HuggingFaceTB/SmolLM2-1.7B-Instruct')"
python -c "from transformers import AutoTokenizer; AutoTokenizer.from_pretrained('deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B')"
```

### vLLM Server Issues

```bash
# Check server logs
journalctl -u smollm2 -n 50
journalctl -u deepseek-r1 -n 50

# Restart servers
sudo systemctl restart smollm2
sudo systemctl restart deepseek-r1
```

### High Memory Usage

```bash
# Check memory
free -h

# If RAM is limited, use only one model
# Or use smaller variants:
LOCAL_MODEL_NAME=HuggingFaceTB/SmolLM2-360M-Instruct  # Only 2GB RAM
```

## Cost Savings

### With Both Local Models

```
Daily usage (100 messages):

Without local models:
- 30 status checks × Haiku   = $0.15
- 20 code tasks × Sonnet      = $1.00
- 30 conversations × Sonnet   = $1.50
- 20 complex tasks × Opus     = $5.00
Total: ~$7.65/day

With local models:
- 30 status checks × SmolLM2  = $0.00 (local)
- 20 code tasks × DeepSeek-R1 = $0.00 (local)
- 30 conversations × Sonnet   = $1.50
- 20 complex tasks × Opus     = $5.00
Total: ~$6.50/day

Savings: ~$1.15/day = ~$420/year
```

## Best Practices

1. **Use SmolLM2 for:**
   - ✅ Status checks
   - ✅ System monitoring
   - ✅ Simple reports
   - ✅ API fallback

2. **Use DeepSeek-R1 for:**
   - ✅ Quick bug fixes
   - ✅ Code refactoring
   - ✅ Debugging help
   - ✅ Small code generation

3. **Use Cloud (Sonnet) for:**
   - ✅ Conversations
   - ✅ Complex understanding
   - ✅ Multi-file changes
   - ✅ Architectural decisions

4. **Use Cloud (Opus) for:**
   - ✅ Feature building
   - ✅ Complex planning
   - ✅ Multi-agent orchestration

## Summary

**Dual-model setup gives you:**
- ✅ Instant status checks (SmolLM2)
- ✅ Fast code fixes (DeepSeek-R1)
- ✅ API failure resilience
- ✅ Cost savings (~$400/year)
- ✅ No rate limits for simple tasks
- ✅ Specialized models for specific jobs

**Quick Start:**
```bash
# Install
pip install vllm transformers torch

# Configure
LOCAL_MODEL_ENABLED=true
LOCAL_MODEL_NAME=HuggingFaceTB/SmolLM2-1.7B-Instruct

LOCAL_CODER_ENABLED=true
LOCAL_CODER_NAME=deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B

# Restart
sudo systemctl restart claude-agent
```

Enjoy unlimited local inference for status checks and code tasks! 🚀
