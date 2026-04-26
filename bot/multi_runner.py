"""
Multi-Agent Runner — runs multiple Molty agents in a single process.
Each agent has its own API key, credentials, and memory. Shared owner EOA.

Usage:
    MULTI_MODE=1 python -m bot.main
    AGENT_NAMES=Agus,Budi,Susilo MULTI_MODE=1 python -m bot.main

Env vars:
    AGENT_NAMES        = comma-separated agent names (e.g., "Agus,Budi")
    MULTI_MODE         = "1" or "true" to enable multi-agent mode

    Per-agent env vars (fallback to global if not set):
    {NAME}_API_KEY             = e.g., AGUS_API_KEY
    {NAME}_AGENT_NAME          = display name
    {NAME}_AGENT_WALLET_ADDRESS
    {NAME}_AGENT_PRIVATE_KEY

    Shared (all agents use same owner):
    OWNER_EOA, OWNER_PRIVATE_KEY
    AUTO_SC_WALLET, AUTO_WHITELIST, AUTO_IDENTITY
"""
import asyncio
import os
import sys
import argparse
from bot.dashboard.state import dashboard_state
from bot.dashboard.server import start_dashboard
from bot.utils.logger import get_logger

log = get_logger(__name__)


def get_multi_agent_names() -> list[str]:
    """Parse AGENT_NAMES env var into list."""
    names_str = os.getenv("AGENT_NAMES", "")
    if names_str:
        names = [n.strip() for n in names_str.split(",") if n.strip()]
        if names:
            return names
    return ["agent-1", "agent-2"]


class MultiAgentRunner:
    """Manages multiple agent instances in a single process."""

    def __init__(self, agent_names: list[str]):
        self.agent_names = agent_names
        self.tasks: list[asyncio.Task] = []
        self.running = True

    async def run(self):
        """Run all agents concurrently via asyncio.gather."""
        log.info("═══════════════════════════════════════════")
        log.info("  MULTI-AGENT RUNNER — %d agents", len(self.agent_names))
        log.info("  Agents: %s", ", ".join(self.agent_names))
        log.info("═══════════════════════════════════════════")

        dashboard_state.bots_running = len(self.agent_names)

        # Create agent heartbeat tasks
        for name in self.agent_names:
            task = asyncio.create_task(self._run_agent(name))
            self.tasks.append(task)
            await asyncio.sleep(0.3)  # stagger startup

        try:
            await asyncio.gather(*self.tasks)
        except asyncio.CancelledError:
            log.info("Multi-agent runner shutting down...")
            self.running = False
            for task in self.tasks:
                task.cancel()

    async def _run_agent(self, agent_name: str):
        """Run single agent heartbeat loop."""
        from bot.heartbeat import Heartbeat

        log.info("  🤖 Agent '%s' heartbeat starting", agent_name)
        heartbeat = Heartbeat(name=agent_name)

        consecutive_errors = 0
        while self.running:
            try:
                await heartbeat.run()
            except asyncio.CancelledError:
                log.info("Agent '%s' cancelled", agent_name)
                break
            except Exception as e:
                consecutive_errors += 1
                wait = min(10 * (2 ** min(consecutive_errors - 1, 4)), 120)
                log.error("Agent '%s' error (#%d): %s. Retry in %ds",
                          agent_name, consecutive_errors, e, wait)
                await asyncio.sleep(wait)


def main():
    """Entry point for multi-agent mode."""
    parser = argparse.ArgumentParser(description="Molty6 AI Agent — Multi-Agent Mode")
    args = parser.parse_args()

    port = int(os.getenv("PORT", os.getenv("DASHBOARD_PORT", "8080")))
    agent_names = get_multi_agent_names()

    log.info("Molty6 Multi-Agent Runner starting with %d agents", len(agent_names))

    async def run_all():
        await start_dashboard(port=port)
        runner = MultiAgentRunner(agent_names)
        await runner.run()

    try:
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(run_all())
    except KeyboardInterrupt:
        log.info("Shutdown complete.")


if __name__ == "__main__":
    main()