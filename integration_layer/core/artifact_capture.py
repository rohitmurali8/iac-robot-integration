"""Local, no-network simulation of a robot capturing sensor data to its
own disk as a mission step completes.

Deliberately has zero dependency on networking or the platform side of
delivery -- MissionExecutor calls capture() the instant a sensor-
capability step's status transitions to COMPLETED (never for NAVIGATE,
which produces no deliverable artifact), and the resulting bytes sit in
memory until DeliveryService's upload phase (data_delivery.py, a later
task) drains and batch-uploads them once the WHOLE mission completes.
This split is what lets per-step capture be unconditional and fast while
the only network-dependent, retry-able operation is the single batch
upload at mission end."""

from __future__ import annotations

from dataclasses import dataclass

from integration_layer.core.capability_model import CapabilityType

SENSOR_CAPABILITIES = {
    CapabilityType.INSPECT_VISUAL,
    CapabilityType.INSPECT_THERMAL,
    CapabilityType.DETECT_GAS,
}


@dataclass(frozen=True)
class CapturedArtifact:
    step_index: int
    capability_type: CapabilityType
    filename: str
    content: bytes  # simulated payload -- explicitly illustrative, not a real sensor frame


class ArtifactCapture:
    def __init__(self) -> None:
        self._by_mission: dict[str, list[CapturedArtifact]] = {}

    def capture(self, mission_id: str, step_index: int, capability_type: CapabilityType) -> CapturedArtifact:
        filename = f"step_{step_index}_{capability_type.value}.txt"
        content = (
            f"simulated {capability_type.value} artifact -- mission {mission_id}, step {step_index}\n"
        ).encode()
        artifact = CapturedArtifact(
            step_index=step_index, capability_type=capability_type, filename=filename, content=content
        )
        self._by_mission.setdefault(mission_id, []).append(artifact)
        return artifact

    def drain(self, mission_id: str) -> list[CapturedArtifact]:
        """Returns and clears every artifact captured for mission_id so
        far. Returns [] (not an error) for a mission with no captured
        artifacts -- a mission made entirely of NAVIGATE steps is a
        normal, benign case."""
        return self._by_mission.pop(mission_id, [])
