from __future__ import annotations

import asyncio
import logging

from app.agent import run_agent_cycle
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


async def run_poll_cycle(settings: Settings | None = None) -> dict[str, int]:
    return await run_agent_cycle(settings)


async def poll_loop(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    interval = max(60, settings.poll_interval_minutes * 60)
    while True:
        try:
            await run_poll_cycle(settings)
        except Exception:
            logger.exception("Poll cycle failed")
        await asyncio.sleep(interval)
