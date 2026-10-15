"""JSON wire format for the one place in this codebase where a
RobotAdapter call actually crosses a real process boundary:
RemoteRobotAdapter (talking to a Kubernetes-deployed robot worker pod
over HTTP) and fleet_worker/app.py (the pod-side process it talks to).

Every adapter elsewhere in this repo (mock_robot/) is driven in-process
-- no serialization needed. This module exists solely for that one
real network boundary, the same reason schemas/mission.proto exists at
the gRPC-boundary layer the rest of this repo's Python core mirrors by
hand -- kept out of mission.py/capability_model.py themselves so those
stay pure data model with no transport concerns mixed in.
"""

from __future__ import annotations

from integration_layer.core.capability_model import CapabilityDescriptor, CapabilityType
from integration_layer.core.mission import FailureReason, MissionStatus, MissionStep, MissionStepStatus
from mock_robot.fault_injection import FaultProfile


def capability_to_dict(capability: CapabilityDescriptor) -> dict:
    return {
        "capability_type": capability.capability_type.value,
        "required_payload": capability.required_payload,
        "params_schema": capability.params_schema,
        "expected_duration_s_range": list(capability.expected_duration_s_range),
    }


def capability_from_dict(data: dict) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        capability_type=CapabilityType(data["capability_type"]),
        required_payload=data.get("required_payload"),
        params_schema=data.get("params_schema", {}),
        expected_duration_s_range=tuple(data.get("expected_duration_s_range", (0.0, 0.0))),
    )


def step_to_dict(step: MissionStep) -> dict:
    return {
        "capability_type": step.capability_type.value,
        "target": step.target,
        "params": step.params,
        "timeout_s": step.timeout_s,
        "max_retries": step.max_retries,
    }


def step_from_dict(data: dict) -> MissionStep:
    return MissionStep(
        capability_type=CapabilityType(data["capability_type"]),
        target=data["target"],
        params=data.get("params", {}),
        timeout_s=data.get("timeout_s", 60.0),
        max_retries=data.get("max_retries", 1),
    )


def status_to_dict(status: MissionStepStatus) -> dict:
    return {
        "mission_id": status.mission_id,
        "step_index": status.step_index,
        "status": status.status.value,
        "failure_reason": status.failure_reason.value,
        "detail": status.detail,
        "timestamp_unix_ms": status.timestamp_unix_ms,
    }


def status_from_dict(data: dict) -> MissionStepStatus:
    return MissionStepStatus(
        mission_id=data["mission_id"],
        step_index=data["step_index"],
        status=MissionStatus(data["status"]),
        failure_reason=FailureReason(data.get("failure_reason", "none")),
        detail=data.get("detail", ""),
        timestamp_unix_ms=data.get("timestamp_unix_ms", 0),
    )


def fault_profile_to_dict(profile: FaultProfile) -> dict:
    return {
        "offline": profile.offline,
        "extra_latency_polls": profile.extra_latency_polls,
        "flaky_failures_remaining": profile.flaky_failures_remaining,
        "failed_sensors": [c.value for c in profile.failed_sensors],
        "random_failure_rates": {reason.value: rate for reason, rate in profile.random_failure_rates.items()},
        "rng_seed": profile.rng_seed,
    }


def fault_profile_from_dict(data: dict) -> FaultProfile:
    return FaultProfile(
        offline=data.get("offline", False),
        extra_latency_polls=data.get("extra_latency_polls", 0),
        flaky_failures_remaining=data.get("flaky_failures_remaining", 0),
        failed_sensors={CapabilityType(c) for c in data.get("failed_sensors", [])},
        random_failure_rates={
            FailureReason(reason): rate for reason, rate in data.get("random_failure_rates", {}).items()
        },
        rng_seed=data.get("rng_seed"),
    )
