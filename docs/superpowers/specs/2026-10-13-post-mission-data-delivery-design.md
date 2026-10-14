# Post-Mission Data Capture, Upload, and Customer Delivery — Design

## Context

The platform today executes multi-step missions, retries failures per `SystemModule`, and updates fleet/robot analytics the instant a mission ends — but nothing happens to the *data* a mission produces. A completed `inspect_thermal`/`inspect_visual`/`detect_gas` mission has no artifact, no report, and no way for the customer who owns/leases that robot to actually receive anything.

This design adds: (1) local, no-network capture of simulated sensor artifacts as sensor-capability steps complete, (2) a single batch upload of those artifacts from "robot" to "platform" once the mission reaches `COMPLETED` — itself subject to the same network fault injection and COMMS retry policy already governing mission execution, (3) packaging into a downloadable report, and (4) a real local HTTP webhook push to whichever customers are subscribed to that robot/capability combination.

**Non-goals:** no real AWS services, no payload signing/auth on the webhook, no persistence beyond process memory + local disk, no GUI for registering customers at runtime (static registry, matching how `AdapterRegistry` is seeded today).

## Architecture

### New module: `integration_layer/core/customer_registry.py`

```python
@dataclass(frozen=True)
class Customer:
    customer_id: str
    webhook_url: str
    subscribed_capabilities: frozenset[CapabilityType]
    site_robot_ids: frozenset[str]   # which robots are deployed at this customer's site

class CustomerRegistry:
    def register(self, customer: Customer) -> None: ...
    def customers_for(self, robot_id: str, capability_type: CapabilityType) -> list[Customer]:
        """Customers whose site_robot_ids contains robot_id AND whose
        subscribed_capabilities contains capability_type. A mission with
        mixed steps queries this once per distinct capability type present
        (excluding NAVIGATE, which has no deliverable product) and the
        results are unioned/deduplicated by customer_id."""
```

Seeded at startup the same way `webapp/app.py`'s `_seed_fleet()` seeds the two OEM robots — a `_seed_customers()` with 2-3 example customers.

### New module: `integration_layer/core/artifact_capture.py`

```python
SENSOR_CAPABILITIES = {CapabilityType.INSPECT_VISUAL, CapabilityType.INSPECT_THERMAL, CapabilityType.DETECT_GAS}

@dataclass(frozen=True)
class CapturedArtifact:
    step_index: int
    capability_type: CapabilityType
    filename: str
    content: bytes   # simulated payload (explicitly illustrative, not a real sensor frame)

class ArtifactCapture:
    """No network, no I/O to platform -- purely local, in-memory 'robot disk'
    simulation. MissionExecutor calls capture() the instant a sensor-capability
    step status transitions to COMPLETED (NOT on NAVIGATE steps, which produce
    no artifact). Keeps a dict[mission_id, list[CapturedArtifact]] until
    DeliveryService consumes and clears it at mission-terminal time."""
    def capture(self, mission_id: str, step_index: int, capability_type: CapabilityType) -> CapturedArtifact: ...
    def drain(self, mission_id: str) -> list[CapturedArtifact]: ...  # returns + clears
```

### New module: `integration_layer/core/data_delivery.py`

Two network-facing phases, each retried independently via the **existing** `retry_policy.strategy_for()`/`backoff_for_attempt()` — no new retry logic is written:

```python
class DeliveryStatus(str, Enum):
    PENDING = "pending"
    UPLOAD_FAILED = "upload_failed"     # robot->platform leg exhausted retries
    PACKAGED = "packaged"
    DELIVERED = "delivered"             # all subscribed customers received it
    PARTIALLY_DELIVERED = "partially_delivered"  # some customers failed
    NO_SUBSCRIBERS = "no_subscribers"   # packaged, nobody was subscribed

@dataclass(frozen=True)
class DeliveryRecord:
    mission_id: str
    status: DeliveryStatus
    package_path: Path | None
    delivered_customer_ids: list[str]
    failed_customer_ids: list[str]

class DeliveryService:
    def __init__(self, registry: AdapterRegistry, customer_registry: CustomerRegistry,
                 output_dir: Path, http_post: Callable[..., Response] = requests.post) -> None: ...

    def deliver(self, mission: Mission, final_status: MissionStepStatus,
                 step_history: list[MissionStepStatus], artifacts: list[CapturedArtifact]) -> DeliveryRecord:
        """Called ONLY when final_status.status == MissionStatus.COMPLETED
        (failed/aborted/paused missions produce no package -- nothing
        real to ship). Two phases:

        Phase 1 -- upload (robot -> platform): simulates the robot batch-
        uploading `artifacts` to the platform. Consults
        self.registry.get(mission.assigned_robot_id).get_fault_profile()
        -- the SAME FaultProfile already driving mission-step fault
        injection -- to decide per-attempt success, retried via
        retry_policy.strategy_for(FailureReason.CONNECTIVITY_LOST) (COMMS:
        4 attempts, backoff 0/1/2/2...). Exhausting retries returns a
        DeliveryRecord(status=UPLOAD_FAILED) immediately -- no package,
        no customer push. This is the "bandwidth-limited site" story:
        the mission itself can succeed while its data never makes it off
        the robot.

        Phase 2 -- package + push (platform -> customer): on upload
        success, builds the zip via a packager, finds customers via
        customer_registry.customers_for() across every distinct
        capability type in mission.steps, and does a REAL local HTTP
        POST to each customer.webhook_url (mock_customer/customer_server.py
        in dev/test). A failed POST (connection error, non-2xx, or the
        mock server's simulated-outage mode) is retried via the SAME
        COMMS retry_policy. Final status is DELIVERED / PARTIALLY_DELIVERED
        / NO_SUBSCRIBERS accordingly.
        """

    def build_package(self, mission: Mission, final_status: MissionStepStatus,
                        step_history: list[MissionStepStatus], artifacts: list[CapturedArtifact]) -> Path:
        """Writes deliveries/mission-report-<mission_id>.zip containing
        mission_report.json (steps, final status, retry tally, a fleet/
        robot analytics snapshot) + one file per CapturedArtifact."""
```

### New package: `mock_customer/customer_server.py`

A second, independent Flask app (mirrors the `mock_robot/` naming convention) simulating a customer's receiving system:
- `POST /webhook/deliveries` — records the received payload in memory (`list[dict]`), returns 200.
- `POST /admin/simulate_outage` / `POST /admin/recover` — toggles the server into returning 503, for deterministic "customer endpoint is down" demo/test scenarios without relying on real network flakiness.
- `GET /admin/received` — test/demo introspection endpoint to assert on what arrived.

This is a **real** process making **real** local HTTP calls — consistent with this repo's "hit real dependencies, don't mock" testing philosophy, the same way the two OEM adapters are real (if simulated) implementations rather than mocked interfaces.

### Integration point: `integration_layer/core/mission_executor.py`

Two hook additions to `MissionExecutor`, both additive (no change to existing signatures):
1. Inside `continue_mission`'s step loop, immediately after a step's `status.status == MissionStatus.COMPLETED` is observed, if `step.capability_type in SENSOR_CAPABILITIES`: call `self.artifact_capture.capture(mission.mission_id, step_index, step.capability_type)`.
2. Immediately after the existing terminal-state block (`mission_executor.py:150-154`, where `analytics.record_mission_outcome(...)` already fires exactly once per mission_id) — guarded by `status.status == MissionStatus.COMPLETED` — call `self.delivery_service.deliver(mission, status, step_history, self.artifact_capture.drain(mission.mission_id))`. Both `artifact_capture` and `delivery_service` are constructor-injected with safe defaults (`field(default_factory=...)`), matching how `step_runner`/`analytics` are already injected — zero changes required to any existing call site or test.

### GUI: `webapp/app.py` / `webapp/templates/index.html`

- New `GET /api/missions/<mission_id>/delivery` — delivery status + package path (if any) for a mission.
- New `GET /api/deliveries/<mission_id>/download` — streams the zip via Flask `send_file`.
- New "Deliveries" panel: per completed mission, download link, delivery status, which customer(s) received it.

## Error handling

- Mission `FAILED`/`ABORTED`/`PAUSED_FOR_INTERVENTION` → `deliver()` is never called; no partial/misleading package ever gets built for a mission that didn't actually finish.
- Upload-phase and push-phase failures both funnel through the existing `FailureReason` → `SystemModule.COMMS` → `RetryStrategy` table — no bespoke retry/backoff math anywhere in this feature.
- A customer whose webhook is unreachable after retries does not block delivery to other subscribed customers — each customer's push is independent; the record just ends up `PARTIALLY_DELIVERED`.

## Testing

- `tests/test_customer_registry.py` — subscription-matching logic (capability ∩ site-robot intersection), dedup across mixed-capability missions.
- `tests/test_artifact_capture.py` — capture only fires for sensor capabilities, `drain()` clears state, isolated per mission_id.
- `tests/test_data_delivery.py` — packaging format (zip contents, manifest shape), upload-phase retry/backoff against a fake/forced `FaultProfile` (same pattern as existing fault-injection tests), push-phase against a **real** `customer_server.py` Flask test client (not a mock) asserting the actual received payload, outage-toggle-then-retry-recovers scenario.
- `tests/test_mission_executor.py` (extend) — terminal hook fires `deliver()` exactly once per mission on COMPLETED, never on other terminal states.
- Regression gate unchanged: `tests/test_adapter_contract.py`, `tests/test_platform_agnosticism.py` must still show an empty diff against `ac6e0bc`.

## Demo

`demo/run_demo.py` gains a delivery section after the existing resilience narrative: (1) a nominal mission whose data is captured, uploaded, packaged, and pushed to a subscribed customer — print the received webhook payload from the real `customer_server.py` process; (2) a robot-offline-during-upload scenario showing `DeliveryStatus.UPLOAD_FAILED` despite the mission itself `COMPLETED` — the "bandwidth-limited site" story made concrete.

## Design decisions worth documenting (new `DESIGN.md`/README section)

- **Why batch-upload-at-mission-end, not per-step upload:** keeps network risk concentrated in one well-understood retry path instead of exposing every sensor step to upload latency/failure; matches how real field robots often cache-and-sync rather than stream.
- **Why a real local mock-customer server, not a logged/simulated push:** consistent with this repo's existing practice of real (if simulated) components over mocked interfaces — the retry logic is exercised against genuine connection failures/non-2xx responses, not an imagined one.
- **Explicit production mapping:** the downloadable zip would really be an object in S3 behind a presigned URL (not a file assembled and held by this process); the webhook push would really hit the customer's registered AWS API Gateway/EventBridge endpoint, likely with HMAC-signed payloads (not implemented here — noted as a gap).
- **Scaling path beyond this PoC:** bundling bytes into a per-mission zip doesn't scale — production would store mission metadata in a document/relational store and artifacts in object storage referenced by key, generating download URLs on demand. Bulk analytics export across many missions would use Parquet/Avro, not JSON, for the same reason the audit log is indexed rather than scanned.

## Open items for spec review

None outstanding — all decisions in this doc were resolved through direct Q&A before writing. One refinement made while writing (not separately asked): splitting "upload" into two distinct network legs (robot→platform, reusing the mission's own `FaultProfile`; platform→customer, reusing the same retry policy against a real HTTP call) rather than one undifferentiated "upload" step — this gives two independently demonstrable failure stories (bad robot network vs. unreachable customer endpoint) from one retry mechanism.
