"""The fixed adapter interface.

Every OEM adapter -- regardless of vendor, robot type, or SDK -- implements
this interface and nothing else is exposed to the rest of the Integration
Layer. Onboarding a new robot means implementing these five methods and
passing tests/test_adapter_contract.py; it does not mean touching
orchestration core, the mission executor, or any other adapter.

This is the boundary the conformance suite enforces. If a capability that
matters can't be expressed through this interface, the interface is wrong
and should change for everyone -- individual adapters should not grow
side-channel methods that only orchestration-adjacent code happens to call.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from integration_layer.core.capability_model import CapabilityDescriptor
from integration_layer.core.mission import MissionStep, MissionStepStatus


class RobotAdapter(ABC):
    """Base class every OEM/robot-type adapter must implement."""

    robot_id: str

    @abstractmethod
    def get_capabilities(self) -> list[CapabilityDescriptor]:
        """Return capabilities this robot currently advertises, given its
        present payload. Must be safe to call frequently (registry polls
        this) -- do not do slow I/O here beyond a cached/local read."""
        raise NotImplementedError

    @abstractmethod
    def execute_mission_step(
        self, mission_id: str, step_index: int, step: MissionStep
    ) -> MissionStepStatus:
        """Begin executing one mission step. Must return promptly with an
        ASSIGNED or IN_PROGRESS status -- long-running execution is tracked
        via get_status(), not by blocking this call until completion.

        mission_id/step_index are passed explicitly (rather than attached
        to MissionStep) because a step is meaningless without the mission
        context it belongs to, and get_status()/handle_exception() already
        key off this same pair -- keeping it consistent here avoids an
        adapter having to guess or stash context on the step object."""
        raise NotImplementedError

    @abstractmethod
    def get_status(self, mission_id: str, step_index: int) -> MissionStepStatus:
        """Poll current status of a step this adapter previously accepted.
        Must distinguish 'still running', 'succeeded', and every entry in
        FailureReason -- collapsing everything into a bare success/fail
        bool is the one thing this method must never do."""
        raise NotImplementedError

    @abstractmethod
    def handle_exception(self, mission_id: str, step_index: int, reason: str) -> None:
        """Called when orchestration core (or the mission executor) wants
        this adapter to abort/clean up a step, e.g. after a timeout or an
        operator intervention. Must be safe to call even if the step has
        already completed or failed on its own."""
        raise NotImplementedError

    @abstractmethod
    def estop(self) -> None:
        """Immediate stop. Must not depend on the health of anything this
        adapter is currently doing -- if execute_mission_step is hung, this
        still has to work. In the ROS2-generic adapter this delegates to
        the independent C++ safety_monitor_node rather than trying to stop
        the robot itself from within Python."""
        raise NotImplementedError
