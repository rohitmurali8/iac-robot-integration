"""Mission and status schema -- the Python-native mirror of the mission
and status messages in schemas/mission.proto.

Missions are data, not code: an ordered sequence of capability invocations
with parameters, a timeout, and a retry policy. Orchestration core builds
these; the Integration Layer executes them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from integration_layer.core.capability_model import CapabilityType


class MissionStatus(str, Enum):
    QUEUED = "queued"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    PAUSED_FOR_INTERVENTION = "paused_for_intervention"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


class FailureReason(str, Enum):
    """Normalized failure vocabulary. OEM-specific error codes get mapped
    into this taxonomy inside the adapter -- orchestration core never sees
    a raw OEM error code."""

    NONE = "none"
    CONNECTIVITY_LOST = "connectivity_lost"
    PRECONDITION_FAILED = "precondition_failed"
    SAFETY_ESTOP_TRIGGERED = "safety_estop_triggered"
    LOW_BATTERY_ABORT = "low_battery_abort"
    OPERATOR_INTERVENTION_REQUESTED = "operator_intervention_requested"
    TIMEOUT = "timeout"
    ADAPTER_UNRESPONSIVE = "adapter_unresponsive"


@dataclass(frozen=True)
class MissionStep:
    capability_type: CapabilityType
    target: str
    params: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 60.0
    max_retries: int = 1


@dataclass
class Mission:
    mission_id: str
    steps: list[MissionStep]
    assigned_robot_id: str | None = None


@dataclass
class MissionStepStatus:
    mission_id: str
    step_index: int
    status: MissionStatus
    failure_reason: FailureReason = FailureReason.NONE
    detail: str = ""
    timestamp_unix_ms: int = field(default_factory=lambda: int(time.time() * 1000))
