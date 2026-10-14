"""Which customers should receive a given mission's delivered data.

A Customer is matched only when its site hosts the robot that ran the
mission AND it's subscribed to the capability type that mission
performed -- this models a real fleet-ops contract (a customer leases
specific robots at their site, and only cares about specific kinds of
work those robots do), not a blanket "notify everyone" rule.

Static, in-memory registry seeded at startup -- same pattern as
AdapterRegistry (integration_layer/core/registry.py). No runtime
registration UI; adding/removing customers is a deployment-time concern
in this PoC, matching the plan's explicit non-goal.
"""

from __future__ import annotations

from dataclasses import dataclass

from integration_layer.core.capability_model import CapabilityType


@dataclass(frozen=True)
class Customer:
    customer_id: str
    webhook_url: str
    subscribed_capabilities: frozenset[CapabilityType]
    site_robot_ids: frozenset[str]


class CustomerRegistry:
    def __init__(self) -> None:
        self._customers: dict[str, Customer] = {}

    def register(self, customer: Customer) -> None:
        self._customers[customer.customer_id] = customer

    def all_customers(self) -> list[Customer]:
        return list(self._customers.values())

    def customers_for(self, robot_id: str, capability_type: CapabilityType) -> list[Customer]:
        """Customers whose site_robot_ids contains robot_id AND whose
        subscribed_capabilities contains capability_type."""
        return [
            customer
            for customer in self._customers.values()
            if robot_id in customer.site_robot_ids and capability_type in customer.subscribed_capabilities
        ]

    def customers_for_mission(self, robot_id: str, capability_types: set[CapabilityType]) -> list[Customer]:
        """Union of customers_for() across every distinct capability type
        a (possibly mixed-step) mission performed, deduplicated by
        customer_id. NAVIGATE is skipped -- it has no deliverable
        product, so no customer is ever subscribed to it meaningfully."""
        matched: dict[str, Customer] = {}
        for capability_type in capability_types:
            if capability_type == CapabilityType.NAVIGATE:
                continue
            for customer in self.customers_for(robot_id, capability_type):
                matched[customer.customer_id] = customer
        return list(matched.values())

    def clear(self) -> None:
        self._customers.clear()
