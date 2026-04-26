"""
Molty Royale AI Agent — Entry Point v2.0.
Run: python -m bot.main

Single mode (default):
    python -m bot.main

Multi-agent mode:
    MULTI_MODE=1 python -m bot.main
    AGENT_NAMES=Agus,Budi,Susilo MULTI_MODE=1 python -m bot.main
"""
import asyncio
import os
import sys
from bot.config import MULTI_MODE
from bot.utils.logger import get_logger

log = get_logger(__name__)

DASHBOARD_PORT = int(os.getenv("PORT", os.getenv("DASHBOARD_PORT", "8080")))


def main():
    """Entry point for the bot."""
    log.info("Molty Royale AI Agent v2.0.0 (mode=%s)", "multi" if MULTI_MODE else "single")
    log.info("Press Ctrl+C to stop")

    if MULTI_MODE:
        from bot.multi_runner import main as run_multi
        run_multi()
        return

    from bot.heartbeat import Heartbeat
    heartbeat = Heartbeat()

    async def run_all():
        from bot.dashboard.server import start_dashboard
        await start_dashboard(port=DASHBOARD_PORT)
        await heartbeat.run()

    try:
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(run_all())
    except KeyboardInterrupt:
        log.info("Shutdown complete.")


if __name__ == "__main__":
    main()
