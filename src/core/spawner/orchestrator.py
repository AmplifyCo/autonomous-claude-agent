"""Orchestrator for coordinating multiple sub-agents."""

import asyncio
import logging
from typing import List, Dict, Any

from .agent_factory import AgentFactory, SubAgent
from ..types import SubAgentResult

logger = logging.getLogger(__name__)


class Orchestrator:
    """Coordinates multiple sub-agents working on different tasks."""

    def __init__(self, agent_factory: AgentFactory):
        """Initialize orchestrator.

        Args:
            agent_factory: Factory for creating sub-agents
        """
        self.factory = agent_factory
        self.active_agents: Dict[str, SubAgent] = {}

        logger.info("Initialized Orchestrator")

    async def spawn_parallel(
        self,
        tasks: List[Dict[str, Any]],
        max_concurrent: int = 3
    ) -> List[SubAgentResult]:
        """Spawn multiple agents to work in parallel.

        Args:
            tasks: List of task specifications
            max_concurrent: Maximum agents to run concurrently

        Returns:
            List of results from all agents
        """
        logger.info(f"Spawning {len(tasks)} agents in parallel (max concurrent: {max_concurrent})")

        # Create all agents
        agents = []
        for idx, task_spec in enumerate(tasks):
            agent = await self.factory.create_agent(
                task=task_spec.get('description', task_spec.get('task', '')),
                model=task_spec.get('model'),
                context=task_spec.get('context', '')
            )
            agents.append(agent)
            self.active_agents[f"agent_{idx}"] = agent

        # Run agents with concurrency limit
        results = []
        semaphore = asyncio.Semaphore(max_concurrent)

        async def run_with_semaphore(agent: SubAgent) -> SubAgentResult:
            async with semaphore:
                logger.info(f"Starting agent: {agent.task[:50]}...")
                result = await agent.run()
                logger.info(f"Agent completed: {agent.task[:50]}...")
                return result

        # Execute all agents
        results = await asyncio.gather(
            *[run_with_semaphore(agent) for agent in agents],
            return_exceptions=True
        )

        # Handle any exceptions
        final_results = []
        for idx, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Agent {idx} failed with exception: {result}")
                final_results.append(SubAgentResult(
                    success=False,
                    summary="Agent failed with exception",
                    error=str(result)
                ))
            else:
                final_results.append(result)

        # Clear active agents
        self.active_agents.clear()

        logger.info(f"All {len(tasks)} agents completed")
        return final_results

    async def spawn_sequential(
        self,
        tasks: List[Dict[str, Any]]
    ) -> List[SubAgentResult]:
        """Spawn agents one after another (for dependent tasks).

        Args:
            tasks: List of task specifications

        Returns:
            List of results from all agents
        """
        logger.info(f"Spawning {len(tasks)} agents sequentially")

        results = []
        accumulated_context = ""

        for idx, task_spec in enumerate(tasks):
            # Add previous results to context
            context = accumulated_context + "\n\n" + task_spec.get('context', '')

            # Create and run agent
            agent = await self.factory.create_agent(
                task=task_spec.get('description', task_spec.get('task', '')),
                model=task_spec.get('model'),
                context=context
            )

            self.active_agents[f"agent_{idx}"] = agent

            logger.info(f"Starting sequential agent {idx + 1}/{len(tasks)}")
            result = await agent.run()
            results.append(result)

            # Update context with result
            if result.success:
                accumulated_context += f"\n\nPrevious step result:\n{result.summary}"

            # Remove from active
            del self.active_agents[f"agent_{idx}"]

            logger.info(f"Sequential agent {idx + 1}/{len(tasks)} completed")

        logger.info(f"All {len(tasks)} sequential agents completed")
        return results

    async def spawn_with_dependencies(
        self,
        task_graph: Dict[str, Any]
    ) -> Dict[str, SubAgentResult]:
        """Spawn agents respecting dependency graph.

        Args:
            task_graph: Graph of tasks with dependencies

        Returns:
            Dict mapping task IDs to results
        """
        logger.info("Spawning agents with dependency graph")

        results = {}
        completed = set()

        # Topological execution
        async def execute_task(task_id: str, task_spec: Dict[str, Any]):
            # Wait for dependencies
            deps = task_spec.get('dependencies', [])
            while not all(dep in completed for dep in deps):
                await asyncio.sleep(0.1)

            # Build context from dependencies
            context = task_spec.get('context', '')
            for dep in deps:
                if dep in results and results[dep].success:
                    context += f"\n\nDependency {dep} result:\n{results[dep].summary}"

            # Create and run agent
            agent = await self.factory.create_agent(
                task=task_spec.get('description', ''),
                model=task_spec.get('model'),
                context=context
            )

            logger.info(f"Executing task with dependencies: {task_id}")
            result = await agent.run()
            results[task_id] = result
            completed.add(task_id)

            return result

        # Execute all tasks
        await asyncio.gather(*[
            execute_task(task_id, task_spec)
            for task_id, task_spec in task_graph.items()
        ])

        logger.info(f"Completed all {len(task_graph)} tasks with dependencies")
        return results

    def get_active_agents(self) -> List[str]:
        """Get list of active agent IDs.

        Returns:
            List of agent IDs
        """
        return list(self.active_agents.keys())

    def get_status(self) -> Dict[str, Any]:
        """Get orchestrator status.

        Returns:
            Status dictionary
        """
        return {
            "active_agents": len(self.active_agents),
            "agent_ids": list(self.active_agents.keys())
        }
