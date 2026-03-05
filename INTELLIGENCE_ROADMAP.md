# Nova Intelligence Roadmap — From Prompt-Taker to Thinking Agent

## Phase 1: Visible Intelligence (IMPLEMENTED)
*Make what Nova already knows felt in every interaction*

### 1A. Context Callbacks
When Nova uses recalled episodic memory, corrections, or preferences, it naturally references them.
"Based on what worked last time...", "You mentioned you prefer..."

### 1B. Conversation Continuity
Open threads tracked across sessions (max 3, 48h expiry). Session greeting when user returns after 30+ min gap.
"Welcome back — we were working on that LinkedIn post, want to continue?"

### 1C. Visible Correction Learning
Corrections acknowledged immediately ("Got it, adjusting") and stored in working memory. Future similar tasks reference past corrections.

### 1D. Preference Profiling
Structured preference model built incrementally from learning extraction. Injected into all prompts so Nova consistently references known preferences.

**Files**: working_memory.py (+159 lines), conversation_manager.py (+92 lines)
**Latency**: Zero new LLM calls

---

## Phase 2: Anticipatory Intelligence (IMPLEMENTED — 2A, 2C, 2D)
*From reactive to predictive — Nova acts before you ask*

### 2A. Pattern Detection Engine (IMPLEMENTED)
Background 12h scan of episodic memory. Extracts time-action patterns ("You post on LinkedIn every Tuesday"). Stores as patterns.json. Fed to attention engine for proactive suggestions.
**New file**: brain/pattern_detector.py
**Wired into**: attention_engine.py, main.py
**Latency**: +1 Gemini Flash call per 12h (background)

### 2B. Proactive Drafts
Attention engine evolves from "observe and report" to "observe, predict, prepare." Tuesday morning: "I've drafted your LinkedIn post based on this week's work." Before meetings: surface context from last conversation with that person.
**Status**: Not yet implemented

### 2C. Smart Scheduling (Circadian Rhythm) (IMPLEMENTED)
Time-based behavior modifiers injected into all conversation prompts. Morning = briefing mode. Work hours = professional (default). Evening = lighter, reflective. Late night = minimal.
**New file**: brain/circadian.py
**Wired into**: conversation_manager.py (_build_system_prompt + _chat)
**Latency**: Zero (rule-based, static methods)

### 2D. Contact Intelligence (IMPLEMENTED)
Per-contact interaction history tracked in JSON. Surfaces "Last time you emailed Sarah..." and pending follow-ups in execution plans. Stale contacts surfaced by attention engine.
**New file**: brain/contact_intelligence.py
**Wired into**: conversation_manager.py, attention_engine.py, main.py
**Latency**: Zero (structured data, no LLM calls)

---

## Phase 3: Reasoning & Judgment (IMPLEMENTED — 3B, 3C, 3D)
*From command executor to thinking partner*

### 3A. Pushback & Alternatives
Post-draft evaluation layer. Before irreversible actions, check timing, tone, consistency. "You could post this now, but spacing it to Monday would get better engagement."
**Status**: Not yet implemented

### 3B. Reasoning Transparency (IMPLEMENTED)
Preflight APPROACH section surfaced to user via progress callback before agent execution. Task runner plan notifications enhanced with tools summary and approach reasoning.
**Modified**: conversation_manager.py (_extract_approach + progress_callback), task_runner.py (_notify_plan)
**Latency**: Zero (uses existing preflight reasoning, just surfaces it)

### 3C. Quality Self-Assessment (IMPLEMENTED)
Substantive responses (>150 chars) get lightweight Gemini Flash self-eval (high/medium/low confidence). Only low-confidence areas surfaced naturally: "The competitor analysis is thin — want me to dig deeper?"
**New file**: brain/self_assessor.py
**Wired into**: conversation_manager.py (action + question blocks), main.py
**Latency**: +1 Gemini Flash call per substantive response (~1s, fail-open)

### 3D. Multi-Turn Reasoning (Deliberation) (IMPLEMENTED)
Keyword-triggered ("should I", "pros and cons", "compare") → gathers evidence from brain context + episodic memory → produces EVIDENCE/TENSIONS/RECOMMENDATION/CAVEATS structure. Replaces standard preflight for judgment questions.
**Uses**: brain/self_assessor.py (SelfAssessor.deliberate + needs_deliberation)
**Wired into**: conversation_manager.py (preflight upgrade), main.py
**Latency**: +1 Gemini Flash call for judgment questions only (~2s, fail-open)

---

## Phase 4: Autonomous Growth (IMPLEMENTED — 4A)
*Nova improves itself without being told*

### 4A. Skill Acquisition (IMPLEMENTED)
Nova reads external API specifications (.md files) and autonomously generates working tool plugins. Pipeline: fetch spec → parse with Gemini Flash → generate BaseTool code + manifest → AST safety validation → write to plugins/ → hot-reload via PluginLoader. Zero hub edits for new tools. Triggered by "learn this skill: URL" or agent tool call.
**New file**: brain/skill_learner.py (~350 lines)
**New file**: tools/skill_tool.py (~90 lines)
**Wired into**: registry.py (_register_skill_tool + set_skill_learner), main.py, conversation_manager.py (_handle_skill_learn fast-path), goal_decomposer.py
**Latency**: +2 Gemini Flash calls per skill learn only (parse ~2s + generate ~3s). Fail-open.

### 4B. Feedback Loop Closure
Track outcomes, not just actions. Poll LinkedIn API for engagement after 24h. Track email replies within 48h. Store outcomes in episodic memory. Pattern detector uses these for calibration.
**Modify**: episodic_memory.py, attention_engine.py

### 4C. Knowledge Base Building
Research results chunked and stored with topic tags. Next time user asks about same topic, Nova doesn't start from scratch — it builds on existing knowledge.
**New file**: brain/knowledge_base.py

### 4D. Self-Monitoring & Growth Reports
Weekly: "23 requests handled, 3 corrections received (all about tone), approval rate up from 60% to 85%." Monthly: areas to improve, self-directed learning goals.
**New file**: brain/self_monitor.py

---

## Phase 5: Agent Economy — From Assistant to CXO (Chief Execution Officer)
*Nova manages like a CXO — knows the mission, delegates to the right agents, only does what nobody else can*

### 5A. MCP Client (Tool Universe) (IMPLEMENTED)
Nova consumes any MCP-compatible tool server. Instead of hand-coding each integration, Nova discovers tools from the MCP Registry (~2,000+ servers) and connects on demand.
- MCP client adapter in Nova's tool layer (MCPClientTool extends BaseTool)
- Config-driven: define servers in config/mcp_servers.json (command, args, env, risk_level)
- Lazy connection: servers spawn on first use, not at boot
- Namespaced tools: `server__tool_name` prevents collisions
- PolicyGate integration: risk levels from config injected into TOOL_RISK_MAP
- Hot-reload: "reload plugins" re-discovers MCP servers
- Auth handling: `${VAR}` env resolution via credential store → os.getenv
**New files**: src/core/tools/mcp/ (mcp_client_tool.py, mcp_server_manager.py)
**Modified**: registry.py (+MCP wiring), main.py (+startup discovery/shutdown)
**Protocols**: MCP (JSON-RPC 2.0), stdio transport
**Impact**: Nova goes from ~15 hand-built tools to thousands, instantly

### 5B. Agent Identity & Discovery (IMPLEMENTED)
Nova publishes an Agent Card (A2A spec) and exposes a JSON-RPC 2.0 endpoint for receiving tasks.
- `/.well-known/agent-card.json` — public discovery (name, skills, capabilities, auth scheme)
- `/a2a` — JSON-RPC 2.0 endpoint (message/send, tasks/get, tasks/cancel)
- Bearer token auth (reuses NOVA_API_KEY)
- A2A tasks flow through existing TaskQueue → GoalDecomposer → TaskRunner pipeline
- Config-driven skills in config/agent_card.json
**New files**: src/a2a/ (models.py, agent_card.py, handler.py)
**Modified**: dashboard.py (+routes), main.py (+wiring)
**Protocols**: A2A (JSON-RPC 2.0), Bearer auth
**Impact**: Nova becomes discoverable — other agents can find and delegate to it

### 5C. Agent Collaboration & Delegation (Orchestrator Mode)
Nova becomes a **delegation-first orchestrator**. When work comes in, Nova evaluates: "Should I do this myself, or is there a better agent for this subtask?"
- **Agent Discovery**: Find specialized agents via A2A Agent Cards, Moltbook network, MCP Registry
- **Capability Matching**: Match subtask requirements against known agent capabilities (skills, cost, reliability, speed)
- **Task Delegation**: Send subtasks to external agents via A2A Protocol (stateful tasks with lifecycle: Working → Completed/Failed)
- **Parallel Orchestration**: Multiple agents working simultaneously on different subtasks, Nova monitors and merges results
- **Quality Gate**: Nova reviews delegated results before delivering to user — reject and retry/reassign if quality is low
- **Cost Optimization**: Track agent costs (tokens, ETH, API calls) and prefer efficient agents for routine work
- **Fallback**: If delegated agent fails or times out, Nova picks up the work itself

Example flow:
```
User: "Research AI agent frameworks, write a comparison post for LinkedIn, and tweet the key takeaway"
Nova (orchestrator):
  ├─ Subtask 1: Research → delegate to a research-specialist agent (via A2A)
  ├─ Subtask 2: Wait for research → Write LinkedIn post (Nova does this — it knows Srinath's voice)
  ├─ Subtask 3: Extract takeaway → Write tweet (Nova does this — voice-specific)
  └─ Quality gate: CriticAgent reviews both drafts → schedule posts
```

Another example:
```
User: "Audit this smart contract for vulnerabilities"
Nova (orchestrator):
  ├─ Discovers 3 code-audit agents on Moltlaunch
  ├─ Sends contract to top-rated agent
  ├─ Monitors progress via A2A task status
  ├─ Receives results, summarizes for Srinath
  └─ If results are thin, assigns to second agent for cross-check
```

**Architecture**:
- `brain/agent_broker.py` — discovers, ranks, and selects agents for subtasks
- `brain/delegation_engine.py` — A2A task lifecycle management (create, monitor, collect, retry)
- Extends existing GoalDecomposer: subtasks now get `execution_mode: self | delegate`
- Extends TaskRunner: delegate-mode subtasks sent to external agents instead of local agent loop
**Protocols**: A2A (task management), MCP (tool sharing), Moltbook API (social discovery)
**Impact**: Nova's throughput multiplies — it manages a team instead of doing everything alone

### 5D. Marketplace & Payments (Agent Treasury)
Nova takes paid work and manages money like a CXO with a project budget — never fronts its own capital.

**Payment flow (standard commercial terms):**
```
1. Client posts job → Nova bids with quote
2. Client accepts → payment locked in escrow (USDC/ETH)
3. Nova decomposes and executes:
   ├─ Self-execute what it can
   └─ Delegate subtasks to sub-agents (work now, pay on completion)
4. Nova delivers → client approves → escrow releases to Nova
5. Nova pays sub-agents from received payment
6. Profit = client payment − sub-agent fees − gas
```
Nova never pays before getting paid. Sub-agents work on delivery terms — same as any contractor relationship. If a sub-agent demands upfront payment, Nova factors that into the bid or picks a different agent.

- **Wallet**: Base (Moltlaunch, Stripe x402) + Solana (broader agent economy). USDC as default currency.
- **x402 client**: HTTP 402 auto-pay for APIs and agent services — agent requests resource, gets payment request, signs USDC, access granted. No human in the loop.
- **Budget management**: Before accepting a job, Nova estimates sub-agent costs and only bids if margin is positive. Tracks spend per subtask.
- **Moltlaunch integration**: Browse gigs, bid, execute, collect ETH. ERC-8004 on-chain identity + reputation.
- **Job matching**: Compare job requirements against Nova's capabilities + delegatable skills
- **Reputation building**: Completed jobs build on-chain reputation, unlocking higher-value work
- **Revenue dashboard**: Jobs completed, gross earnings, delegation costs, net profit, wallet balances
**Protocols**: x402 (HTTP-native payments), Moltlaunch API, ERC-8004 (agent identity on Base), Stripe Agentic Wallets
**Impact**: Nova becomes revenue-generating — earns by orchestrating, not just executing

### 5E. Agent Network & Alliances
Nova builds a trusted network of specialist agents it works with repeatedly.
- **Agent Rolodex**: Track known agents with reliability scores, specializations, response times, costs
- **Preferred partners**: Agents that consistently deliver quality become preferred for future delegation
- **Reciprocal work**: Accept delegated subtasks from other agents (Nova is strong at content, communication, research)
- **Alliance formation**: Informal agent teams that frequently collaborate on complex multi-agent jobs
**Impact**: Nova has a "professional network" — like a freelancer with trusted subcontractors

---

## Summary

| Phase | Theme | Nova Feels Like... | Key Shift |
|-------|-------|--------------------|-----------|
| 1     | Visible Intelligence | "It remembers me" | Show existing intelligence |
| 2     | Anticipatory | "It knows what I need" | React to Predict |
| 3     | Reasoning | "It thinks for itself" | Execute to Judge |
| 4     | Autonomous Growth | "It's getting better" | Static to Evolving |
| 5     | Agent Economy | "It runs the show" | Assistant to CXO |
