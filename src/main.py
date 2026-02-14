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
from ...integrations.anthropic_client import AnthropicClient

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
            logger.info("   - Test with simple tasks to verify functionality")

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
