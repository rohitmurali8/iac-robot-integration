"""Capability model.

A capability is a named unit of physical work a robot can perform, with a
declared parameter schema. Robots advertise capabilities dynamically based
on current payload -- this is what lets one adapter class serve a bare
quadruped and one with a thermal camera bolted on without becoming two
adapters.

This module is the Python-native mirror of schemas/mission.proto's
CapabilityType/CapabilityDescriptor. In a real build these would be
generated from the .proto; kept hand-written here so the scaffold runs
without a protoc toolchain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class CapabilityType(str, Enum):
    NAVIGATE = "navigate"
    INSPECT_VISUAL = "inspect_visual"
    INSPECT_THERMAL = "inspect_thermal"
    DETECT_GAS = "detect_gas"
    MANIPULATE = "manipulate"  # scoped, not fully specified in v0
    WELD = "weld"              # supervised-only in v0


@dataclass(frozen=True)
class CapabilityDescriptor:
    """What a robot currently advertises. Recomputed as payload, battery,
    or location change -- not a static fact baked into the adapter."""

    capability_type: CapabilityType
    required_payload: str | None = None
    params_schema: dict[str, str] = field(default_factory=dict)
    expected_duration_s_range: tuple[float, float] = (0.0, 0.0)

    def satisfies(self, requested: CapabilityType) -> bool:
        return self.capability_type == requested
