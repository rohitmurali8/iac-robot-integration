"""Deterministic + probabilistic fault profiles for both OEM adapters.

Deterministic primitives (offline, flaky countdown, sensor-failure
toggle, extra latency) are reproducible by construction -- "N consecutive
transient failures then success" is a countdown, not a coin flip, and
latency is simulated as extra get_status() poll rounds, not wall-clock
time.sleep(), so tests stay fast by default.

Probabilistic primitives (random_failure_rates) exist for conditions that
are genuinely unpredictable in the field. They use a SEEDED random.Random
instance -- an explicit rng_seed makes the exact sequence of outcomes
reproducible (for tests and scripted demos); rng_seed=None uses real
entropy for genuine unpredictability in interactive/demo use. Both paths
ultimately produce a FailureReason -- the retry policy in retry_policy.py
doesn't care whether a failure was deterministic or random, it only reacts
to the FailureReason.

Shared here (not duplicated per-adapter) because, unlike OEM wire-level
mechanics (session tokens, job IDs, battery-field naming) which
deliberately stay adapter-specific per DESIGN.md's "OEM-specific vs.
shared platform" split, fault-injection semantics ARE platform-level: the
*shape* of "offline," "flaky N times then recovers," "sensor down," and
"random failure rate" is identical in spirit for every adapter and should
not be reinvented per OEM -- only *consulted* differently inside each
adapter's own execute_mission_step()/get_status() per its own polling
shape (a later task wires this consultation into both adapters).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from integration_layer.core.capability_model import CapabilityType
from integration_layer.core.mission import FailureReason


@dataclass
class FaultProfile:
    """Attached to (or toggled on) a single adapter instance. All fields
    default to "healthy" so existing adapter construction with no
    FaultProfile behaves exactly as today."""

    # -- deterministic, reproducible --

    # Fully unreachable: every execute_mission_step()/get_status() call
    # for a NEW step immediately returns FAILED/CONNECTIVITY_LOST. Does
    # NOT retroactively affect steps already in flight/terminal.
    offline: bool = False

    # Extra get_status() poll rounds injected before a step that would
    # otherwise resolve is allowed to resolve -- simulates added latency
    # deterministically (a count, not a sleep). 0 == no added latency.
    extra_latency_polls: int = 0

    # Number of times execute_mission_step() should be answered with a
    # transient failure (FAILED/NETWORK_DEGRADED) before letting the step
    # proceed normally. Decremented each time it is consumed. Simulates a
    # flaky link that recovers deterministically after exactly N
    # attempts, not probabilistically.
    flaky_failures_remaining: int = 0

    # Per-capability sensor failure toggle: capabilities in this set are
    # treated as having a hardware fault. A step whose capability_type is
    # in this set immediately fails with FAILED/SENSOR_FAULT, regardless
    # of offline/flaky state (sensor failures are physical, not network).
    failed_sensors: set[CapabilityType] = field(default_factory=set)

    # -- probabilistic, for unpredictable field conditions --

    # Per-attempt probability (0.0-1.0) that executing a step is answered
    # with the given FailureReason instead of proceeding normally. Dict
    # iteration order (insertion order) decides priority if multiple
    # rates are set and more than one would trigger on the same draw.
    random_failure_rates: dict[FailureReason, float] = field(default_factory=dict)

    # None -> real entropy (genuinely unpredictable, for interactive/demo
    # use). An int -> every maybe_random_failure() call sequence is
    # exactly reproducible for that seed, which is what tests use to pin
    # deterministic assertions about statistically "random" behavior.
    rng_seed: int | None = None
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.rng_seed)

    def is_sensor_failed(self, capability_type: CapabilityType) -> bool:
        return capability_type in self.failed_sensors

    def consume_flaky_failure(self) -> bool:
        """Returns True (and decrements) exactly once per remaining
        flaky failure. Call this from execute_mission_step() at the point
        a fresh attempt is being made."""
        if self.flaky_failures_remaining > 0:
            self.flaky_failures_remaining -= 1
            return True
        return False

    def maybe_random_failure(self) -> FailureReason | None:
        """Consults random_failure_rates with this profile's seeded RNG.
        Returns the first triggered FailureReason or None if none fire
        this call. rate=0.0 never fires; rate=1.0 always fires -- both
        edges are deterministic regardless of seed, and are the base
        test cases for this method."""
        for reason, probability in self.random_failure_rates.items():
            if self._rng.random() < probability:
                return reason
        return None


def healthy_profile() -> FaultProfile:
    """Explicit factory for the default/no-fault state -- used as the
    default for adapters' self._fault_profile so `FaultProfile()`
    construction isn't duplicated inline across two adapter files."""
    return FaultProfile()
