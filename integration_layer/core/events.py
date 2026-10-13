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
    production log store.

    Indexed by mission_id and robot_id at write time (record()), not
    scanned at read time -- for_mission()/for_robot() are O(k) (k = the
    number of relevant events), not O(n) full-log scans. This matters
    once dashboard/analytics/LLM-query reads happen far more often than
    audit writes."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._by_mission: dict[str, list[AuditEvent]] = {}
        self._by_robot: dict[str, list[AuditEvent]] = {}

    def record(self, event: AuditEvent) -> None:
        self._events.append(event)
        if event.mission_id:
            self._by_mission.setdefault(event.mission_id, []).append(event)
        if event.robot_id:
            self._by_robot.setdefault(event.robot_id, []).append(event)

    def for_mission(self, mission_id: str) -> list[AuditEvent]:
        return list(self._by_mission.get(mission_id, []))

    def for_robot(self, robot_id: str) -> list[AuditEvent]:
        """All events this robot has ever been involved in, across every
        mission, in the order they were recorded. The index behind the
        per-robot 'Mission Log' view added in a later GUI task."""
        return list(self._by_robot.get(robot_id, []))

    def mission_ids_for_robot(self, robot_id: str) -> list[str]:
        """Distinct mission IDs this robot has participated in, in
        first-seen order (not sorted -- first-seen is the natural,
        cheap-to-compute order and matches how a human would expect a
        'recent activity' list to read)."""
        seen: dict[str, None] = {}  # dict, not set, to preserve insertion order
        for event in self._by_robot.get(robot_id, []):
            if event.mission_id:
                seen[event.mission_id] = None
        return list(seen.keys())

    def all_events(self) -> list[AuditEvent]:
        return list(self._events)

    def clear(self) -> None:
        self._events.clear()
        self._by_mission.clear()
        self._by_robot.clear()
