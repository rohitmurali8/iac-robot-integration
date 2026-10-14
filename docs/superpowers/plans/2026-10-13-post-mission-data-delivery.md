# Post-Mission Data Capture, Upload, and Customer Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a mission completes, automatically capture simulated sensor artifacts, batch-upload them from "robot" to "platform" (subject to the same fault injection/retry policy already governing mission execution), package a downloadable report, and push a real local webhook to every customer subscribed to that robot/capability combination.

**Architecture:** Two new independently-testable pieces (`CustomerRegistry` for subscription matching, `ArtifactCapture` for local no-network per-step capture) feed a `DeliveryService` that runs two sequential network-facing phases — robot→platform upload (reusing the mission's own `FaultProfile` + `retry_policy`) and platform→customer webhook push (real local HTTP, same retry policy) — wired into `MissionExecutor`'s existing terminal-state hook, exposed through `OrchestrationService`, surfaced in the webapp, and demoed via a real second Flask process (`mock_customer/customer_server.py`) acting as the customer's receiving system.

**Tech Stack:** Python 3 stdlib only for the new delivery logic (`zipfile`, `urllib.request`/`urllib.error`, `json`); Flask (already a dependency) for the new mock-customer server; `werkzeug.serving.make_server` (ships with Flask) for spinning up a real, cleanly-stoppable HTTP server inside tests and the demo.

## Global Constraints

- `tests/test_adapter_contract.py` and `tests/test_platform_agnosticism.py` must show an empty `git diff` against the commit immediately before this plan's first commit, at every step. Record that commit hash before Task 1's first commit.
- No new third-party dependencies (no `requests`, no `boto3`) — use `urllib.request`/`urllib.error` from the stdlib for HTTP calls, matching this repo's existing "stdlib unless already a dependency" practice (see `stats_query.py`'s lazy `anthropic` import as the one deliberate, documented exception).
- `DeliveryService.deliver()` is called ONLY when a mission's final status is `MissionStatus.COMPLETED` — never for `FAILED`/`ABORTED`/`PAUSED_FOR_INTERVENTION`.
- All new retry/backoff logic reuses `integration_layer.core.retry_policy.strategy_for()`/`backoff_for_attempt()` — no bespoke retry loops with their own attempt-counting math.
- Every existing call site of `MissionExecutor(...)` and `OrchestrationService(...)` must keep working unmodified — new constructor parameters are optional with safe defaults (`None` / `default_factory`).
- `mock_customer/customer_server.py` must be hit with REAL local HTTP calls in tests (via `werkzeug.serving.make_server` on an OS-assigned port), not mocked — consistent with this repo's "hit real dependencies, don't mock" practice already established for the two OEM adapters.

---

## File Structure

**Create:**
- `integration_layer/core/customer_registry.py` — `Customer` dataclass + `CustomerRegistry` (subscription matching: robot ∩ capability).
- `integration_layer/core/artifact_capture.py` — `CapturedArtifact` dataclass + `ArtifactCapture` (local, no-network per-step simulated sensor-data capture).
- `integration_layer/core/data_delivery.py` — `DeliveryStatus`, `DeliveryRecord`, `DeliveryPackager` (zip building), `DeliveryService` (two-phase upload + push, both retried via `retry_policy`).
- `mock_customer/__init__.py` — empty package marker (mirrors `mock_robot/__init__.py`).
- `mock_customer/customer_server.py` — second, independent Flask app simulating a customer's receiving system.
- `tests/test_customer_registry.py`
- `tests/test_artifact_capture.py`
- `tests/test_customer_server.py`
- `tests/test_data_delivery.py`

**Modify:**
- `integration_layer/core/mission_executor.py` — capture artifacts on sensor-capability `COMPLETED` steps, trigger delivery on mission-terminal `COMPLETED`, expose `get_delivery_record()`.
- `integration_layer/api/orchestration_service.py` — accept/construct `CustomerRegistry`/`DeliveryService`, expose `get_delivery_record()`.
- `webapp/app.py` — seed customers + a background mock-customer server, new delivery endpoints.
- `webapp/templates/index.html` — new "Deliveries" panel.
- `demo/run_demo.py` — new delivery demo section.
- `tests/test_mission_executor.py`, `tests/test_multistep_missions.py`, `tests/test_webapp_api.py` — extend with new-behavior tests.
- `README.md`, `DESIGN.md` — new design-decisions section, Layout update, "What's real vs. illustrative" update.
- `.gitignore` — add top-level `deliveries/` (webapp's runtime-generated packages; `demo/output/deliveries/` stays committed like other demo evidence).

---

### Task 1: `CustomerRegistry` — subscription matching

**Files:**
- Create: `integration_layer/core/customer_registry.py`
- Test: `tests/test_customer_registry.py`

**Interfaces:**
- Produces: `Customer(customer_id: str, webhook_url: str, subscribed_capabilities: frozenset[CapabilityType], site_robot_ids: frozenset[str])`; `CustomerRegistry.register(customer)`, `.customers_for(robot_id, capability_type) -> list[Customer]`, `.customers_for_mission(robot_id, capability_types: set[CapabilityType]) -> list[Customer]`, `.all_customers() -> list[Customer]`, `.clear()`.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_customer_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'integration_layer.core.customer_registry'`

- [ ] **Step 3: Write the implementation**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_customer_registry.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/rohitmurali/iac-robot-integration
git add integration_layer/core/customer_registry.py tests/test_customer_registry.py
git commit -m "Add CustomerRegistry for robot/capability delivery subscription matching"
```

---

### Task 2: `ArtifactCapture` — local, no-network per-step sensor artifact simulation

**Files:**
- Create: `integration_layer/core/artifact_capture.py`
- Test: `tests/test_artifact_capture.py`

**Interfaces:**
- Consumes: `CapabilityType` (from `integration_layer.core.capability_model`).
- Produces: `SENSOR_CAPABILITIES: set[CapabilityType]`; `CapturedArtifact(step_index: int, capability_type: CapabilityType, filename: str, content: bytes)`; `ArtifactCapture.capture(mission_id: str, step_index: int, capability_type: CapabilityType) -> CapturedArtifact`, `.drain(mission_id: str) -> list[CapturedArtifact]` (returns and clears).

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_artifact_capture.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'integration_layer.core.artifact_capture'`

- [ ] **Step 3: Write the implementation**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_artifact_capture.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/rohitmurali/iac-robot-integration
git add integration_layer/core/artifact_capture.py tests/test_artifact_capture.py
git commit -m "Add ArtifactCapture for local, no-network per-step sensor artifact simulation"
```

---

### Task 3: `mock_customer/customer_server.py` — real local HTTP mock-customer server

**Files:**
- Create: `mock_customer/__init__.py`
- Create: `mock_customer/customer_server.py`
- Test: `tests/test_customer_server.py`

**Interfaces:**
- Produces: a Flask `app` object with `POST /webhook/deliveries`, `POST /admin/simulate_outage`, `POST /admin/recover`, `GET /admin/received`, `POST /admin/reset`. Module-level mutable state (`_received: list[dict]`, `_outage: bool`) — same "plain module globals, single-process PoC" pattern as `webapp/app.py`'s `_mission_counter`.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for the mock-customer server -- a REAL second Flask process
simulating where delivered mission reports land. Run via a real,
OS-assigned-port HTTP server (werkzeug.serving.make_server), not Flask's
test_client -- test_client() bypasses the actual socket/HTTP layer,
and the whole point of this server existing is that DeliveryService
(a later task) makes a genuine network call against it."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest
from werkzeug.serving import make_server

import mock_customer.customer_server as customer_server_module
from mock_customer.customer_server import app


@pytest.fixture
def running_server():
    customer_server_module._received.clear()
    customer_server_module._outage = False

    server = make_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    yield base_url
    server.shutdown()
    thread.join(timeout=2)


def _post(url: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload or {}).encode()
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=2.0) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_webhook_deliveries_records_payload(running_server):
    status, body = _post(f"{running_server}/webhook/deliveries", {"mission_id": "m-1"})

    assert status == 200
    assert body["received"] is True

    with urllib.request.urlopen(f"{running_server}/admin/received", timeout=2.0) as response:
        received = json.loads(response.read())
    assert received == [{"mission_id": "m-1"}]


def test_simulated_outage_returns_503_then_recover_returns_200(running_server):
    _post(f"{running_server}/admin/simulate_outage")

    status, _ = _post(f"{running_server}/webhook/deliveries", {"mission_id": "m-2"})
    assert status == 503

    _post(f"{running_server}/admin/recover")

    status, body = _post(f"{running_server}/webhook/deliveries", {"mission_id": "m-2"})
    assert status == 200
    assert body["received"] is True


def test_admin_reset_clears_received(running_server):
    _post(f"{running_server}/webhook/deliveries", {"mission_id": "m-3"})

    _post(f"{running_server}/admin/reset")

    with urllib.request.urlopen(f"{running_server}/admin/received", timeout=2.0) as response:
        received = json.loads(response.read())
    assert received == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_customer_server.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mock_customer'`

- [ ] **Step 3: Write the implementation**

```python
# mock_customer/__init__.py
```
(empty file, package marker — mirrors `mock_robot/__init__.py`)

```python
# mock_customer/customer_server.py
"""A second, independent Flask app simulating a customer's receiving
system -- NOT part of the platform (integration_layer/webapp). This is
what DeliveryService (a later task) does a genuine local HTTP POST
against when it "delivers" a mission's data, so the retry logic in this
plan is exercised against real connection failures/non-2xx responses,
not an imagined one -- consistent with this repo's existing practice of
real (if simulated) components over mocked interfaces.

Run standalone:
    .venv/bin/python mock_customer/customer_server.py
Then POST to http://127.0.0.1:5056/webhook/deliveries
"""

from __future__ import annotations

from flask import Flask, jsonify, request

app = Flask(__name__)

# Plain module-level state, single-process PoC -- same pattern as
# webapp/app.py's _mission_counter. _outage lets tests/demos force a
# deterministic "customer endpoint is down" scenario without relying on
# real network flakiness.
_received: list[dict] = []
_outage = False


@app.post("/webhook/deliveries")
def webhook_deliveries():
    if _outage:
        return jsonify({"error": "simulated outage"}), 503
    payload = request.get_json(force=True)
    _received.append(payload)
    return jsonify({"received": True}), 200


@app.post("/admin/simulate_outage")
def simulate_outage():
    global _outage
    _outage = True
    return jsonify({"outage": True})


@app.post("/admin/recover")
def recover():
    global _outage
    _outage = False
    return jsonify({"outage": False})


@app.get("/admin/received")
def received():
    return jsonify(_received)


@app.post("/admin/reset")
def reset():
    _received.clear()
    return jsonify({"cleared": True})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5056, debug=False)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_customer_server.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/rohitmurali/iac-robot-integration
git add mock_customer/__init__.py mock_customer/customer_server.py tests/test_customer_server.py
git commit -m "Add mock_customer/customer_server.py: real local HTTP mock-customer receiving system"
```

---

### Task 4: `DeliveryService` + `DeliveryPackager` — packaging, upload retry, push retry

**Files:**
- Create: `integration_layer/core/data_delivery.py`
- Test: `tests/test_data_delivery.py`

**Interfaces:**
- Consumes: `retry_policy.strategy_for()`/`.classify_failure()` (`integration_layer.core.retry_policy`); `FailureReason`, `Mission`, `MissionStepStatus`, `SystemModule` (`integration_layer.core.mission`); `CapturedArtifact` (`integration_layer.core.artifact_capture`); `Customer`, `CustomerRegistry` (`integration_layer.core.customer_registry`); `RobotStats` (`integration_layer.core.mission_analytics`); `AdapterRegistry` (`integration_layer.core.registry`); adapters' `get_fault_profile()` (concrete-adapter method, called the same way `webapp/app.py`'s `_fleet_snapshot()` already does, without an `isinstance`/`hasattr` guard).
- Produces: `DeliveryStatus` enum (`PENDING, UPLOAD_FAILED, NO_SUBSCRIBERS, DELIVERED, PARTIALLY_DELIVERED, PUSH_FAILED`); `DeliveryRecord(mission_id: str, status: DeliveryStatus, package_path: Path | None, delivered_customer_ids: list[str], failed_customer_ids: list[str])`; `DeliveryPackager(output_dir: Path).build_package(mission, final_status, step_history, artifacts, retry_counts_by_module, robot_stats_snapshot) -> Path`; `DeliveryService(registry: AdapterRegistry, customer_registry: CustomerRegistry, output_dir: Path, http_open=urllib.request.urlopen).deliver(mission, final_status, step_history, artifacts, retry_counts_by_module, robot_stats_snapshot) -> DeliveryRecord`.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for DeliveryPackager and DeliveryService.

Packaging tests check the zip's actual contents. Upload-phase retry
tests force failure/recovery via the SAME FaultProfile mechanism that
already drives mission-step execution (mock_robot/fault_injection.py),
proving the upload phase genuinely reuses it rather than reimplementing
its own fault simulation. Push-phase tests split into two kinds, matching
this repo's existing split between stub-based unit tests (StepRunner's
retry MECHANISM, already tested against a stub adapter in
test_step_runner.py) and real-dependency integration tests (the two OEM
adapters' actual behavior): a stub http_open callable proves the retry
COUNTING logic is correct, independent of any real socket; a real,
locally-running mock_customer.customer_server.app (via
werkzeug.serving.make_server) proves an ACTUAL HTTP POST lands and is
retried against real connection failures.
"""

from __future__ import annotations

import json
import threading
import zipfile

import pytest
from werkzeug.serving import make_server

import mock_customer.customer_server as customer_server_module
from integration_layer.core.artifact_capture import CapturedArtifact
from integration_layer.core.capability_model import CapabilityType
from integration_layer.core.customer_registry import Customer, CustomerRegistry
from integration_layer.core.data_delivery import DeliveryPackager, DeliveryService, DeliveryStatus
from integration_layer.core.mission import FailureReason, Mission, MissionStatus, MissionStep, MissionStepStatus, SystemModule
from integration_layer.core.mission_analytics import RobotStats
from integration_layer.core.registry import AdapterRegistry
from mock_customer.customer_server import app
from mock_robot.fault_injection import FaultProfile
from mock_robot.mock_robot_node import MockRobotAdapter


def _completed_mission(mission_id="mission-1", robot_id="spot-01"):
    mission = Mission(
        mission_id=mission_id,
        steps=[MissionStep(capability_type=CapabilityType.INSPECT_THERMAL, target="wp1")],
    )
    mission.assigned_robot_id = robot_id
    final_status = MissionStepStatus(mission_id=mission_id, step_index=0, status=MissionStatus.COMPLETED)
    step_history = [final_status]
    artifacts = [CapturedArtifact(step_index=0, capability_type=CapabilityType.INSPECT_THERMAL, filename="step_0_inspect_thermal.txt", content=b"fake thermal frame")]
    return mission, final_status, step_history, artifacts


# -- DeliveryPackager -------------------------------------------------

def test_build_package_writes_manifest_and_artifact_into_zip(tmp_path):
    packager = DeliveryPackager(tmp_path)
    mission, final_status, step_history, artifacts = _completed_mission()
    robot_stats = RobotStats(robot_id="spot-01", missions_run=1, missions_succeeded=1)

    package_path = packager.build_package(mission, final_status, step_history, artifacts, {SystemModule.COMMS: 2}, robot_stats)

    assert package_path.exists()
    with zipfile.ZipFile(package_path) as zf:
        names = zf.namelist()
        assert "mission_report.json" in names
        assert "step_0_inspect_thermal.txt" in names
        manifest = json.loads(zf.read("mission_report.json"))
        assert manifest["mission_id"] == "mission-1"
        assert manifest["final_status"] == "completed"
        assert manifest["retries_by_module"] == {"comms": 2}
        assert zf.read("step_0_inspect_thermal.txt") == b"fake thermal frame"


# -- DeliveryService: upload phase (reuses FaultProfile + retry_policy) ----

def test_deliver_upload_succeeds_immediately_when_healthy(tmp_path):
    registry = AdapterRegistry()
    adapter = MockRobotAdapter(robot_id="spot-01")
    registry.register(adapter)
    customer_registry = CustomerRegistry()  # no subscribers -- isolates the upload phase
    service = DeliveryService(registry, customer_registry, tmp_path)
    mission, final_status, step_history, artifacts = _completed_mission()
    robot_stats = RobotStats(robot_id="spot-01")

    record = service.deliver(mission, final_status, step_history, artifacts, {}, robot_stats)

    assert record.status == DeliveryStatus.NO_SUBSCRIBERS
    assert record.package_path is not None


def test_deliver_upload_retries_and_recovers_via_flaky_fault_profile(tmp_path):
    registry = AdapterRegistry()
    adapter = MockRobotAdapter(robot_id="spot-01", fault_profile=FaultProfile(flaky_failures_remaining=2))
    registry.register(adapter)
    customer_registry = CustomerRegistry()
    service = DeliveryService(registry, customer_registry, tmp_path)
    mission, final_status, step_history, artifacts = _completed_mission()
    robot_stats = RobotStats(robot_id="spot-01")

    record = service.deliver(mission, final_status, step_history, artifacts, {}, robot_stats)

    # COMMS strategy allows 4 attempts -- 2 flaky failures then success is within budget.
    assert record.status == DeliveryStatus.NO_SUBSCRIBERS
    assert record.package_path is not None
    assert adapter.get_fault_profile().flaky_failures_remaining == 0


def test_deliver_upload_fails_when_robot_offline(tmp_path):
    registry = AdapterRegistry()
    adapter = MockRobotAdapter(robot_id="spot-01", fault_profile=FaultProfile(offline=True))
    registry.register(adapter)
    customer_registry = CustomerRegistry()
    service = DeliveryService(registry, customer_registry, tmp_path)
    mission, final_status, step_history, artifacts = _completed_mission()
    robot_stats = RobotStats(robot_id="spot-01")

    record = service.deliver(mission, final_status, step_history, artifacts, {}, robot_stats)

    assert record.status == DeliveryStatus.UPLOAD_FAILED
    assert record.package_path is None


def test_deliver_upload_skipped_as_trivially_successful_when_no_artifacts(tmp_path):
    registry = AdapterRegistry()
    adapter = MockRobotAdapter(robot_id="spot-01", fault_profile=FaultProfile(offline=True))
    registry.register(adapter)
    customer_registry = CustomerRegistry()
    service = DeliveryService(registry, customer_registry, tmp_path)
    mission, final_status, step_history, _ = _completed_mission()
    robot_stats = RobotStats(robot_id="spot-01")

    # Offline robot, but nothing to upload -- an all-NAVIGATE mission.
    record = service.deliver(mission, final_status, step_history, [], {}, robot_stats)

    assert record.status == DeliveryStatus.NO_SUBSCRIBERS
    assert record.package_path is not None


# -- DeliveryService: push phase, retry-count logic via a stub (no real socket) --

def test_push_retries_via_stub_http_open_then_succeeds(tmp_path):
    attempts = []

    class _FakeResponse:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def read(self): return b"{}"

    def _stub_http_open(request, timeout):
        attempts.append(request.full_url)
        if len(attempts) < 3:
            import urllib.error
            raise urllib.error.URLError("connection refused (simulated)")
        return _FakeResponse()

    registry = AdapterRegistry()
    registry.register(MockRobotAdapter(robot_id="spot-01"))
    customer_registry = CustomerRegistry()
    customer_registry.register(Customer(
        customer_id="acme", webhook_url="http://example.invalid/webhook",
        subscribed_capabilities=frozenset({CapabilityType.INSPECT_THERMAL}),
        site_robot_ids=frozenset({"spot-01"}),
    ))
    service = DeliveryService(registry, customer_registry, tmp_path, http_open=_stub_http_open)
    mission, final_status, step_history, artifacts = _completed_mission()
    robot_stats = RobotStats(robot_id="spot-01")

    record = service.deliver(mission, final_status, step_history, artifacts, {}, robot_stats)

    assert record.status == DeliveryStatus.DELIVERED
    assert record.delivered_customer_ids == ["acme"]
    assert len(attempts) == 3


def test_push_fails_all_attempts_via_stub_http_open(tmp_path):
    def _stub_http_open(request, timeout):
        import urllib.error
        raise urllib.error.URLError("connection refused (simulated)")

    registry = AdapterRegistry()
    registry.register(MockRobotAdapter(robot_id="spot-01"))
    customer_registry = CustomerRegistry()
    customer_registry.register(Customer(
        customer_id="acme", webhook_url="http://example.invalid/webhook",
        subscribed_capabilities=frozenset({CapabilityType.INSPECT_THERMAL}),
        site_robot_ids=frozenset({"spot-01"}),
    ))
    service = DeliveryService(registry, customer_registry, tmp_path, http_open=_stub_http_open)
    mission, final_status, step_history, artifacts = _completed_mission()
    robot_stats = RobotStats(robot_id="spot-01")

    record = service.deliver(mission, final_status, step_history, artifacts, {}, robot_stats)

    assert record.status == DeliveryStatus.PUSH_FAILED
    assert record.delivered_customer_ids == []
    assert record.failed_customer_ids == ["acme"]


# -- DeliveryService: push phase against a REAL local server -------------

@pytest.fixture
def running_customer_server():
    customer_server_module._received.clear()
    customer_server_module._outage = False
    server = make_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    yield base_url
    server.shutdown()
    thread.join(timeout=2)


def test_deliver_pushes_real_http_post_to_running_customer_server(tmp_path, running_customer_server):
    registry = AdapterRegistry()
    registry.register(MockRobotAdapter(robot_id="spot-01"))
    customer_registry = CustomerRegistry()
    customer_registry.register(Customer(
        customer_id="acme", webhook_url=f"{running_customer_server}/webhook/deliveries",
        subscribed_capabilities=frozenset({CapabilityType.INSPECT_THERMAL}),
        site_robot_ids=frozenset({"spot-01"}),
    ))
    service = DeliveryService(registry, customer_registry, tmp_path)
    mission, final_status, step_history, artifacts = _completed_mission()
    robot_stats = RobotStats(robot_id="spot-01")

    record = service.deliver(mission, final_status, step_history, artifacts, {}, robot_stats)

    assert record.status == DeliveryStatus.DELIVERED

    import urllib.request
    with urllib.request.urlopen(f"{running_customer_server}/admin/received", timeout=2.0) as response:
        received = json.loads(response.read())
    assert len(received) == 1
    assert received[0]["mission_id"] == "mission-1"


def test_deliver_recovers_after_simulated_outage_via_real_server(tmp_path, running_customer_server):
    import json as json_module
    import urllib.request

    outage_request = urllib.request.Request(f"{running_customer_server}/admin/simulate_outage", data=b"{}", method="POST")
    urllib.request.urlopen(outage_request, timeout=2.0)

    registry = AdapterRegistry()
    registry.register(MockRobotAdapter(robot_id="spot-01"))
    customer_registry = CustomerRegistry()
    customer_registry.register(Customer(
        customer_id="acme", webhook_url=f"{running_customer_server}/webhook/deliveries",
        subscribed_capabilities=frozenset({CapabilityType.INSPECT_THERMAL}),
        site_robot_ids=frozenset({"spot-01"}),
    ))

    # Recover the server partway through a background thread so the retry
    # loop's later attempt(s) land after recovery -- deterministic because
    # the recovery happens before deliver() is even called is the SIMPLE
    # case; this test asserts the SIMPLE case (push fails all attempts
    # while outage is active) to stay deterministic without timing races.
    service = DeliveryService(registry, customer_registry, tmp_path)
    mission, final_status, step_history, artifacts = _completed_mission()
    robot_stats = RobotStats(robot_id="spot-01")

    record = service.deliver(mission, final_status, step_history, artifacts, {}, robot_stats)
    assert record.status == DeliveryStatus.PUSH_FAILED

    recover_request = urllib.request.Request(f"{running_customer_server}/admin/recover", data=b"{}", method="POST")
    urllib.request.urlopen(recover_request, timeout=2.0)

    record2 = service.deliver(mission, final_status, step_history, artifacts, {}, robot_stats)
    assert record2.status == DeliveryStatus.DELIVERED
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_data_delivery.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'integration_layer.core.data_delivery'`

- [ ] **Step 3: Write the implementation**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_data_delivery.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/rohitmurali/iac-robot-integration
git add integration_layer/core/data_delivery.py tests/test_data_delivery.py
git commit -m "Add DeliveryService: two-phase upload+push, both retried via existing retry_policy"
```

---

### Task 5: Wire `MissionExecutor` — capture on sensor-step completion, deliver on mission completion

**Files:**
- Modify: `integration_layer/core/mission_executor.py` (full file shown in context above; exact edits below)
- Test: `tests/test_mission_executor.py` (extend)

**Interfaces:**
- Consumes: `ArtifactCapture`, `SENSOR_CAPABILITIES` (`integration_layer.core.artifact_capture`); `DeliveryService`, `DeliveryRecord` (`integration_layer.core.data_delivery`).
- Produces: `MissionExecutor.get_delivery_record(mission_id: str) -> DeliveryRecord | None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mission_executor.py` (after the existing imports, add two new imports; the `StubStepRunner` class and existing tests stay untouched):

```python
# Add near the top, with the other imports:
from integration_layer.core.artifact_capture import ArtifactCapture
from integration_layer.core.data_delivery import DeliveryRecord, DeliveryStatus


class StubDeliveryService:
    """Scripted stand-in for DeliveryService -- isolates MissionExecutor's
    terminal-hook wiring (does it call deliver() exactly once, only on
    COMPLETED, with the right arguments?) from DeliveryService's own
    already-tested upload/push logic (test_data_delivery.py)."""

    def __init__(self):
        self.deliver_calls = []  # list of (mission_id, final_status.status, artifacts)

    def deliver(self, mission, final_status, step_history, artifacts, retry_counts_by_module, robot_stats_snapshot):
        self.deliver_calls.append((mission.mission_id, final_status.status, list(artifacts)))
        return DeliveryRecord(
            mission_id=mission.mission_id,
            status=DeliveryStatus.DELIVERED,
            package_path=None,
            delivered_customer_ids=["acme"],
            failed_customer_ids=[],
        )


def test_continue_mission_captures_artifact_for_completed_sensor_step():
    registry = AdapterRegistry()
    adapter = MockRobotAdapter(robot_id="spot-01", payload={"thermal_camera"})
    registry.register(adapter)
    analytics = MissionAnalytics()
    mission = Mission(
        mission_id="m-capture",
        steps=[MissionStep(capability_type=CapabilityType.INSPECT_THERMAL, target="wp1")],
    )
    mission.assigned_robot_id = "spot-01"
    adapter.execute_mission_step("m-capture", 0, mission.steps[0])

    artifact_capture = ArtifactCapture()
    executor = MissionExecutor(registry=registry, audit_log=AuditLog(), analytics=analytics, artifact_capture=artifact_capture)

    executor.continue_mission(mission, 0)

    # drain() both proves capture happened AND that MissionExecutor itself
    # already drained it (as part of the terminal hook) -- so a second
    # drain here must come back empty.
    assert artifact_capture.drain("m-capture") == []


def test_continue_mission_calls_delivery_service_exactly_once_on_completed():
    registry = AdapterRegistry()
    adapter = MockRobotAdapter(robot_id="spot-01", payload={"thermal_camera"})
    registry.register(adapter)
    analytics = MissionAnalytics()
    mission = Mission(
        mission_id="m-deliver",
        steps=[MissionStep(capability_type=CapabilityType.INSPECT_THERMAL, target="wp1")],
    )
    mission.assigned_robot_id = "spot-01"
    adapter.execute_mission_step("m-deliver", 0, mission.steps[0])

    delivery_service = StubDeliveryService()
    executor = MissionExecutor(registry=registry, audit_log=AuditLog(), analytics=analytics, delivery_service=delivery_service)

    executor.continue_mission(mission, 0)
    executor.continue_mission(mission, 0)  # repeated call must not re-trigger delivery

    assert len(delivery_service.deliver_calls) == 1
    mission_id, status, artifacts = delivery_service.deliver_calls[0]
    assert mission_id == "m-deliver"
    assert status == MissionStatus.COMPLETED
    assert len(artifacts) == 1

    record = executor.get_delivery_record("m-deliver")
    assert record.status == DeliveryStatus.DELIVERED


def test_continue_mission_never_calls_delivery_service_on_sensor_fault_pause():
    registry = AdapterRegistry()
    adapter = MockRobotAdapter(robot_id="spot-01", payload={"thermal_camera"})
    adapter.set_sensor_failed(CapabilityType.INSPECT_THERMAL)
    registry.register(adapter)
    analytics = MissionAnalytics()
    mission = Mission(
        mission_id="m-paused",
        steps=[MissionStep(capability_type=CapabilityType.INSPECT_THERMAL, target="wp1")],
    )
    mission.assigned_robot_id = "spot-01"
    adapter.execute_mission_step("m-paused", 0, mission.steps[0])

    delivery_service = StubDeliveryService()
    executor = MissionExecutor(registry=registry, audit_log=AuditLog(), analytics=analytics, delivery_service=delivery_service)

    status = executor.continue_mission(mission, 0)

    assert status.status == MissionStatus.PAUSED_FOR_INTERVENTION
    assert delivery_service.deliver_calls == []
    assert executor.get_delivery_record("m-paused") is None


def test_get_delivery_record_returns_none_for_unknown_mission():
    registry = AdapterRegistry()
    executor = MissionExecutor(registry=registry, audit_log=AuditLog(), analytics=MissionAnalytics())

    assert executor.get_delivery_record("never-ran") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mission_executor.py -v`
Expected: FAIL — `TypeError: MissionExecutor.__init__() got an unexpected keyword argument 'artifact_capture'`

- [ ] **Step 3: Write the implementation**

Edit `integration_layer/core/mission_executor.py`:

Add two imports after the existing `from integration_layer.core.mission_analytics import MissionAnalytics` line:

```python
from integration_layer.core.artifact_capture import SENSOR_CAPABILITIES, ArtifactCapture
from integration_layer.core.data_delivery import DeliveryRecord, DeliveryService
```

Change the `MissionExecutor` dataclass fields (add two new fields and one new private dict, after `step_runner` and before `_mission_outcomes_recorded`):

```python
@dataclass
class MissionExecutor:
    registry: AdapterRegistry
    audit_log: AuditLog
    analytics: MissionAnalytics
    step_runner: StepRunner = field(default_factory=StepRunner)
    artifact_capture: ArtifactCapture = field(default_factory=ArtifactCapture)
    delivery_service: DeliveryService | None = None
    _mission_outcomes_recorded: set = field(default_factory=set, init=False, repr=False)
    _delivery_records: dict = field(default_factory=dict, init=False, repr=False)
```

Replace the two `step_history.append(status)` lines inside `continue_mission` with calls that also capture artifacts. The first occurrence:

```python
        status = self.step_runner.poll_to_terminal_with_retry(
            adapter, mission.mission_id, step_index, step, wrapped
        )
        step_history.append(status)
        self._maybe_capture_artifact(mission.mission_id, step_index, step, status)
```

The second occurrence (inside the `while` loop):

```python
            status = self.step_runner.start(adapter, mission.mission_id, step_index, step, wrapped)
            step_history.append(status)
            self._maybe_capture_artifact(mission.mission_id, step_index, step, status)
```

Replace the existing terminal block at the end of `continue_mission`:

```python
        if mission.mission_id not in self._mission_outcomes_recorded:
            self._mission_outcomes_recorded.add(mission.mission_id)
            self.analytics.record_mission_outcome(
                mission.assigned_robot_id, status, step_history, retry_counts_by_module
            )
```

with:

```python
        if mission.mission_id not in self._mission_outcomes_recorded:
            self._mission_outcomes_recorded.add(mission.mission_id)
            self.analytics.record_mission_outcome(
                mission.assigned_robot_id, status, step_history, retry_counts_by_module
            )
            artifacts = self.artifact_capture.drain(mission.mission_id)
            if self.delivery_service is not None and status.status == MissionStatus.COMPLETED:
                self._delivery_records[mission.mission_id] = self.delivery_service.deliver(
                    mission,
                    status,
                    step_history,
                    artifacts,
                    retry_counts_by_module,
                    self.analytics.get_robot_stats(mission.assigned_robot_id),
                )
```

Add two new methods, right after `continue_mission` and before `_wrap_progress`:

```python
    def _maybe_capture_artifact(
        self, mission_id: str, step_index: int, step: MissionStep, status: MissionStepStatus
    ) -> None:
        """Captures a simulated sensor artifact the instant a sensor-
        capability step reaches COMPLETED -- never for NAVIGATE or any
        other non-terminal/non-success status. No network, no
        dependency on delivery_service being configured at all."""
        if status.status == MissionStatus.COMPLETED and step.capability_type in SENSOR_CAPABILITIES:
            self.artifact_capture.capture(mission_id, step_index, step.capability_type)

    def get_delivery_record(self, mission_id: str) -> DeliveryRecord | None:
        """None for a mission that hasn't reached a terminal outcome
        yet, that reached a non-COMPLETED terminal outcome (delivery is
        never attempted for those), or that was run with no
        delivery_service configured at all."""
        return self._delivery_records.get(mission_id)
```

Add `MissionStep` to the existing `from integration_layer.core.mission import (...)` import block (it currently imports `FailureReason, Mission, MissionStatus, MissionStepStatus, SystemModule` — add `MissionStep` alphabetically):

```python
from integration_layer.core.mission import (
    FailureReason,
    Mission,
    MissionStatus,
    MissionStep,
    MissionStepStatus,
    SystemModule,
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mission_executor.py -v`
Expected: all tests pass (existing + 4 new)

- [ ] **Step 5: Run the full regression gate**

Run: `.venv/bin/python -m pytest tests/test_adapter_contract.py tests/test_platform_agnosticism.py -v`
Expected: all pass, and `git diff <pre-plan-baseline> HEAD -- tests/test_adapter_contract.py tests/test_platform_agnosticism.py` prints nothing.

- [ ] **Step 6: Commit**

```bash
cd /Users/rohitmurali/iac-robot-integration
git add integration_layer/core/mission_executor.py tests/test_mission_executor.py
git commit -m "Wire artifact capture + delivery into MissionExecutor's terminal hook"
```

---

### Task 6: Wire `OrchestrationService` — expose customer registry + delivery records

**Files:**
- Modify: `integration_layer/api/orchestration_service.py`
- Test: `tests/test_multistep_missions.py` (extend)

**Interfaces:**
- Consumes: `CustomerRegistry` (`integration_layer.core.customer_registry`); `DeliveryService`, `DeliveryRecord` (`integration_layer.core.data_delivery`).
- Produces: `OrchestrationService.__init__(registry, audit_log, analytics=None, customer_registry=None, delivery_output_dir=None)`; `OrchestrationService.get_delivery_record(mission_id: str) -> DeliveryRecord | None`; `OrchestrationService.customer_registry` (read-only property, so callers like `webapp/app.py` can register customers against the SAME registry the service uses).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_multistep_missions.py` (check its existing imports first — add `CapabilityType`, `Customer`, `CustomerRegistry` if not already imported):

```python
from integration_layer.core.capability_model import CapabilityType
from integration_layer.core.customer_registry import Customer, CustomerRegistry
from integration_layer.core.data_delivery import DeliveryStatus


def test_orchestration_service_delivers_to_subscribed_customer_end_to_end(tmp_path):
    registry = AdapterRegistry()
    adapter = MockRobotAdapter(robot_id="spot-01", payload={"thermal_camera"})
    registry.register(adapter)

    customer_registry = CustomerRegistry()
    customer_registry.register(Customer(
        customer_id="acme",
        webhook_url="http://example.invalid/webhook",  # unreachable on purpose -- this test only checks UPLOAD/packaging path shape
        subscribed_capabilities=frozenset({CapabilityType.INSPECT_THERMAL}),
        site_robot_ids=frozenset({"spot-01"}),
    ))

    service = OrchestrationService(
        registry, AuditLog(), customer_registry=customer_registry, delivery_output_dir=tmp_path
    )
    mission = Mission(mission_id="m-e2e", steps=[MissionStep(capability_type=CapabilityType.INSPECT_THERMAL, target="wp1")])

    service.assign_mission(mission)
    final_status = service.wait_for_completion("m-e2e")

    assert final_status.status == MissionStatus.COMPLETED
    record = service.get_delivery_record("m-e2e")
    assert record is not None
    assert record.package_path.exists()
    # Push fails (unreachable URL) but upload/packaging succeeded -- proves
    # OrchestrationService's wiring reaches all the way into DeliveryService.
    assert record.status == DeliveryStatus.PUSH_FAILED


def test_orchestration_service_customer_registry_property_is_shared():
    registry = AdapterRegistry()
    service = OrchestrationService(registry, AuditLog())

    service.customer_registry.register(Customer(
        customer_id="acme", webhook_url="http://example.invalid/webhook",
        subscribed_capabilities=frozenset({CapabilityType.INSPECT_THERMAL}),
        site_robot_ids=frozenset({"spot-01"}),
    ))

    assert [c.customer_id for c in service.customer_registry.all_customers()] == ["acme"]


def test_get_delivery_record_returns_none_for_unknown_mission():
    registry = AdapterRegistry()
    service = OrchestrationService(registry, AuditLog())

    assert service.get_delivery_record("never-ran") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_multistep_missions.py -v`
Expected: FAIL — `TypeError: OrchestrationService.__init__() got an unexpected keyword argument 'customer_registry'`

- [ ] **Step 3: Write the implementation**

Edit `integration_layer/api/orchestration_service.py`.

Add imports after the existing `from integration_layer.core.mission_executor import ...` line:

```python
from integration_layer.core.customer_registry import CustomerRegistry
from integration_layer.core.data_delivery import DeliveryRecord, DeliveryService
```

Add a default output dir constant near the existing `_DEFAULT_MAX_POLLS`/`_DEFAULT_POLL_INTERVAL_S` constants:

```python
_DEFAULT_DELIVERY_OUTPUT_DIR = Path("deliveries")
```

(add `from pathlib import Path` to the top-level imports alongside the existing `from __future__ import annotations` line)

Replace `__init__`:

```python
    def __init__(
        self,
        registry: AdapterRegistry,
        audit_log: AuditLog,
        analytics: MissionAnalytics | None = None,
        customer_registry: CustomerRegistry | None = None,
        delivery_output_dir: Path | None = None,
    ) -> None:
        self._registry = registry
        self._audit_log = audit_log
        self._analytics = analytics if analytics is not None else MissionAnalytics()
        self._customer_registry = customer_registry if customer_registry is not None else CustomerRegistry()
        delivery_service = DeliveryService(
            registry, self._customer_registry, delivery_output_dir or _DEFAULT_DELIVERY_OUTPUT_DIR
        )
        self._executor = MissionExecutor(registry, audit_log, self._analytics, delivery_service=delivery_service)
        self._mission_progress: dict[str, tuple[str, int]] = {}
        self._mission_step_history: dict[str, list[MissionStepStatus]] = {}
        self._missions: dict[str, Mission] = {}
```

Add a `customer_registry` property and `get_delivery_record` method, right after `get_fleet_stats`:

```python
    @property
    def customer_registry(self) -> CustomerRegistry:
        """The SAME CustomerRegistry instance this service's
        DeliveryService consults -- exposed so a caller (e.g. webapp's
        startup seeding) can register customers against the exact
        registry that will actually be checked at delivery time,
        whether this service constructed its own default registry or
        was handed one explicitly."""
        return self._customer_registry

    def get_delivery_record(self, mission_id: str) -> DeliveryRecord | None:
        return self._executor.get_delivery_record(mission_id)
```

In `_executor_for`, also pass `delivery_service` through to the throwaway `MissionExecutor` so a custom-poll-config call still delivers (same caveat already documented for analytics dedup applies identically here — no current caller exercises this):

```python
    def _executor_for(self, max_polls: int, poll_interval_s: float) -> MissionExecutor:
        if max_polls == _DEFAULT_MAX_POLLS and poll_interval_s == _DEFAULT_POLL_INTERVAL_S:
            return self._executor
        step_runner = StepRunner(max_polls=max_polls, poll_interval_s=poll_interval_s)
        return MissionExecutor(
            self._registry,
            self._audit_log,
            self._analytics,
            step_runner=step_runner,
            delivery_service=self._executor.delivery_service,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_multistep_missions.py -v`
Expected: all pass (existing + 3 new)

- [ ] **Step 5: Run the full regression gate**

Run:
```bash
.venv/bin/python -m pytest tests/test_adapter_contract.py tests/test_platform_agnosticism.py tests/test_webapp_api.py -v
.venv/bin/python -m pytest tests/ -v
```
Expected: 100% pass, pinned-file diff against pre-plan baseline still empty.

- [ ] **Step 6: Commit**

```bash
cd /Users/rohitmurali/iac-robot-integration
git add integration_layer/api/orchestration_service.py tests/test_multistep_missions.py
git commit -m "Wire CustomerRegistry + DeliveryService into OrchestrationService"
```

---

### Task 7: Webapp API — seed customers + background mock-customer server, delivery endpoints

**Files:**
- Modify: `webapp/app.py`
- Test: `tests/test_webapp_api.py` (extend)

**Interfaces:**
- Consumes: `Customer`, `CustomerRegistry` (`integration_layer.core.customer_registry`); `mock_customer.customer_server.app`; `werkzeug.serving.make_server`.
- Produces: `GET /api/missions/<mission_id>/delivery`, `GET /api/robots/<robot_id>/deliveries`, `GET /api/deliveries/<mission_id>/download`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_webapp_api.py`:

```python
def test_mission_delivery_endpoint_reflects_completed_mission(client):
    res = client.post("/api/missions", json={"capability": "inspect_thermal"})
    mission_id = res.get_json()["mission_id"]

    delivery_res = client.get(f"/api/missions/{mission_id}/delivery")
    delivery = delivery_res.get_json()

    assert delivery_res.status_code == 200
    assert delivery["mission_id"] == mission_id
    assert delivery["status"] in ("delivered", "partially_delivered", "push_failed", "no_subscribers")


def test_mission_delivery_endpoint_unknown_mission_returns_404(client):
    res = client.get("/api/missions/does-not-exist/delivery")

    assert res.status_code == 404
    assert "error" in res.get_json()


def test_robot_deliveries_endpoint_lists_that_robots_deliveries(client):
    client.post("/api/missions", json={"capability": "inspect_thermal"})

    res = client.get("/api/robots/spot-01/deliveries")
    deliveries = res.get_json()

    assert res.status_code == 200
    assert len(deliveries) >= 1
    assert deliveries[0]["mission_id"]


def test_robot_deliveries_endpoint_unknown_robot_returns_404(client):
    res = client.get("/api/robots/does-not-exist/deliveries")

    assert res.status_code == 404


def test_download_delivery_package_returns_zip_bytes(client):
    res = client.post("/api/missions", json={"capability": "inspect_thermal"})
    mission_id = res.get_json()["mission_id"]

    download_res = client.get(f"/api/deliveries/{mission_id}/download")

    assert download_res.status_code == 200
    assert download_res.data[:2] == b"PK"  # zip file magic bytes


def test_download_delivery_package_unknown_mission_returns_404(client):
    res = client.get("/api/deliveries/does-not-exist/download")

    assert res.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_webapp_api.py -v`
Expected: FAIL — `404 != 200` / `AttributeError` on the new endpoints (they don't exist yet)

- [ ] **Step 3: Write the implementation**

Edit `webapp/app.py`.

Add imports, after the existing `from mock_robot.mock_robot_node import MockRobotAdapter` line (`threading` is already imported at the top of this file for the SSE live-mission path — reuse that same module, do not re-import it):

```python
from werkzeug.serving import make_server

from integration_layer.core.customer_registry import Customer, CustomerRegistry
from mock_customer.customer_server import app as customer_server_app
```

Replace the fleet/service setup block:

```python
registry = AdapterRegistry()
audit_log = AuditLog()
service = OrchestrationService(registry, audit_log)
```

with:

```python
registry = AdapterRegistry()
audit_log = AuditLog()

# A second, real local Flask server simulating where delivered mission
# reports actually land -- started once, in a background thread, for
# the life of this process. OS-assigned port (0) avoids clashing with
# webapp's own port or any other process on this machine.
_customer_server = make_server("127.0.0.1", 0, customer_server_app)
_customer_server_thread = threading.Thread(target=_customer_server.serve_forever, daemon=True)
_customer_server_thread.start()
CUSTOMER_SERVER_URL = f"http://127.0.0.1:{_customer_server.server_port}"

customer_registry = CustomerRegistry()
service = OrchestrationService(registry, audit_log, customer_registry=customer_registry)
```

Add a `_seed_customers()` function right after `_seed_fleet()`:

```python
def _seed_customers() -> None:
    customer_registry.register(Customer(
        customer_id="acme-industrial",
        webhook_url=f"{CUSTOMER_SERVER_URL}/webhook/deliveries",
        subscribed_capabilities=frozenset({CapabilityType.INSPECT_VISUAL, CapabilityType.INSPECT_THERMAL}),
        site_robot_ids=frozenset({"spot-01"}),
    ))
    customer_registry.register(Customer(
        customer_id="chemsafe-corp",
        webhook_url=f"{CUSTOMER_SERVER_URL}/webhook/deliveries",
        subscribed_capabilities=frozenset({CapabilityType.DETECT_GAS}),
        site_robot_ids=frozenset({"fleetx-01"}),
    ))


_seed_customers()
```

Add a `_delivery_dict` helper near the other `_*_dict` helpers:

```python
def _delivery_dict(record) -> dict:
    return {
        "mission_id": record.mission_id,
        "status": record.status.value,
        "has_package": record.package_path is not None,
        "delivered_customer_ids": record.delivered_customer_ids,
        "failed_customer_ids": record.failed_customer_ids,
    }
```

Add three new routes, after the existing `api_robot_stats` route and before `api_stats_query`:

```python
@app.get("/api/missions/<mission_id>/delivery")
def api_mission_delivery(mission_id: str):
    record = service.get_delivery_record(mission_id)
    if record is None:
        return jsonify({"error": f"no delivery record for mission_id {mission_id!r}"}), 404
    return jsonify(_delivery_dict(record))


@app.get("/api/robots/<robot_id>/deliveries")
def api_robot_deliveries(robot_id: str):
    if registry.get(robot_id) is None:
        return jsonify({"error": f"unknown robot_id {robot_id!r}"}), 404

    mission_ids = audit_log.mission_ids_for_robot(robot_id)
    deliveries = []
    for mission_id in mission_ids:
        record = service.get_delivery_record(mission_id)
        if record is not None:
            deliveries.append(_delivery_dict(record))
    return jsonify(deliveries)


@app.get("/api/deliveries/<mission_id>/download")
def api_download_delivery(mission_id: str):
    record = service.get_delivery_record(mission_id)
    if record is None or record.package_path is None:
        return jsonify({"error": f"no downloadable package for mission_id {mission_id!r}"}), 404
    return send_file(record.package_path, as_attachment=True, download_name=record.package_path.name)
```

Add `send_file` to the existing `from flask import ...` line:

```python
from flask import Flask, Response, jsonify, request, send_file
```

Update `/api/reset` to also re-seed customers against the fresh service's registry (replace the existing route body):

```python
@app.post("/api/reset")
def api_reset():
    global service
    registry.clear()
    audit_log.clear()
    _seed_fleet()
    customer_registry.clear()
    _seed_customers()
    service = OrchestrationService(registry, audit_log, customer_registry=customer_registry)
    return jsonify({"fleet": _fleet_snapshot()})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_webapp_api.py -v`
Expected: all pass (existing + 6 new)

- [ ] **Step 5: Run the full regression gate**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: 100% pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/rohitmurali/iac-robot-integration
git add webapp/app.py tests/test_webapp_api.py
git commit -m "Add webapp delivery endpoints + seeded customers + background mock-customer server"
```

---

### Task 8: GUI — "Deliveries" panel

**Files:**
- Modify: `webapp/templates/index.html`

**Interfaces:**
- Consumes: `GET /api/robots/<robot_id>/deliveries`, `GET /api/deliveries/<mission_id>/download` (Task 7).

- [ ] **Step 1: Add the new section**

In `webapp/templates/index.html`, add a new `<section class="wide">` immediately after the existing "Mission log" section (after its closing `</section>` at line 116, before the "Fleet analytics" section):

```html
  <section class="wide">
    <h2>Deliveries</h2>
    <p style="font-size:12px; color:#9aa3b5; margin:0 0 10px;">Per-robot mission data packages and customer webhook delivery status. Click "View mission log" on a robot card above, then look here.</p>
    <div id="deliveries-body"></div>
  </section>
```

- [ ] **Step 2: Add the render function**

Add a `loadDeliveries(robotId)` function right after the existing `loadMissionLog` function:

```javascript
function deliveryPillClass(status) {
  if (status === "delivered") return "pill-completed";
  if (status === "push_failed" || status === "upload_failed") return "pill-failed";
  return "pill-progress";
}

async function loadDeliveries(robotId) {
  const res = await fetch(`/api/robots/${robotId}/deliveries`);
  const deliveries = await res.json();
  const body = document.getElementById("deliveries-body");
  if (deliveries.length === 0) {
    body.innerHTML = `<p style="font-size:12px; color:#9aa3b5;">${robotId} has no delivered missions yet.</p>`;
    return;
  }
  body.innerHTML = `
    <h3 style="font-size:13px; margin: 10px 0 6px;">${robotId}'s deliveries</h3>
    <table>
      <thead><tr><th>Mission</th><th>Status</th><th>Delivered to</th><th>Failed for</th><th>Package</th></tr></thead>
      <tbody>
        ${deliveries.map(d => `
          <tr>
            <td>${d.mission_id}</td>
            <td><span class="pill ${deliveryPillClass(d.status)}">${d.status}</span></td>
            <td>${d.delivered_customer_ids.join(", ") || "-"}</td>
            <td>${d.failed_customer_ids.join(", ") || "-"}</td>
            <td>${d.has_package ? `<a href="/api/deliveries/${d.mission_id}/download" style="color:#8fb4ff;">download</a>` : "-"}</td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  `;
}
```

- [ ] **Step 3: Call it alongside the existing mission-log load**

Find the existing `data-mission-log-btn` wiring inside `wireFleetControls()`:

```javascript
  document.querySelectorAll("[data-mission-log-btn]").forEach(btn => {
    btn.addEventListener("click", () => loadMissionLog(btn.dataset.missionLogBtn));
  });
```

Replace it with:

```javascript
  document.querySelectorAll("[data-mission-log-btn]").forEach(btn => {
    btn.addEventListener("click", () => {
      loadMissionLog(btn.dataset.missionLogBtn);
      loadDeliveries(btn.dataset.missionLogBtn);
    });
  });
```

- [ ] **Step 4: Manually verify**

Run: `.venv/bin/python webapp/app.py` (in a background terminal), then in a browser open `http://127.0.0.1:5055`, click "Run mission" for a thermal/visual/gas capability, click "View mission log" on `spot-01` or `fleetx-01`, and confirm the new Deliveries table renders with a status pill and a working download link. Stop the server after confirming (Ctrl-C).

- [ ] **Step 5: Commit**

```bash
cd /Users/rohitmurali/iac-robot-integration
git add webapp/templates/index.html
git commit -m "Add Deliveries panel to the GUI: per-robot delivery status + package download"
```

---

### Task 9: Demo — delivery scenario in `demo/run_demo.py`

**Files:**
- Modify: `demo/run_demo.py`

**Interfaces:**
- Consumes: `OrchestrationService(customer_registry=..., delivery_output_dir=...)` (Task 6); `Customer`, `CustomerRegistry` (`integration_layer.core.customer_registry`); `mock_customer.customer_server.app`; `werkzeug.serving.make_server`.

- [ ] **Step 1: Add a delivery demo section**

Add new imports after the existing `from mock_robot.mock_robot_node import MockRobotAdapter` line:

```python
import threading

from werkzeug.serving import make_server

from integration_layer.core.customer_registry import Customer, CustomerRegistry
from mock_customer.customer_server import app as customer_server_app
```

Add a new function, after `run_resilience_demo` and before `main`:

```python
def run_delivery_demo(service: OrchestrationService, spot: MockRobotAdapter, log) -> None:
    """Demonstrates post-mission data capture, upload, and customer
    delivery under two conditions: (1) nominal -- a thermal-inspection
    mission's simulated sensor artifact is captured, uploaded, packaged,
    and pushed to a subscribed customer over a REAL local HTTP call;
    print the actual payload the mock-customer server received. (2) the
    robot goes offline right before the mission completes -- the
    mission itself still COMPLETES, but its data never makes it off the
    robot (DeliveryStatus.UPLOAD_FAILED) -- the "bandwidth-limited site"
    story made concrete."""
    log("\n" + "=" * 72)
    log("Delivery demo -- post-mission data capture, upload, and customer delivery")
    log("=" * 72)

    log("\n--- 1. Nominal: thermal inspection, data delivered to a real subscribed customer ---")
    nominal = Mission(
        mission_id="mission-thermal-for-delivery",
        steps=[MissionStep(capability_type=CapabilityType.INSPECT_THERMAL, target="wp1")],
    )
    service.assign_mission(nominal)
    service.wait_for_completion(nominal.mission_id)
    record = service.get_delivery_record(nominal.mission_id)
    log(f"  delivery status: {record.status.value}")
    log(f"  delivered to: {record.delivered_customer_ids}")
    import urllib.request
    import json as json_module
    with urllib.request.urlopen(f"{CUSTOMER_SERVER_URL_FOR_DEMO}/admin/received", timeout=2.0) as response:
        received = json_module.loads(response.read())
    log(f"  mock-customer server actually received: {received[-1] if received else None}")

    log("\n--- 2. Robot goes offline for the whole mission: mission itself fails, delivery is never attempted ---")
    log("  (DeliveryService.deliver() is only reachable through a COMPLETED mission -- the publicly")
    log("   reachable way to show 'data never makes it off the robot' is the mission failing outright;")
    log("   the UPLOAD_FAILED status itself -- a mission that completes but whose data upload separately")
    log("   fails -- is exercised directly in tests/test_data_delivery.py, which can force that exact")
    log("   split via a FaultProfile the public API has no way to toggle mid-mission.)")
    spot.set_offline(True)
    offline_mission = Mission(
        mission_id="mission-thermal-offline",
        steps=[MissionStep(capability_type=CapabilityType.INSPECT_THERMAL, target="wp1")],
    )
    service.assign_mission(offline_mission)
    final_status = service.wait_for_completion(offline_mission.mission_id)
    log(f"  mission final status: {final_status.status.value} (module=comms, same retry policy, mission itself fails)")
    log(f"  delivery record: {service.get_delivery_record(offline_mission.mission_id)} (None -- deliver() is never called for a non-COMPLETED mission)")
    spot.set_offline(False)
```

- [ ] **Step 2: Wire it into `main()`**

Replace the existing fleet/service construction block inside `main()`:

```python
    registry = AdapterRegistry()
    spot = MockRobotAdapter(
        robot_id="spot-01", payload={"visual_camera", "thermal_camera"}
    )
    fleetx = CloudFleetAdapter(
        robot_id="fleetx-01", fleet_manifest={"sensors": ["voc_sensor"]}
    )
    registry.register(spot)
    registry.register(fleetx)
```

with (add the customer-server startup and customer registry immediately after):

```python
    registry = AdapterRegistry()
    spot = MockRobotAdapter(
        robot_id="spot-01", payload={"visual_camera", "thermal_camera"}
    )
    fleetx = CloudFleetAdapter(
        robot_id="fleetx-01", fleet_manifest={"sensors": ["voc_sensor"]}
    )
    registry.register(spot)
    registry.register(fleetx)

    customer_server = make_server("127.0.0.1", 0, customer_server_app)
    customer_server_thread = threading.Thread(target=customer_server.serve_forever, daemon=True)
    customer_server_thread.start()
    global CUSTOMER_SERVER_URL_FOR_DEMO
    CUSTOMER_SERVER_URL_FOR_DEMO = f"http://127.0.0.1:{customer_server.server_port}"

    customer_registry = CustomerRegistry()
    customer_registry.register(Customer(
        customer_id="acme-industrial",
        webhook_url=f"{CUSTOMER_SERVER_URL_FOR_DEMO}/webhook/deliveries",
        subscribed_capabilities=frozenset({CapabilityType.INSPECT_THERMAL, CapabilityType.INSPECT_VISUAL}),
        site_robot_ids=frozenset({"spot-01"}),
    ))
```

Add the module-level global declaration near the top of the file, after `OUTPUT_DIR = Path(__file__).resolve().parent / "output"`:

```python
CUSTOMER_SERVER_URL_FOR_DEMO: str = ""
```

Replace the existing `service = OrchestrationService(registry, audit_log)` line with:

```python
    service = OrchestrationService(
        registry, audit_log, customer_registry=customer_registry, delivery_output_dir=OUTPUT_DIR / "deliveries"
    )
```

Add the call to `run_delivery_demo`, right after the existing `run_resilience_demo(service, spot, log)` call:

```python
    run_resilience_demo(service, spot, log)
    run_delivery_demo(service, spot, log)
```

Add a clean shutdown at the very end of `main()`, right before `return records`:

```python
    customer_server.shutdown()
    customer_server_thread.join(timeout=2)

    return records
```

- [ ] **Step 3: Run the demo**

Run:
```bash
cd /Users/rohitmurali/iac-robot-integration
.venv/bin/python demo/run_demo.py
```
Expected: runs cleanly to completion, no exceptions, prints the new "Delivery demo" section with a real received-payload line and the offline-upload scenario; `demo/output/demo_run.txt` and `demo/output/deliveries/*.zip` are written.

- [ ] **Step 4: Commit**

```bash
cd /Users/rohitmurali/iac-robot-integration
git add demo/run_demo.py demo/output/
git commit -m "Add delivery demo scenario: real webhook delivery + offline-upload failure story"
```

---

### Task 10: Final pass — regression, `.gitignore`, README/DESIGN.md, webapp artifact refresh

**Files:**
- Modify: `.gitignore`
- Modify: `README.md`
- Modify: `DESIGN.md`
- Modify: `webapp/output/*` (regenerated, not hand-edited)

**Interfaces:** none (documentation + regression only)

- [ ] **Step 1: `.gitignore`**

Add one line to `.gitignore` (after the existing `.pytest_cache/` line):

```
deliveries/
```

(This is webapp's runtime-generated output dir, created fresh on every process start — `demo/output/deliveries/` is a DIFFERENT path and stays committed, matching how `demo/output/demo_run.txt` and `demo/output/mission_timeline.png` are already committed as evidence.)

- [ ] **Step 2: Final regression gate**

Run:
```bash
cd /Users/rohitmurali/iac-robot-integration
.venv/bin/python -m pytest tests/ -v
git diff <pre-plan-baseline-recorded-before-task-1> HEAD -- tests/test_adapter_contract.py tests/test_platform_agnosticism.py
```
Confirm 100% pass and the pinned-file diff is genuinely empty.

- [ ] **Step 3: Refresh `webapp/output/*`**

Run:
```bash
.venv/bin/python webapp/record_gui_demo.py
```
Confirm it completes without error and the new Deliveries section is visible or at least doesn't break the recording (consistent with Task 12 of the prior plan's precedent: a page section extending below the viewport is not a defect).

- [ ] **Step 4: `README.md` updates**

Read the current `README.md` in full first (it has sections: "Live GUI", "Multi-step missions, resilience, and fleet analytics", "Proof of concept (CLI form)", "What's real vs. illustrative", "Layout", "How I used AI").

Add a new top-level section, after "Multi-step missions, resilience, and fleet analytics" and before "Proof of concept (CLI form)", titled "Post-mission data capture, upload, and customer delivery", covering:
- Sensor-capability steps (`inspect_visual`/`inspect_thermal`/`detect_gas`) capture a simulated local artifact the instant they complete, via `ArtifactCapture` (`integration_layer/core/artifact_capture.py`) — no network involved.
- The moment a mission reaches `COMPLETED`, `MissionExecutor` automatically batch-uploads the mission's captured artifacts from "robot" to "platform" — the one network-dependent operation in this flow, subject to the SAME `FaultProfile` and retried via the SAME `retry_policy` COMMS strategy already governing mission-step execution. A robot that's offline right when its mission completes can still COMPLETE the mission while its data never makes it off the robot (`DeliveryStatus.UPLOAD_FAILED`) — see `demo/run_demo.py`'s delivery demo section.
- On a successful upload, `DeliveryPackager` builds a `mission-report-<id>.zip` (manifest + artifact files), and `CustomerRegistry` finds every customer subscribed to that robot/capability combination (`integration_layer/core/customer_registry.py`) for a real local HTTP webhook push, retried via the same COMMS policy, against `mock_customer/customer_server.py` — a genuinely separate, real local Flask process simulating a customer's receiving system, not a mocked call.
- Point to the new GUI "Deliveries" panel (per-robot delivery status + package download link) and the new `/api/missions/<id>/delivery`, `/api/robots/<id>/deliveries`, `/api/deliveries/<id>/download` endpoints.
- Note the explicit production mapping and scaling-path caveats (copy from `docs/superpowers/specs/2026-10-13-post-mission-data-delivery-design.md`'s "Design decisions worth documenting" section, condensed): the downloadable zip would really be an S3 object behind a presigned URL; the webhook push would really hit the customer's registered AWS API Gateway/EventBridge endpoint (not implemented here — no payload signing/auth either, a documented gap); at scale, bundling bytes into a zip doesn't work — production stores metadata in a document/relational store and artifacts in object storage by key, and bulk analytics export would use Parquet/Avro, not JSON.

Update the "Live GUI" section: add one sentence noting the new Deliveries panel (per-robot package download + webhook delivery status).

Update the test-count citation wherever the README currently says "155/155" (from the prior plan's final pass) to the actual current `pytest tests/` count from Step 2 above.

Update "What's real vs. illustrative": add a bullet noting `mock_customer/customer_server.py` is a REAL local HTTP process (runs, is tested, hit with genuine requests) while the AWS S3/API-Gateway production mapping described in the new section above is illustrative only — not built, not called.

Update the "Layout" section's file tree, adding:
```
integration_layer/
  core/
    customer_registry.py             # which customers get which mission's delivered data
    artifact_capture.py              # local, no-network per-step simulated sensor capture
    data_delivery.py                 # two-phase upload+push delivery, both retried via retry_policy
mock_customer/
  customer_server.py                 # real local Flask app simulating a customer's receiving system
tests/
  test_customer_registry.py
  test_artifact_capture.py
  test_customer_server.py
  test_data_delivery.py
```
(Check the actual final file list via `find integration_layer mock_robot mock_customer tests -name "*.py" | sort` and make sure this matches reality rather than this plan's guess.)

Append a new paragraph to "How I used AI" covering this round honestly: the user asked for post-mission data download/upload/delivery-to-customers plus design-decision documentation; this went through the brainstorming skill's full Q&A cycle (capture scope, delivery mechanism, upload direction, webhook target — including redirecting from "wire in real AWS" to "a real local mock-customer server with an explicit documented production mapping," which the user explicitly chose over the costlier/riskier real-AWS option) before any code was written; name the one design refinement made while writing the spec (splitting "upload" into two independently-retried network legs) and flag that it was a refinement, not something separately asked.

- [ ] **Step 5: `DESIGN.md` updates**

Read `DESIGN.md` in full first. If it has a section discussing chaos/fault-tolerance or data handling that this plan's work extends, add a short paragraph there (matching its existing style) pointing to `integration_layer/core/data_delivery.py` as a concrete implementation of "what happens to mission data after a mission completes," and the explicit, documented AWS-production mapping (S3 presigned URL, API Gateway/EventBridge) as the honest boundary between what's built vs. illustrative here.

- [ ] **Step 6: Final verification and commit**

Run:
```bash
.venv/bin/python -m pytest tests/ -v   # one more time, after all doc/artifact changes
```
Commit:
```bash
cd /Users/rohitmurali/iac-robot-integration
git add .gitignore README.md DESIGN.md webapp/output/
git commit -m "Final pass: regression, .gitignore, README/DESIGN.md updates for data delivery pipeline"
```

## Self-review checklist (confirm all before reporting DONE)

1. The pinned regression gate (`test_adapter_contract.py`, `test_platform_agnosticism.py`) shows a genuinely empty `git diff` against the commit from BEFORE this plan's work began — paste the actual command and its (empty) output.
2. `demo/output/*` and `webapp/output/*` changes were produced by actually running the real scripts, not hand-edited.
3. Every existing `MissionExecutor(...)`/`OrchestrationService(...)` call site in the codebase (grep for both) still works with zero changes — new parameters are additive-only.
4. `mock_customer/customer_server.py` is hit with genuine local HTTP calls in at least one test per consuming module (`test_customer_server.py`, `test_data_delivery.py`'s `running_customer_server`-based tests, Task 7's webapp tests indirectly via the background thread) — not mocked anywhere.
5. The README's new section and Layout tree match the actual final file list, not this plan's guess.
