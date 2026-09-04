from __future__ import annotations

from datetime import datetime
from typing import TypedDict


class SweepStatus(TypedDict):
    phase: str
    query: str
    next_at: str | None
    inbox_rev: int


_state: SweepStatus = {"phase": "idle", "query": "", "next_at": None, "inbox_rev": 0}


def set_sweep(*, phase: str, query: str = "", next_at: datetime | None = None) -> None:
    global _state
    iso = next_at.isoformat() if next_at is not None else None
    _state = {
        "phase": phase,
        "query": query,
        "next_at": iso,
        "inbox_rev": _state["inbox_rev"],
    }


def bump_inbox_rev() -> int:
    global _state
    _state = {**_state, "inbox_rev": _state["inbox_rev"] + 1}
    return _state["inbox_rev"]


def clear_sweep() -> None:
    set_sweep(phase="idle")


def sweep_snapshot() -> SweepStatus:
    return {
        "phase": _state["phase"],
        "query": _state["query"],
        "next_at": _state["next_at"],
        "inbox_rev": _state["inbox_rev"],
    }
