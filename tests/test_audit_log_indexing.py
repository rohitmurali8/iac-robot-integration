"""Tests for AuditLog indexing behavior (Task 3).

Validates that:
- The indexed version is O(k) (k=matching events), not O(n) full-log scans
- Behavior is identical to the old flat-scan implementation
- Empty mission_id/robot_id values do not pollute indices
- Clear() empties all three structures (event list + indices)
"""

import pytest
from integration_layer.core.events import AuditLog, AuditEvent


class TestForMissionIndexing:
    """Tests that for_mission() returns correct events using the index."""

    def test_for_mission_returns_only_that_missions_events_in_order(self) -> None:
        """Record events for two different mission_ids interleaved, assert
        for_mission() returns only the matching ones, in the order they
        were recorded."""
        log = AuditLog()

        # Record events for two missions interleaved
        e1 = AuditEvent(mission_id="mission_A", robot_id="robot_1", event_type="step_started")
        e2 = AuditEvent(mission_id="mission_B", robot_id="robot_2", event_type="step_started")
        e3 = AuditEvent(mission_id="mission_A", robot_id="robot_1", event_type="step_completed")
        e4 = AuditEvent(mission_id="mission_B", robot_id="robot_2", event_type="step_completed")
        e5 = AuditEvent(mission_id="mission_A", robot_id="robot_1", event_type="operator_intervention")

        log.record(e1)
        log.record(e2)
        log.record(e3)
        log.record(e4)
        log.record(e5)

        # Assert for_mission("mission_A") returns only mission_A events in order
        mission_a_events = log.for_mission("mission_A")
        assert mission_a_events == [e1, e3, e5]
        assert len(mission_a_events) == 3

        # Assert for_mission("mission_B") returns only mission_B events in order
        mission_b_events = log.for_mission("mission_B")
        assert mission_b_events == [e2, e4]
        assert len(mission_b_events) == 2

    def test_for_mission_behavior_unchanged_from_flat_scan(self) -> None:
        """Key regression test: assert for_mission(x) returns the exact same
        list (same events, same order) as the old O(n) scan would have."""
        log = AuditLog()

        # Build log with mix of events across several missions
        e1 = AuditEvent(mission_id="m1", robot_id="r1", event_type="start")
        e2 = AuditEvent(mission_id="m2", robot_id="r2", event_type="start")
        e3 = AuditEvent(mission_id="m1", robot_id="r2", event_type="progress")
        e4 = AuditEvent(mission_id="m3", robot_id="r1", event_type="start")
        e5 = AuditEvent(mission_id="m2", robot_id="r1", event_type="complete")

        log.record(e1)
        log.record(e2)
        log.record(e3)
        log.record(e4)
        log.record(e5)

        # Manually compute what the old flat-scan would return
        all_events = log.all_events()

        for mission_id in ["m1", "m2", "m3"]:
            # Old flat-scan approach
            old_result = [e for e in all_events if e.mission_id == mission_id]
            # New indexed approach
            new_result = log.for_mission(mission_id)
            assert new_result == old_result, f"Results differ for mission_id={mission_id}"


class TestForRobotIndexing:
    """Tests that for_robot() returns correct events using the index."""

    def test_for_robot_returns_only_that_robots_events_across_missions(self) -> None:
        """Record events for the same robot_id across two different mission_ids,
        plus events for a different robot_id. Assert for_robot() returns only
        the first robot's events across both missions, in recorded order."""
        log = AuditLog()

        # Record events: robot_1 in mission_A, robot_2 in mission_B, robot_1 in mission_B
        e1 = AuditEvent(mission_id="mission_A", robot_id="robot_1", event_type="step_started")
        e2 = AuditEvent(mission_id="mission_B", robot_id="robot_2", event_type="step_started")
        e3 = AuditEvent(mission_id="mission_B", robot_id="robot_1", event_type="step_completed")
        e4 = AuditEvent(mission_id="mission_A", robot_id="robot_2", event_type="operator_intervention")
        e5 = AuditEvent(mission_id="mission_A", robot_id="robot_1", event_type="idle")

        log.record(e1)
        log.record(e2)
        log.record(e3)
        log.record(e4)
        log.record(e5)

        # Assert for_robot("robot_1") returns only robot_1's events across all missions
        robot_1_events = log.for_robot("robot_1")
        assert robot_1_events == [e1, e3, e5]

        # Assert for_robot("robot_2") returns only robot_2's events
        robot_2_events = log.for_robot("robot_2")
        assert robot_2_events == [e2, e4]


class TestMissionIdsForRobotIndexing:
    """Tests that mission_ids_for_robot() returns distinct mission IDs in
    first-seen order."""

    def test_mission_ids_for_robot_returns_distinct_ids_in_first_seen_order(self) -> None:
        """Record multiple events for the same mission_id for a robot (should
        collapse to one entry), then events for a second mission_id. Assert
        the result is [first_mission_id, second_mission_id] -- distinct,
        first-seen order, not sorted alphabetically/numerically."""
        log = AuditLog()

        # Record multiple events for robot_1 in mission_Z (first-seen)
        e1 = AuditEvent(mission_id="mission_Z", robot_id="robot_1", event_type="start")
        e2 = AuditEvent(mission_id="mission_Z", robot_id="robot_1", event_type="progress")
        e3 = AuditEvent(mission_id="mission_Z", robot_id="robot_1", event_type="more_progress")

        # Then record events for robot_1 in mission_A (second mission seen)
        e4 = AuditEvent(mission_id="mission_A", robot_id="robot_1", event_type="start")
        e5 = AuditEvent(mission_id="mission_A", robot_id="robot_1", event_type="end")

        log.record(e1)
        log.record(e2)
        log.record(e3)
        log.record(e4)
        log.record(e5)

        # Assert result is [mission_Z, mission_A] in first-seen order
        # NOT [mission_A, mission_Z] (sorted alphabetically)
        mission_ids = log.mission_ids_for_robot("robot_1")
        assert mission_ids == ["mission_Z", "mission_A"]

        # Verify they are distinct (no duplicates despite multiple events per mission)
        assert len(mission_ids) == len(set(mission_ids))


class TestEmptyValuesHandling:
    """Tests that empty mission_id and robot_id do not pollute indices."""

    def test_record_with_empty_mission_id_does_not_pollute_index(self) -> None:
        """Record an event with mission_id="" (mirroring emergency_stop's real
        call pattern), assert for_mission("") returns [] (not the event) --
        proves the empty-string guard works."""
        log = AuditLog()

        # Record an event with empty mission_id (like emergency_stop does)
        e1 = AuditEvent(mission_id="", robot_id="robot_1", event_type="estop_triggered")
        log.record(e1)

        # Assert for_mission("") returns empty list, not [e1]
        result = log.for_mission("")
        assert result == []

        # Also verify the event is still in all_events() (it's recorded, just not indexed)
        all_events = log.all_events()
        assert e1 in all_events

    def test_record_with_empty_robot_id_does_not_pollute_index(self) -> None:
        """Record an event with robot_id="" and verify it's not indexed by robot."""
        log = AuditLog()

        # Record an event with empty robot_id
        e1 = AuditEvent(mission_id="mission_A", robot_id="", event_type="event_type")
        log.record(e1)

        # Assert for_robot("") returns empty list
        result = log.for_robot("")
        assert result == []

        # Event should still be in all_events()
        all_events = log.all_events()
        assert e1 in all_events


class TestWhiteBoxIndexPopulation:
    """White-box tests that directly inspect the internal index structures."""

    def test_record_populates_indices_directly(self) -> None:
        """White-box check: after calling record() once, assert log._by_mission
        and log._by_robot actually contain the event under the right keys --
        proves the indices are genuinely being populated."""
        log = AuditLog()

        event = AuditEvent(mission_id="mission_X", robot_id="robot_Y", event_type="test_event")
        log.record(event)

        # White-box: directly inspect _by_mission
        assert "mission_X" in log._by_mission
        assert log._by_mission["mission_X"] == [event]

        # White-box: directly inspect _by_robot
        assert "robot_Y" in log._by_robot
        assert log._by_robot["robot_Y"] == [event]

    def test_clear_empties_all_three_structures(self) -> None:
        """Populate, call clear(), assert all_events(), for_mission(...),
        for_robot(...) are all empty afterward, and (white-box) that
        _by_mission/_by_robot dicts are actually empty."""
        log = AuditLog()

        # Populate with some events
        e1 = AuditEvent(mission_id="m1", robot_id="r1", event_type="event1")
        e2 = AuditEvent(mission_id="m2", robot_id="r2", event_type="event2")
        log.record(e1)
        log.record(e2)

        # Verify they're in place
        assert len(log.all_events()) == 2
        assert log.for_mission("m1") == [e1]
        assert log.for_robot("r1") == [e1]

        # Clear
        log.clear()

        # Verify all three structures are empty
        assert log.all_events() == []
        assert log.for_mission("m1") == []
        assert log.for_robot("r1") == []

        # White-box: verify internal structures are empty, not just lookup-empty
        assert len(log._events) == 0
        assert len(log._by_mission) == 0
        assert len(log._by_robot) == 0
