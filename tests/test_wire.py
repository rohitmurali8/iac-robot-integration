"""Round-trip tests for integration_layer/core/wire.py -- the JSON wire
format RemoteRobotAdapter and fleet_worker/app.py use to cross the one
real process boundary in this codebase. Deliberately just round-trip
correctness (to_dict then from_dict reproduces the original) rather
than testing the HTTP transport itself, which test_remote_adapter.py
covers against a real running fleet_worker process."""

from __future__ import annotations

from integration_layer.core.capability_model import CapabilityDescriptor, CapabilityType
from integration_layer.core.mission import FailureReason, MissionStatus, MissionStep, MissionStepStatus
from integration_layer.core.wire import (
    capability_from_dict,
    capability_to_dict,
    status_from_dict,
    status_to_dict,
    step_from_dict,
    step_to_dict,
)


def test_capability_round_trip():
    original = CapabilityDescriptor(
        capability_type=CapabilityType.INSPECT_THERMAL,
        required_payload="thermal_camera",
        params_schema={"resolution": "string"},
        expected_duration_s_range=(1.5, 4.0),
    )

    result = capability_from_dict(capability_to_dict(original))

    assert result == original


def test_capability_round_trip_with_none_required_payload():
    original = CapabilityDescriptor(capability_type=CapabilityType.NAVIGATE)

    result = capability_from_dict(capability_to_dict(original))

    assert result.required_payload is None


def test_step_round_trip():
    original = MissionStep(
        capability_type=CapabilityType.DETECT_GAS,
        target="wp7",
        params={"zone": "north"},
        timeout_s=45.0,
        max_retries=3,
    )

    result = step_from_dict(step_to_dict(original))

    assert result == original


def test_status_round_trip_with_failure_reason():
    original = MissionStepStatus(
        mission_id="m-1",
        step_index=2,
        status=MissionStatus.FAILED,
        failure_reason=FailureReason.CONNECTIVITY_LOST,
        detail="worker pod unreachable",
        timestamp_unix_ms=1234567890,
    )

    result = status_from_dict(status_to_dict(original))

    assert result == original


def test_status_round_trip_default_none_failure_reason():
    original = MissionStepStatus(mission_id="m-2", step_index=0, status=MissionStatus.COMPLETED)

    result = status_from_dict(status_to_dict(original))

    assert result.failure_reason == FailureReason.NONE
