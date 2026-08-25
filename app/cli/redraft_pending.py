from __future__ import annotations

import asyncio
import logging

from app.db.session import init_db
from app.tools.execution import redraft_open_jobs

logger = logging.getLogger(__name__)


async def _run() -> int:
    return await redraft_open_jobs()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    init_db()
    count = asyncio.run(_run())
    logger.info("redrafted %s jobs", count)


if __name__ == "__main__":
    main()
