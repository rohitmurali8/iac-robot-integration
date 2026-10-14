"""Post-mission data packaging, upload, and customer delivery.

Two sequential, independently retried network-facing phases -- both
reusing the EXISTING retry_policy table, never a bespoke retry loop:

1. Upload (robot -> platform): simulates the robot batch-uploading its
   locally-captured artifacts to the platform. Consults the mission's
   OWN FaultProfile (the same one already driving mission-step fault
   injection) to decide per-attempt success, retried via
   retry_policy.strategy_for(FailureReason.CONNECTIVITY_LOST) (COMMS: 4
   attempts). Exhausting retries returns DeliveryRecord(UPLOAD_FAILED)
   immediately -- no package, no customer push. This is the
   "bandwidth-limited site" story: a mission can COMPLETE while its data
   never makes it off the robot.

2. Package + push (platform -> customer): on upload success, packages a
   zip and does a REAL local HTTP POST to each subscribed customer's
   webhook_url, retried via the SAME retry_policy. A customer whose
   webhook is unreachable after retries doesn't block delivery to other
   customers -- each push is independent.

deliver() is called by MissionExecutor (a later task) ONLY when a
mission's final status is COMPLETED -- FAILED/ABORTED/PAUSED_FOR_INTERVENTION
missions never reach this module at all.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from integration_layer.core import retry_policy
from integration_layer.core.artifact_capture import CapturedArtifact
from integration_layer.core.customer_registry import CustomerRegistry
from integration_layer.core.mission import FailureReason, Mission, MissionStepStatus, SystemModule
from integration_layer.core.mission_analytics import RobotStats
from integration_layer.core.registry import AdapterRegistry


class DeliveryStatus(str, Enum):
    PENDING = "pending"
    UPLOAD_FAILED = "upload_failed"
    NO_SUBSCRIBERS = "no_subscribers"
    DELIVERED = "delivered"
    PARTIALLY_DELIVERED = "partially_delivered"
    PUSH_FAILED = "push_failed"


@dataclass(frozen=True)
class DeliveryRecord:
    mission_id: str
    status: DeliveryStatus
    package_path: Path | None
    delivered_customer_ids: list[str]
    failed_customer_ids: list[str]


class DeliveryPackager:
    def __init__(self, output_dir: Path) -> None:
        self._output_dir = Path(output_dir)

    def build_package(
        self,
        mission: Mission,
        final_status: MissionStepStatus,
        step_history: list[MissionStepStatus],
        artifacts: list[CapturedArtifact],
        retry_counts_by_module: dict[SystemModule, int],
        robot_stats_snapshot: RobotStats,
    ) -> Path:
        """Writes <output_dir>/mission-report-<mission_id>.zip containing
        mission_report.json (steps, final status, retry tally, a robot
        analytics snapshot) + one file per CapturedArtifact."""
        self._output_dir.mkdir(parents=True, exist_ok=True)
        package_path = self._output_dir / f"mission-report-{mission.mission_id}.zip"

        manifest = {
            "mission_id": mission.mission_id,
            "robot_id": mission.assigned_robot_id,
            "final_status": final_status.status.value,
            "failure_reason": final_status.failure_reason.value,
            "steps": [
                {
                    "step_index": s.step_index,
                    "status": s.status.value,
                    "failure_reason": s.failure_reason.value,
                    "detail": s.detail,
                }
                for s in step_history
            ],
            "retries_by_module": {module.value: count for module, count in retry_counts_by_module.items()},
            "artifact_filenames": [a.filename for a in artifacts],
            "robot_stats_snapshot": {
                "robot_id": robot_stats_snapshot.robot_id,
                "missions_run": robot_stats_snapshot.missions_run,
                "success_rate": robot_stats_snapshot.success_rate,
            },
        }

        with zipfile.ZipFile(package_path, "w") as zf:
            zf.writestr("mission_report.json", json.dumps(manifest, indent=2))
            for artifact in artifacts:
                zf.writestr(artifact.filename, artifact.content)

        return package_path


class DeliveryService:
    def __init__(
        self,
        registry: AdapterRegistry,
        customer_registry: CustomerRegistry,
        output_dir: Path,
        http_open: Callable = urllib.request.urlopen,
    ) -> None:
        self._registry = registry
        self._customer_registry = customer_registry
        self._packager = DeliveryPackager(output_dir)
        self._http_open = http_open

    def deliver(
        self,
        mission: Mission,
        final_status: MissionStepStatus,
        step_history: list[MissionStepStatus],
        artifacts: list[CapturedArtifact],
        retry_counts_by_module: dict[SystemModule, int],
        robot_stats_snapshot: RobotStats,
    ) -> DeliveryRecord:
        if not self._upload_artifacts(mission, artifacts):
            return DeliveryRecord(
                mission_id=mission.mission_id,
                status=DeliveryStatus.UPLOAD_FAILED,
                package_path=None,
                delivered_customer_ids=[],
                failed_customer_ids=[],
            )

        package_path = self._packager.build_package(
            mission, final_status, step_history, artifacts, retry_counts_by_module, robot_stats_snapshot
        )

        capability_types = {step.capability_type for step in mission.steps}
        customers = self._customer_registry.customers_for_mission(mission.assigned_robot_id, capability_types)
        if not customers:
            return DeliveryRecord(
                mission_id=mission.mission_id,
                status=DeliveryStatus.NO_SUBSCRIBERS,
                package_path=package_path,
                delivered_customer_ids=[],
                failed_customer_ids=[],
            )

        delivered: list[str] = []
        failed: list[str] = []
        for customer in customers:
            if self._push_to_customer(customer, mission, package_path):
                delivered.append(customer.customer_id)
            else:
                failed.append(customer.customer_id)

        if failed and not delivered:
            status = DeliveryStatus.PUSH_FAILED
        elif failed:
            status = DeliveryStatus.PARTIALLY_DELIVERED
        else:
            status = DeliveryStatus.DELIVERED

        return DeliveryRecord(
            mission_id=mission.mission_id,
            status=status,
            package_path=package_path,
            delivered_customer_ids=delivered,
            failed_customer_ids=failed,
        )

    def _upload_artifacts(self, mission: Mission, artifacts: list[CapturedArtifact]) -> bool:
        """Simulates the robot batch-uploading `artifacts` to the
        platform. No artifacts (an all-NAVIGATE mission) is trivially
        successful -- there's nothing to upload. Consults the mission's
        own FaultProfile via the registered adapter's get_fault_profile()
        -- the concrete-adapter method every adapter in this codebase
        exposes, called the same way webapp/app.py's _fleet_snapshot()
        already does."""
        if not artifacts:
            return True

        adapter = self._registry.get(mission.assigned_robot_id)
        fault_profile = adapter.get_fault_profile()
        strategy = retry_policy.strategy_for(FailureReason.CONNECTIVITY_LOST)  # COMMS strategy

        attempt = 1
        while True:
            failed = fault_profile.offline or fault_profile.consume_flaky_failure()
            if not failed:
                return True
            if attempt >= strategy.max_attempts:
                return False
            attempt += 1

    def _push_to_customer(self, customer, mission: Mission, package_path: Path) -> bool:
        strategy = retry_policy.strategy_for(FailureReason.CONNECTIVITY_LOST)  # COMMS strategy
        payload = json.dumps(
            {
                "mission_id": mission.mission_id,
                "robot_id": mission.assigned_robot_id,
                "customer_id": customer.customer_id,
                "package_filename": package_path.name,
            }
        ).encode()

        attempt = 1
        while True:
            try:
                request = urllib.request.Request(
                    customer.webhook_url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
                )
                with self._http_open(request, timeout=2.0) as response:
                    if 200 <= response.status < 300:
                        return True
            except (urllib.error.URLError, OSError):
                pass
            if attempt >= strategy.max_attempts:
                return False
            attempt += 1
