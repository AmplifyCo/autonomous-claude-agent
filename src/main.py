"""Main entry point for the autonomous agent."""

import asyncio
import logging
import sys
from pathlib import Path
from datetime import datetime

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml

from src.core.config import load_config
from src.core.agent import AutonomousAgent
from src.core.brain.core_brain import CoreBrain
from src.core.brain.digital_clone_brain import DigitalCloneBrain
from src.core.spawner.agent_factory import AgentFactory
from src.core.spawner.orchestrator import Orchestrator
from src.core.conversation_manager import ConversationManager
from src.integrations.anthropic_client import AnthropicClient
from src.integrations.model_router import ModelRouter
from src.channels.telegram_channel import TelegramChannel
from src.utils.telegram_notifier import TelegramNotifier, TelegramCommandHandler
from src.utils.dashboard import Dashboard
from src.utils.auto_updater import AutoUpdater
from src.core.scheduler import ReminderScheduler

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)


async def main():
    """Main entry point for the autonomous agent."""

    try:
        # Load configuration
        logger.info("Loading configuration...")
        config = load_config()

        logger.info(f"🤖 Autonomous Claude Agent v1.0.0")
        logger.info(f"Model: {config.default_model}")
        logger.info(f"Self-build mode: {config.self_build_mode}")

        # Initialize appropriate brain
        if config.self_build_mode:
            logger.info("🧠 Initializing coreBrain for self-building...")
            brain = CoreBrain(config.core_brain_path)

            # Populate CoreBrain with project essentials
            logger.info("📚 Populating CoreBrain with project knowledge...")
            await brain.populate_project_essentials({
                "git_url": "https://github.com/AmplifyCo/digital-twin.git",
                "architecture": """Digital Twin - Self-Building AI System with Dual Brain Architecture

Architecture:
- ConversationManager: Channel-agnostic conversation intelligence
- TelegramChannel: Thin transport layer for Telegram
- CoreBrain: Build knowledge and system architecture (shared/factory knowledge)
- DigitalCloneBrain: User-specific conversations (private/personal knowledge)
- ModelRouter: Intelligent model selection (Opus → Sonnet → Haiku → Local fallback)
- Agent: Autonomous task execution with tools (Bash, File, Web, Browser)
- AutoUpdater: Automated security updates with vulnerability scanning
- Monitoring: Telegram notifications + Web dashboard""",

                "build_state": """Current Build Status:
✅ Implemented:
- Configuration system
- Anthropic API client
- Tool system (Bash, File, Web, Browser)
- Dual brain architecture (CoreBrain + DigitalCloneBrain)
- Core agent execution loop
- Sub-agent spawning system
- Multi-agent orchestrator
- Auto-update system with vulnerability scanning
- Monitoring (Telegram + Dashboard)
- Channel-agnostic conversation architecture
- Intent classification with local LLM
- Brain context injection for Claude

🔨 In Progress:
- Meta-agent self-builder

📋 Pending:
- [Add as features are requested]""",

                "guidelines": """Coding Guidelines:
- Use async/await for all I/O operations
- Store build knowledge in CoreBrain (shared across deployments)
- Store user conversations in DigitalCloneBrain (private per user)
- Local LLM for intent classification, Claude API for intelligence
- Local model as fallback when API unavailable
- Always inject Brain context into Claude prompts
- Log important decisions and patterns to CoreBrain
- Keep code modular and well-documented""",

                "system_context": """System: Digital Twin
Mode: Self-Build (CoreBrain active)
Purpose: Building an autonomous AI system that builds itself
Service: digital-twin.service (systemd)
Deployment: EC2 with Cloudflare Tunnel
Models: Claude Opus/Sonnet/Haiku + SmolLM2 (local fallback)"""
            })
        else:
            logger.info("🧠 Initializing DigitalCloneBrain for production...")
            brain = DigitalCloneBrain(config.digital_clone_brain_path)

        # ALWAYS initialize DigitalCloneBrain for conversations/learning
        # Even in build mode, the bot needs to remember user preferences,
        # learn from conversations, and maintain personality
        if config.self_build_mode:
            logger.info("🧠 Also initializing DigitalCloneBrain for conversations...")
            digital_brain = DigitalCloneBrain(config.digital_clone_brain_path)
            core_brain = brain  # CoreBrain for principles + build knowledge
        else:
            digital_brain = brain  # Already a DigitalCloneBrain
            # Still need CoreBrain for intelligence principles
            from src.core.brain.core_brain import CoreBrain as CoreBrainClass
            core_brain = CoreBrainClass(config.core_brain_path)

        # Store intelligence principles in CoreBrain (idempotent — uses doc_id)
        logger.info("🧠 Loading intelligence principles into CoreBrain...")
        await core_brain.store_intelligence_principles()

        # Initialize monitoring systems
        logger.info("📊 Initializing monitoring systems...")
        telegram = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)
        dashboard = Dashboard(config.dashboard_host, config.dashboard_port)

        # Send startup notification
        await telegram.notify(
            f"🚀 *Agent Starting*\n\n"
            f"Mode: {'Self-Build' if config.self_build_mode else 'Production'}\n"
            f"Model: {config.default_model}",
            level="info"
        )

        # Initialize agent
        logger.info("🤖 Initializing autonomous agent...")
        agent = AutonomousAgent(config, brain)
        agent.start_time = datetime.now()  # Track start time for uptime
        agent.digital_brain = digital_brain   # Conversation memories, preferences, contacts
        agent.core_brain = core_brain         # Intelligence principles, build knowledge, patterns

        # Initialize sub-agent spawner
        api_client = AnthropicClient(config.api_key)
        agent_factory = AgentFactory(api_client, config)
        orchestrator = Orchestrator(agent_factory)

        # Initialize auto-updater
        logger.info("🔄 Initializing auto-updater...")
        yaml_config = {}
        config_path = Path("config/agent.yaml")
        if config_path.exists():
            with open(config_path, 'r') as f:
                yaml_config = yaml.safe_load(f) or {}

        auto_update_config = yaml_config.get("auto_update", {})
        auto_updater = AutoUpdater(
            bash_tool=agent.tools.get_tool("bash"),
            telegram=telegram,
            config=auto_update_config
        )

        logger.info("\n✅ All systems initialized!")
        logger.info("\n" + "="*50)
        logger.info("Implemented Components:")
        logger.info("="*50)
        logger.info("  ✓ Configuration system")
        logger.info("  ✓ Anthropic API client")
        logger.info("  ✓ Tool system (Bash, File, Web, Browser)")
        logger.info("  ✓ Dual brain architecture (coreBrain + DigitalCloneBrain)")
        logger.info("  ✓ Core agent execution loop")
        logger.info("  ✓ Sub-agent spawning system")
        logger.info("  ✓ Multi-agent orchestrator")
        logger.info("  ✓ Auto-update system with vulnerability scanning")
        logger.info("  ✓ Monitoring (Telegram + Dashboard)")
        logger.info("\n" + "="*50)
        logger.info("Still Needed:")
        logger.info("="*50)
        logger.info("  • Meta-agent self-builder")
        logger.info("="*50)

        # Demo mode
        if config.self_build_mode:
            logger.info("\n⚠️  Self-building meta-agent not yet implemented")
            logger.info("📝 Next: Implement meta-agent that reads COMPLETE_GUIDE.md")
        else:
            logger.info("\n💡 Agent is ready! You can now:")
            logger.info("   - Call agent.run(task) to execute tasks autonomously")
            logger.info("   - Use orchestrator to spawn multiple sub-agents")
            logger.info("   - Monitor via Telegram commands or web dashboard")

        # Initialize Telegram chat with webhooks (using new channel-agnostic architecture)
        telegram_chat = None
        if telegram.enabled and config.telegram_bot_token and config.telegram_chat_id:
            # Check for Cloudflare tunnel URL first (HTTPS, permanent)
            import subprocess
            import json
            webhook_url = None

            # Try to read Cloudflare tunnel URL
            tunnel_info_path = Path("data/cloudflare_tunnel.json")
            if tunnel_info_path.exists():
                try:
                    with open(tunnel_info_path, 'r') as f:
                        tunnel_info = json.load(f)
                        webhook_url = tunnel_info.get("webhook_url")
                        logger.info(f"🌐 Using Cloudflare Tunnel: {webhook_url}")
                except Exception as e:
                    logger.warning(f"Could not read Cloudflare tunnel info: {e}")

            # Fallback to HTTP with public IP (will fail with Telegram)
            if not webhook_url:
                try:
                    public_ip = subprocess.check_output(
                        ["curl", "-s", "ifconfig.me"],
                        timeout=5
                    ).decode().strip()
                    webhook_url = f"http://{public_ip}:{config.dashboard_port}/telegram/webhook"
                    logger.warning("⚠️  Using HTTP webhook URL - Telegram requires HTTPS!")
                    logger.warning("   Run: bash deploy/cloudflare/setup-tunnel.sh")
                except:
                    webhook_url = None
                    logger.warning("Could not determine webhook URL")

            # Initialize ModelRouter for intelligent model selection
            model_router = ModelRouter(config)

            # Initialize ConversationManager (channel-agnostic core intelligence)
            conversation_manager = ConversationManager(
                agent=agent,
                anthropic_client=api_client,
                model_router=model_router,
                brain=brain  # Auto-selected CoreBrain or DigitalCloneBrain
            )

            # Initialize TelegramChannel (thin transport wrapper)
            telegram_chat = TelegramChannel(
                bot_token=config.telegram_bot_token,
                chat_id=config.telegram_chat_id,
                conversation_manager=conversation_manager,
                webhook_url=webhook_url
            )

            # Register chat handler with dashboard
            if dashboard.enabled:
                dashboard.set_telegram_chat(telegram_chat)

            logger.info("💬 Telegram chat interface initialized (channel-agnostic architecture)")

        # Start dashboard server (non-blocking)
        dashboard_task = None
        if config.dashboard_enabled and dashboard.enabled:
            logger.info(f"\n🌐 Dashboard available at: http://0.0.0.0:{config.dashboard_port}")
            dashboard_task = asyncio.create_task(dashboard.start())
            logger.info("   Starting dashboard server...")

            # Setup webhook after dashboard starts
            if telegram_chat and telegram_chat.webhook_url:
                await asyncio.sleep(2)  # Give dashboard time to start
                await telegram_chat.setup_webhook()

        # Show Telegram info
        if telegram.enabled:
            logger.info(f"\n📱 Telegram notifications enabled")
            if telegram_chat:
                logger.info("   💬 Chat interface: ACTIVE (webhooks)")
                logger.info("   Send a message to your bot to start chatting!")
            else:
                logger.info("   Send /start to your bot to interact")

        # Show auto-update info
        if auto_updater.enabled:
            logger.info(f"\n🔄 Auto-update enabled")
            logger.info(f"   Security-only: {auto_updater.security_only}")
            logger.info(f"   Schedule: {auto_update_config.get('schedule', 'daily')}")
            logger.info(f"   Auto-restart: {auto_updater.auto_restart}")

        # Keep running (for systemd service)
        logger.info("\n✅ Agent initialized and ready!")
        logger.info("Keeping process alive for systemd service...")

        # Start reminder scheduler background task
        reminder_scheduler = ReminderScheduler(telegram=telegram, data_dir="./data")
        reminder_task = asyncio.create_task(reminder_scheduler.start())

        # Start auto-updater background task
        auto_update_task = None
        if auto_updater.enabled:
            logger.info("🔄 Starting auto-update background task...")
            auto_update_task = asyncio.create_task(auto_updater.start_background_task())

        # Keep alive indefinitely
        try:
            await asyncio.Future()  # Run forever
        except KeyboardInterrupt:
            logger.info("\n👋 Shutting down gracefully...")
            if reminder_task:
                reminder_task.cancel()
            if auto_update_task:
                auto_update_task.cancel()
            if dashboard_task:
                dashboard_task.cancel()
            await telegram.notify("Agent shutting down", level="warning")

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
