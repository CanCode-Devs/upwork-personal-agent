from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from app.agent import run_agent_cycle
from app.config import Settings, get_settings
from app.models import PollStatus, PollStatusView

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class PollScheduler:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._loop_running = False
        self._polling = False
        self._pending_source = ""
        self._source = ""
        self._started_at: datetime | None = None
        self._last_finished_at: datetime | None = None
        self._next_poll_at: datetime | None = None
        self._last_new = 0
        self._last_updated = 0
        self._interval = 15 * 60

    def configure(self, interval_seconds: int) -> None:
        self._interval = max(60, interval_seconds)
        if self._polling or self._pending_source:
            return
        now = _utc_now()
        if self._last_finished_at is not None:
            scheduled = self._last_finished_at + timedelta(seconds=self._interval)
            new_next = now if scheduled <= now else scheduled
            if self._next_poll_at != new_next:
                earlier = self._next_poll_at is None or new_next < self._next_poll_at
                self._next_poll_at = new_next
                if earlier and self._loop_running:
                    self._wake.set()
            return
        if self._next_poll_at is None:
            self._next_poll_at = now

    def snapshot(self) -> PollStatusView:
        pending = bool(self._pending_source)
        return PollStatusView(
            polling=self._polling or pending,
            source=self._source or self._pending_source,
            next_poll_at=None if (self._polling or pending) else self._next_poll_at,
            last_finished_at=self._last_finished_at,
            started_at=self._started_at,
            interval_seconds=self._interval,
            last_new=self._last_new,
            last_updated=self._last_updated,
        )

    async def trigger(self, settings: Settings | None = None) -> None:
        if self._polling or self._pending_source:
            return
        if self._loop_running:
            self._pending_source = "manual"
            self._next_poll_at = _utc_now()
            self._wake.set()
            return
        await self._run_once(settings or get_settings(), "manual")

    async def run_loop(self, settings: Settings) -> None:
        self.configure(settings.poll_interval_minutes * 60)
        self._loop_running = True
        self._pending_source = "auto"
        try:
            while True:
                current = get_settings()
                self.configure(current.poll_interval_minutes * 60)
                remaining = self._seconds_until_next()
                if remaining > 0:
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=remaining)
                    except TimeoutError:
                        pass
                    self._wake.clear()
                    continue
                source = self._pending_source or "auto"
                await self._run_once(current, source)
                self._wake.clear()
        finally:
            self._loop_running = False

    def _seconds_until_next(self) -> float:
        if self._pending_source:
            return 0.0
        if self._next_poll_at is None:
            return 0.0
        return max(0.0, (self._next_poll_at - _utc_now()).total_seconds())

    async def _run_once(self, settings: Settings, source: str) -> None:
        async with self._lock:
            if self._polling:
                return
            self._polling = True
            self._source = self._pending_source or source
            self._pending_source = ""
            self._started_at = _utc_now()
            self._next_poll_at = None
            self._last_new = 0
            self._last_updated = 0
            try:
                counts = await run_agent_cycle(settings)
                self._last_new = int(counts.get("new", 0))
                self._last_updated = int(counts.get("activity_refreshed", 0))
            except Exception:
                logger.exception("Poll cycle failed")
            finally:
                self._polling = False
                self._source = ""
                self._started_at = None
                self._last_finished_at = _utc_now()
                self._next_poll_at = self._last_finished_at + timedelta(seconds=self._interval)


_scheduler = PollScheduler()


def get_poll_status() -> PollStatusView:
    _scheduler.configure(get_settings().poll_interval_minutes * 60)
    return _scheduler.snapshot()


def poll_status_payload() -> PollStatus:
    return get_poll_status().model_dump(mode="json")


async def trigger_poll_now(settings: Settings | None = None) -> None:
    await _scheduler.trigger(settings)


async def run_poll_cycle(settings: Settings | None = None) -> dict[str, int]:
    return await run_agent_cycle(settings)


async def poll_loop(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    await _scheduler.run_loop(settings)
