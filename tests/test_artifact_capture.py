"""Tests for ArtifactCapture -- purely local, in-memory simulation of a
robot capturing sensor data to its own disk as a step completes. No
network, no I/O to any platform -- that happens later, in
DeliveryService's upload phase (test_data_delivery.py)."""

from __future__ import annotations

from integration_layer.core.artifact_capture import SENSOR_CAPABILITIES, ArtifactCapture
from integration_layer.core.capability_model import CapabilityType


def test_sensor_capabilities_excludes_navigate():
    assert CapabilityType.NAVIGATE not in SENSOR_CAPABILITIES
    assert CapabilityType.INSPECT_VISUAL in SENSOR_CAPABILITIES
    assert CapabilityType.INSPECT_THERMAL in SENSOR_CAPABILITIES
    assert CapabilityType.DETECT_GAS in SENSOR_CAPABILITIES


def test_capture_returns_artifact_with_content():
    capture = ArtifactCapture()

    artifact = capture.capture("mission-1", 2, CapabilityType.INSPECT_THERMAL)

    assert artifact.step_index == 2
    assert artifact.capability_type == CapabilityType.INSPECT_THERMAL
    assert artifact.filename
    assert isinstance(artifact.content, bytes)
    assert len(artifact.content) > 0


def test_drain_returns_all_captured_artifacts_for_that_mission_only():
    capture = ArtifactCapture()
    capture.capture("mission-1", 0, CapabilityType.INSPECT_VISUAL)
    capture.capture("mission-1", 1, CapabilityType.INSPECT_THERMAL)
    capture.capture("mission-2", 0, CapabilityType.DETECT_GAS)

    drained = capture.drain("mission-1")

    assert [a.step_index for a in drained] == [0, 1]
    # mission-2's artifact must be untouched.
    other = capture.drain("mission-2")
    assert len(other) == 1


def test_drain_clears_state_so_a_second_drain_is_empty():
    capture = ArtifactCapture()
    capture.capture("mission-1", 0, CapabilityType.INSPECT_VISUAL)

    capture.drain("mission-1")
    second_drain = capture.drain("mission-1")

    assert second_drain == []


def test_drain_unknown_mission_returns_empty_list_not_error():
    capture = ArtifactCapture()

    assert capture.drain("never-captured") == []
