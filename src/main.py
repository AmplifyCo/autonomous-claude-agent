"""Main entry point for the autonomous agent."""

import asyncio
import logging
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.config import load_config
from src.core.agent import AutonomousAgent
from src.core.brain.core_brain import CoreBrain
from src.core.brain.digital_clone_brain import DigitalCloneBrain
from src.core.spawner.agent_factory import AgentFactory
from src.core.spawner.orchestrator import Orchestrator
from src.integrations.anthropic_client import AnthropicClient
from src.utils.telegram_notifier import TelegramNotifier, TelegramCommandHandler
from src.utils.dashboard import Dashboard

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
        else:
            logger.info("🧠 Initializing DigitalCloneBrain for production...")
            brain = DigitalCloneBrain(config.digital_clone_brain_path)

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

        # Initialize sub-agent spawner
        api_client = AnthropicClient(config.api_key)
        agent_factory = AgentFactory(api_client, config)
        orchestrator = Orchestrator(agent_factory)

        logger.info("\n✅ All systems initialized!")
        logger.info("\n" + "="*50)
        logger.info("Implemented Components:")
        logger.info("="*50)
        logger.info("  ✓ Configuration system")
        logger.info("  ✓ Anthropic API client")
        logger.info("  ✓ Tool system (Bash, File, Web)")
        logger.info("  ✓ Dual brain architecture (coreBrain + DigitalCloneBrain)")
        logger.info("  ✓ Core agent execution loop")
        logger.info("  ✓ Sub-agent spawning system")
        logger.info("  ✓ Multi-agent orchestrator")
        logger.info("\n" + "="*50)
        logger.info("Still Needed:")
        logger.info("="*50)
        logger.info("  • Meta-agent self-builder")
        logger.info("  • Monitoring (Telegram + Dashboard)")
        logger.info("  • EC2 deployment scripts")
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

        # Start dashboard server (non-blocking)
        if config.dashboard_enabled and dashboard.enabled:
            logger.info(f"\n🌐 Dashboard available at: http://0.0.0.0:{config.dashboard_port}")
            logger.info("   (Will start when agent runs)")

        # Show Telegram info
        if telegram.enabled:
            logger.info(f"\n📱 Telegram notifications enabled")
            logger.info("   Send /start to your bot to interact")

        # Keep running (for systemd service)
        logger.info("\n✅ Agent initialized and ready!")
        logger.info("Keeping process alive for systemd service...")

        # Keep alive indefinitely
        try:
            await asyncio.Future()  # Run forever
        except KeyboardInterrupt:
            logger.info("\n👋 Shutting down gracefully...")
            await telegram.notify("Agent shutting down", level="warning")

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
