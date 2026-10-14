"""Tests for CustomerRegistry -- which customers should receive a given
mission's delivered data. A customer is matched only when BOTH its site
hosts the robot that ran the mission AND it's subscribed to the
capability type that mission performed -- modeling a real fleet-ops
contract (a customer leases specific robots, and only cares about
specific kinds of work)."""

from __future__ import annotations

from integration_layer.core.capability_model import CapabilityType
from integration_layer.core.customer_registry import Customer, CustomerRegistry


def _customer(customer_id, capabilities, robot_ids, webhook_url="http://example.invalid/webhook"):
    return Customer(
        customer_id=customer_id,
        webhook_url=webhook_url,
        subscribed_capabilities=frozenset(capabilities),
        site_robot_ids=frozenset(robot_ids),
    )


def test_customers_for_matches_on_robot_and_capability_intersection():
    registry = CustomerRegistry()
    registry.register(_customer("acme", [CapabilityType.INSPECT_THERMAL], ["spot-01"]))

    matches = registry.customers_for("spot-01", CapabilityType.INSPECT_THERMAL)

    assert [c.customer_id for c in matches] == ["acme"]


def test_customers_for_excludes_wrong_robot():
    registry = CustomerRegistry()
    registry.register(_customer("acme", [CapabilityType.INSPECT_THERMAL], ["spot-01"]))

    matches = registry.customers_for("fleetx-01", CapabilityType.INSPECT_THERMAL)

    assert matches == []


def test_customers_for_excludes_wrong_capability():
    registry = CustomerRegistry()
    registry.register(_customer("acme", [CapabilityType.INSPECT_THERMAL], ["spot-01"]))

    matches = registry.customers_for("spot-01", CapabilityType.DETECT_GAS)

    assert matches == []


def test_customers_for_mission_unions_and_dedupes_across_capability_types():
    registry = CustomerRegistry()
    registry.register(_customer("acme", [CapabilityType.INSPECT_VISUAL, CapabilityType.INSPECT_THERMAL], ["spot-01"]))
    registry.register(_customer("beta", [CapabilityType.INSPECT_THERMAL], ["spot-01"]))

    matches = registry.customers_for_mission(
        "spot-01", {CapabilityType.NAVIGATE, CapabilityType.INSPECT_VISUAL, CapabilityType.INSPECT_THERMAL}
    )

    # acme is subscribed to BOTH capability types present but must appear once.
    assert sorted(c.customer_id for c in matches) == ["acme", "beta"]


def test_customers_for_mission_ignores_navigate_only():
    registry = CustomerRegistry()
    registry.register(_customer("acme", [CapabilityType.INSPECT_VISUAL], ["spot-01"]))

    matches = registry.customers_for_mission("spot-01", {CapabilityType.NAVIGATE})

    assert matches == []


def test_clear_removes_all_customers():
    registry = CustomerRegistry()
    registry.register(_customer("acme", [CapabilityType.INSPECT_VISUAL], ["spot-01"]))

    registry.clear()

    assert registry.all_customers() == []
