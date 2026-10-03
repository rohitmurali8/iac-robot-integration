"""Audit event schema.

Every state transition, operator intervention, and safety trigger gets
recorded here. This is both the compliance record and, per DESIGN.md
section 5, the primary tool for debugging field failures after the fact.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class AuditEvent:
    mission_id: str
    robot_id: str
    event_type: str  # e.g. "step_started", "operator_intervention", "estop_triggered"
    detail: str = ""
    timestamp_unix_ms: int = field(default_factory=lambda: int(time.time() * 1000))


class AuditLog:
    """In-memory audit sink for the scaffold. A real deployment would back
    this with an append-only store (e.g. Postgres or a log stream) so the
    audit trail survives process restarts -- kept in-memory here since this
    code exists to demonstrate the event shape and call sites, not to be a
    production log store."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self._events.append(event)

    def for_mission(self, mission_id: str) -> list[AuditEvent]:
        return [e for e in self._events if e.mission_id == mission_id]

    def all_events(self) -> list[AuditEvent]:
        return list(self._events)

    def clear(self) -> None:
        self._events.clear()
