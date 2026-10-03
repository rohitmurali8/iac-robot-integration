"""Adapter registry: which adapters exist, and what they currently
advertise.

The mission executor asks the registry "who can do X right now" -- it
never talks to a specific adapter by name unless a mission has already
been assigned to a specific robot_id.
"""

from __future__ import annotations

from integration_layer.core.adapter_base import RobotAdapter
from integration_layer.core.capability_model import CapabilityType


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, RobotAdapter] = {}

    def register(self, adapter: RobotAdapter) -> None:
        self._adapters[adapter.robot_id] = adapter

    def unregister(self, robot_id: str) -> None:
        self._adapters.pop(robot_id, None)

    def clear(self) -> None:
        self._adapters.clear()

    def get(self, robot_id: str) -> RobotAdapter | None:
        return self._adapters.get(robot_id)

    def find_capable(self, capability_type: CapabilityType) -> list[RobotAdapter]:
        """Every robot currently advertising the requested capability.
        Orchestration core's scheduler picks among these based on
        constraints the Integration Layer doesn't know about (proximity,
        current workload, approvals) -- this method only answers "who
        could physically do this right now," not "who should."""
        capable = []
        for adapter in self._adapters.values():
            for capability in adapter.get_capabilities():
                if capability.satisfies(capability_type):
                    capable.append(adapter)
                    break
        return capable

    def all_robot_ids(self) -> list[str]:
        return list(self._adapters.keys())
