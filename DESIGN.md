# IAC Technical Assessment: Robot Integration Layer

Author: [your name]

This document is my answer to the Robot Integration Layer design exercise. It covers architecture, scope boundaries, build-vs-partner decisions, a 90-day execution plan, reliability, and team/customer process. A companion code scaffold lives alongside this doc (see `README.md`) and implements the core ideas below against a generic ROS2 robot.

**This design is also backed by a working proof of concept, not just description.** `mock_robot/` contains two genuinely different OEM adapters (a local/ROS2-style one and a cloud/REST-style one with async job polling and its own error vocabulary) behind the single `RobotAdapter` interface described in section 1. `tests/test_platform_agnosticism.py` proves a mission can be redirected from one OEM to the other by changing data, not platform code. There's both a CLI demo (`demo/run_demo.py`) and a real browser GUI (`webapp/`, a Flask dashboard with live fleet status, mission controls, and an audit trail) exercising the exact same `OrchestrationService` — including a recorded browser session (`webapp/output/gui_demo.webm`) driving the real GUI in headless Chromium. See `README.md` for how to run either — including real bugs the process caught while building both.

---

## 1. Integration Layer Architecture

### The problem with the obvious approach

The naive path is: every time we add a robot, someone writes a driver that talks to that robot's SDK and exposes whatever ad hoc API the orchestration core happens to need that week. This works for robot #1. By robot #4, orchestration core has four different mental models of "what a robot is," mission logic has robot-specific branches, and every OEM firmware update risks breaking something upstream that has no business knowing the robot exists. This is the failure mode the assignment is asking how to avoid.

### The core decision: capability-based contract, not per-robot RPC

The Integration Layer exposes **capabilities**, not robots. A capability is a named unit of physical work with a declared parameter schema: `navigate`, `inspect_visual`, `inspect_thermal`, `detect_gas`, `manipulate`, `weld`. A robot advertises which capabilities it has, given its current payload. A mission is an ordered sequence of capability invocations with parameters, preconditions, timeouts, and a retry policy — expressed as data, not code.

Orchestration core never says "tell Spot #3 to walk to waypoint 12." It says "execute capability `navigate` with `{target: waypoint_12}` as part of mission M, using any robot that currently advertises `navigate` and satisfies the mission's other constraints." The Integration Layer resolves *which* robot and *how* to command it. This is the single design decision that keeps the driver problem from becoming an N-driver problem: orchestration core's code doesn't grow when robot #10 arrives, because robot #10 is just another implementation of a capability interface it already understands.

### Three layers

```
Orchestration Core  (mission planning, scheduling, approvals — not this doc)
        |
        |  gRPC/REST: capability + mission + status contract (schemas/mission.proto)
        v
Robot Integration Layer
  - Adapter Registry        (which adapters exist, what they advertise)
  - Mission Executor        (per-robot mission state machine, retries, timeouts)
  - Capability Contract     (the schema everything above and below agrees to)
  - Exception/Audit Pipeline (normalized failure taxonomy, event log)
        |
        |  fixed adapter interface (adapter_base.py): get_capabilities(),
        |  execute_mission_step(), get_status(), handle_exception(), estop()
        v
  OEM Adapters (one per robot/OEM)
  [ROS2-generic adapter] [OEM Fleet API adapter] [Vendor SDK adapter] ...
        |
        v
   Physical robots
```

New robot onboarding = implement the five adapter methods against real SDK/ROS2 calls, then pass a shared **conformance test suite**. The conformance suite is what actually enforces the contract over time — without it, "every adapter implements the same interface" quietly rots into "every adapter implements roughly the same interface, except for the three places someone was in a hurry." The suite is the thing that would catch that, in CI, before it reaches the field.

### Worked example: a generic ROS2 robot

The companion code (`integration_layer/adapters/ros2_generic/adapter.py`) shows this concretely. A ROS2-based mobile robot typically exposes:

- A `nav2`-style `NavigateToPose` or `FollowWaypoints` action server for locomotion
- Sensor topics (`/camera/image_raw`, `/thermal/image`, `/gas_sensor/reading`) depending on payload
- `/battery_state`, `/odom` for health/telemetry
- An e-stop service/topic for safety

The adapter's `get_capabilities()` introspects which topics/action servers are actually present on this robot instance and returns `navigate` plus whichever sensor capabilities the payload supports — this is how the same adapter class handles a bare quadruped and one with a thermal camera bolted on, without a new adapter per configuration. `execute_mission_step()` translates a capability invocation (`{capability: "inspect_thermal", target: waypoint, duration_s: 30}`) into a `NavigateToPose` goal followed by a thermal-topic subscription for the mission window, and reports back through the standard `MissionStepStatus` the orchestration core actually understands.

The **hot-path split** matters here too: the Python adapter is not in the real-time loop. A separate C++ ROS2 node (`cpp_nodes/safety_monitor_node.cpp`) runs an independent watchdog — proximity/e-stop monitoring at sensor-rate frequency — and a second C++ node (`command_bridge_node.cpp`) handles high-rate velocity/joint command relay when a mission step needs tighter control-loop timing than Python's GC and interpreter overhead can reliably guarantee. The Python adapter supervises these nodes and reads their state, but doesn't sit between them and the robot's actual control loop. This is a deliberate, narrow use of C++ — not a rewrite of the framework — applied only where latency is safety- or performance-critical.

---

## 2. Fit Into the Bigger Picture

### What belongs in the Integration Layer

- The capability/mission/status **contract** (schema, versioned)
- The **adapter framework** and the fixed interface every OEM adapter implements
- The **mission executor**: per-robot-assignment state machine (`QUEUED → ASSIGNED → IN_PROGRESS → PAUSED_FOR_INTERVENTION → COMPLETED/FAILED/ABORTED`), retries, timeouts
- **Adapter registry**: what capabilities exist right now, on which robots, given current payload/battery/location
- **Exception taxonomy**: translating OEM-specific error codes into a normalized failure vocabulary orchestration core can reason about
- **Audit/event log**: every state transition, every operator intervention, every safety trigger — timestamped and attributable
- **Safety supervision pattern**: the watchdog/e-stop bridging concept (the specific triggers may be OEM-tuned inside the adapter, but the pattern and its reporting contract are shared)

### What does not belong here

- **Mission planning and scheduling** — deciding which robot does what, when, and in what order across the fleet is an orchestration-core concern; the Integration Layer executes an assigned mission, it doesn't decide to assign one.
- **Approvals workflow, business rules, billing** — orchestration core / product layer.
- **Customer-facing dashboards and reporting** — consumers of the audit/event stream, not part of producing it.
- **Facility system business logic** (e.g., "if gas reading exceeds X, halt all missions in zone Y") — the Integration Layer can expose facility sensors as another capability-like data source, but the *decision* using that data is orchestration's job. The boundary is: Integration Layer produces normalized signals; orchestration core consumes them and decides.

### The design principle

**Narrow, stable, versioned contract at the top. Wide, disposable, vendor-specific mess at the bottom. A conformance boundary between them enforced by tests, not by convention.**

The interface between orchestration and robot execution is the capability/mission/status schema — it should never leak robot-specific concepts (ROS2 topic names, OEM auth tokens, SDK-specific error codes) above the Integration Layer. If orchestration core needs to know a topic name to work, the boundary has already failed. The practical test I'd apply continuously: *could we swap every physical robot fleet for a different vendor's equivalent hardware, and have orchestration core run completely unmodified?* If yes, the boundary is where it should be.

---

## 3. Build vs. Partner

### Build ourselves

The capability contract, the adapter framework, the mission executor, the conformance suite, and the audit/event pipeline. This is the actual differentiator behind "vendor-agnostic orchestration" — it's not outsourceable without outsourcing the company's thesis.

### Partner for

- **OEM fleet managers** — when an OEM already exposes a fleet-level API (multi-robot status, remote dispatch), integrate against that as an adapter's data source instead of reimplementing low-level per-robot SDK calls for every unit. Faster, and the OEM keeps it working across their own firmware changes.
- **InOrbit/Formant-style robot ops platforms** — use for cross-fleet telemetry visualization, teleop, and remote-intervention UI rather than building a competing observability stack in year one. Treat the ops platform as a consumer of our normalized telemetry (or, where a customer already has one deployed, as an additional adapter-level data source) — not as something the core contract depends on.
- **Customer facility systems** (access control, existing gas sensors, BMS/SCADA) — build a thin ingestion adapter; don't become a systems integrator. Partner with existing site integrators for physical/network commissioning.
- **Other partners**: private network providers for connectivity at remote/industrial sites; functional-safety/welding-certification consultants for regulated capabilities (domain expertise to contract, not to build in-house).

### Decision framework

Three questions, asked per capability or integration:

1. **Does this touch the core translation contract** — the capability↔OEM mapping or mission-execution semantics? If yes, build. This is the moat; a partner dependency here is a dependency on someone else's roadmap for our differentiation.
2. **Is this undifferentiated infrastructure** a customer would evaluate independently of IAC (dashboards, teleop UI, generic fleet visualization)? If yes, partner — speed beats control here, and building it ourselves is time spent proving nothing about the orchestration thesis.
3. **What's the lock-in/switching cost** if the partner's pricing changes or they pivot? Mitigate structurally: keep every partner integration behind the *same* adapter abstraction used for robots, so the partner is swappable rather than load-bearing.

As a rule of thumb: if building in-house would take more than ~2x the time of a partner integration, *and* the capability isn't part of what we're actually differentiating on, partner. Revisit the calculus at each fleet-scale milestone — a partner's pricing or fit that's fine at 10 robots may not hold at 500, so this isn't a one-time decision.

### How I'd evaluate partners

- **Technical fit**: does their data model map cleanly onto our capability contract, or does integrating them force a leaky abstraction upward?
- **Data access**: will they expose raw/normalized data, or only their own UI? If we can't get the data out, we're locked into their product surface, not just their infrastructure.
- **Reliability track record and SLA** — for anything in a safety- or mission-critical path.
- **Roadmap direction**: are they moving toward being embeddable infrastructure, or toward becoming a competing orchestration platform? The second is a reason to keep them at arm's length regardless of short-term fit.
- **Commercial terms and lock-in risk.**
- A real **technical pilot** — one robot, one mission type, before any broader commitment. No partner decision gets made off a sales deck.

---

## 4. First 90 Days

### Scoping the fleet by integration complexity, not business priority

Nine robots (4 quadrupeds, 5 mobile manipulators, heterogeneous configs) and five mission types. I'd order the mission types by architectural complexity, because that determines what's safe to promise by when — not by what sales wants first:

1. **Visual inspection** — camera + navigate. Lowest complexity, exercises the core capability model, read-only (low safety risk). First slice.
2. **Gas detection** — sensor payload + navigate. Same shape as #1 with a different sensor capability; proves the model handles payload heterogeneity without new architecture. Second slice.
3. **Thermal inspection** — same shape again, different sensor. By this point it should be close to free — if it isn't, that's a signal the capability abstraction isn't actually generalizing and needs to be fixed before adding more capability types.
4. **Material handling** — manipulation. Genuinely harder: grasp/pose parameters, physical contact, higher safety stakes, needs its own precondition-checking. Deferred past pure-mobility missions.
5. **Welding** — highest complexity and regulatory/safety weight. I would not target autonomous welding in 90 days. I'd scope it as a **supervised capability** — the Integration Layer tracks and audits a human-operated or teleoperated welding session — and treat full automation as a later phase, explicitly.

### Day 30 — Foundation

- Define capability schema v0: `navigate`, `inspect_visual`, `inspect_thermal`, `detect_gas` (sketch `manipulate`/`weld` schemas but don't finalize them yet). Define mission and status/audit schemas.
- Build the adapter interface and a conformance test suite v0.
- Stand up the mock-robot simulator so Integration Layer work doesn't block on hardware availability.
- Get one **real** adapter working against real hardware — the simplest available robot/SDK, likely ROS2-based — not just the simulator.
- Stand up the orchestration↔Integration Layer interface, with a stub orchestration client if the real one isn't ready, so the contract gets exercised end-to-end from day one rather than validated in isolation.
- Ship the walking skeleton: visual inspection, assign → execute → status → completion → audit log, on one real robot.
- Team: myself plus, if the first OEM SDK is unfamiliar, a short-term contractor engagement to accelerate that first integration — the adapter itself stays owned in-house.

### Day 60 — Breadth

- Onboard the remaining robots using the now-proven adapter pattern. This should mostly be "write adapter #2, #3, #4," not new architecture — if it isn't, that's the signal to stop and fix the contract before scaling further.
- Add gas detection and thermal inspection (reuse the sensor-payload pattern from visual inspection).
- Build out the exception/failure taxonomy: connectivity loss, precondition failure, safety e-stop, low-battery abort, operator-intervention-requested — each normalized and surfaced through the same schema, regardless of which OEM's specific error triggered it.
- Build operator intervention support: pause/resume/teleop-handoff, recorded into the standard audit schema regardless of which tool (in-house or partner) initiated the handoff.
- Begin material-handling capability design — grasp/pose parameters, precondition checks, likely bench-validated before any physical field attempt.
- Scope welding as a capability definition and safety review only — no automation work yet.
- Start capturing adapter-level reliability metrics (command latency, action success rate, telemetry staleness) — feeds both day-90 metrics and the reliability approach in section 5.

### Day 90 — Production-ready for the defined scope

- All 9 robots onboarded via adapters that pass the conformance suite.
- Visual inspection, gas detection, and thermal inspection running as real orchestration-driven missions — scheduled, approved, audited — across the robots that support each.
- Material handling running in **supervised, bounded** production on at least one manipulator, for a defined payload envelope — not general pick-and-place.
- Welding remains a scoped design plus safety review, running human-operated/teleoperated with the Integration Layer providing tracking and audit only. This is an intentional deferral, not a shortfall, and I would say so explicitly in any 90-day review rather than let it read as unfinished work.

### What "smallest useful version" means here

Capability contract + adapter framework + conformance suite + mission executor + exception/audit pipeline, plus three working sensor-based capabilities across all nine robots and one manipulation capability in supervised production on a subset. That is enough standardization — proven across genuinely heterogeneous hardware — to demonstrate the vendor-agnostic orchestration thesis, without needing to have solved general-purpose manipulation or welding automation.

### Common interface across robot types

- **Capability descriptor**: `{capability_type, required_payload, preconditions, params_schema, expected_duration_range}`
- **Mission step**: `{capability_type, target, params, timeout, retry_policy}`
- **Status**: `{QUEUED, ASSIGNED, IN_PROGRESS, PAUSED_FOR_INTERVENTION, COMPLETED, FAILED, ABORTED}` plus a structured `failure_reason` from the shared taxonomy
- **Capability constraints**: payload compatibility (weight, sensor type), battery/range, environmental rating (outdoor/wet/hazardous)

### OEM-specific vs. shared platform

**Stays inside the adapter**: auth/session management, SDK/protocol calls, topic/action names, firmware-version-specific quirks, OEM error codes (mapped outward into the shared taxonomy), hardware-instance calibration.

**Stays in the shared platform**: the capability schema, mission executor state machine, conformance suite, audit/event schema, the safety-supervision pattern (individual triggers may be OEM-tuned, but the pattern and its reporting contract don't vary), exception taxonomy, retry/backoff framework.

### Testing before and during deployment

- **Pre-deployment**: mock-robot simulator plus the conformance suite run in CI on every adapter change — contract drift gets caught before hardware time is spent on it.
- **Bench/HIL**: adapter validated against real hardware in a controlled bay before any new mission type goes to the field — real SDK latency and error behavior reliably differ from the simulator.
- **Staged field rollout per capability-robot pair**: shadow mode (real telemetry in, no commands out) → supervised mode (human confirms each step) → autonomous with a human on standby → full autonomous. Staged independently per combination, not as a single fleet-wide switch.
- **Failure injection**: deliberately test connectivity loss, mid-mission low battery, and e-stop triggers in staging, so the exception path and audit trail are validated before anything depends on them in the field.

### People

1–2 robotics/ROS-capable engineers for the adapter framework and C++ nodes; one backend engineer for the orchestration interface, schema, and mission executor (may be the same people early on); OEM field/support engineers on an as-needed basis during hardware bring-up, not as FTEs; a contractor with welding/functional-safety domain expertise to scope (not automate) the welding capability. A dedicated field-engineer role becomes necessary once more than one or two deployments run concurrently — that role owns bring-up and on-call, and explicitly channels field learnings back into the shared schema rather than quietly absorbing one-off fixes.

### Working with sales without one-off custom work

Every customer requirement gets triaged against the capability model first: does this map to an existing capability (possibly with a new parameter), or is it genuinely net-new? If it maps, it's platform work. If it doesn't, it's flagged explicitly as custom and goes through a real prioritization conversation — it does not silently become part of an engineer's backlog. Sales sells against a living capability roadmap ("supported / in progress / not planned") instead of promising bespoke features. Any approved custom work still gets built as a new capability behind the standard contract — the cost of doing it properly is the forcing function that keeps the custom-work conversation honest.

### Turning deployments into reusable capability

Each mission type becomes a `capability_type` in the shared schema with a documented parameter contract and its own conformance tests — the first customer's implementation is the reference implementation, not a one-off. Every deployment has explicit engineering time budgeted to generalize what was learned: if a customer's specific gas sensor reveals a parameter the schema didn't have, that becomes a schema change, not a customer-specific branch.

### Success metrics

**Technical**: adapter conformance pass rate on first field attempt (proxy for contract quality); mission success rate per capability type; mean time to onboard a new robot/OEM (should trend down); command-to-execution latency (P50/P95); telemetry staleness; false-positive exception rate (should be low — a safety system that cries wolf gets ignored).

**Operational**: missions completed in shadow vs. supervised vs. autonomous mode (shows the maturity curve); operator intervention rate per mission (trending down = increasing trust); fraction of customer requests mapped to the existing capability model vs. requiring genuine custom work (validates the platform thesis); audit-log completeness and queryability for every mission (compliance-readiness).

**The one metric I'd actually watch**: could a tenth robot, from a brand-new OEM, be onboarded in meaningfully less time than the first — using only the adapter framework and conformance suite, with zero changes to orchestration core? That's the real test of whether the vendor-agnostic thesis is working, not just documented.

---

## 5. Reliability and Field Deployment

Reliability at this layer is mostly about **degrading predictably and staying observable**, not about individual robot uptime — that's substantially the OEM's problem, and the Integration Layer's job is to not make it worse and to always know the true state.

- **Independent safety path**: the watchdog/e-stop node runs independently of the Python adapter's health — a crashed or hung adapter process must not disable the ability to stop the robot. This is the reason that node is a separate, minimal C++ process rather than a thread inside the adapter.
- **Idempotent, explicitly-reported mission steps**: orchestration core should never have to assume state — every transition is reported, and retried commands are safe to re-issue.
- **Rich liveness contract**: "adapter process died," "robot is alive but slow," and "robot genuinely failed" need to be distinguishable, not collapsed into a single timeout. This is why the status/failure taxonomy needs to be richer than success/fail.
- **Firmware and adapter versioning**: OEM firmware updates are a real, recurring reliability risk. Adapters declare a compatible firmware version range, and the conformance suite gets re-run against real hardware whenever firmware changes — not just when adapter code changes.
- **Audit log as a reliability tool, not just a compliance artifact**: every state transition recorded means field failures are debuggable after the fact from data, not from memory.
- **Chaos testing before the field does it for you**: connectivity flakiness, GPS-denial, sensor dropout — exercised in staging deliberately.
- **Fast rollback**: if a field issue appears, reverting an adapter to the last known-good version needs to be fast and low-risk, not a full redeploy.
- **Network assumptions**: customer-site connectivity should be assumed intermittent by default — mission-status updates buffer and replay rather than requiring a constant connection, and adapters handle timeouts gracefully rather than assuming best-case network.
- **Ownership boundary during incidents**: the audit log plus a clear map of "what the Integration Layer owns vs. what's the robot's own fault" matters most exactly when something is on fire — that boundary needs to be decided in advance, not negotiated during an incident.

---

## 6. Team and Customer Execution

This section is the steady-state version of the people/process discipline introduced in section 4, as the company scales past the first deployment.

**First hires / contractor use**: robotics/ROS platform engineer(s) owning the adapter framework and conformance suite; a backend engineer owning the orchestration interface and mission executor; a field-engineer role that emerges once concurrent deployments exceed what one person can personally shadow, owning bring-up/on-call and explicitly feeding field learnings back into the shared schema. Contractors for domain-specific certification/safety expertise (welding, hazardous environments), and short, bounded engagements to accelerate the *first* adapter of an unfamiliar robot type — never to own the contract or the framework long-term.

**What stays in-house, always**: the capability contract, the adapter framework, the conformance suite, the mission executor, and the audit/event pipeline. This is the platform's IP; no partner or contractor makes architectural decisions here, only implements specific adapters against a contract we own.

**Evaluating customer requirements**: the same triage discipline from section 4 — map to capability model, param addition, or true net-new — formalized as a lightweight technical-fit review attached to any deal involving new robots, missions, or payloads, owned by whoever owns the platform (initially me), so it isn't a purely commercial decision made without engineering input.

**Avoiding one-off custom work**: the capability roadmap sales sells against, plus treating any true custom request as a full new-capability build (not a quick patch), are the two structural levers. Beyond structure, this also requires actually being willing to say no sometimes — that's a judgment call a policy document can't fully substitute for.

**Turning early deployments into reusable capability**: the same "generalize after every deployment" discipline from section 4, formalized at steady state as a recurring engineering ritual — after each new deployment, a short retro on what needed a schema change, what should have generalized but didn't, feeding directly into the next capability-roadmap planning cycle rather than staying anecdotal.

---

## How I used AI on this assignment

I used Claude to think through this assignment interactively rather than to generate the answer wholesale. Concretely:

- I used it to explore the core architectural question — the capability-contract-vs-per-robot-RPC decision in section 1 is the specific answer I landed on, but I got there by having Claude lay out the naive approach's failure mode and propose the capability-model alternative, which I then validated and committed to.
- I was asked several scoping questions back before any content was written: which robot to use as the code example, the Python/C++ language split and where the boundary between them should fall, whether the deliverable should be a doc-only writeup or a doc plus a real (if illustrative) project structure, and how to handle this section. I made the actual calls on all of those — a generic ROS2 robot over a specific OEM SDK (more representative of the "vendor-agnostic" thesis than picking one vendor), a narrow C++ scope limited to genuinely latency-critical nodes rather than a broader rewrite, and a real project structure over doc-only, since "ownership" is easier to demonstrate against actual files than prose alone.
- The 90-day sequencing (ordering mission types by integration complexity rather than business priority, and explicitly scoping welding as supervised-only rather than promising autonomy) reflects my own judgment about what's actually achievable and honest to commit to in that timeframe — I directed that framing, rather than accepting a generic "do everything" plan.
- The code scaffold (adapter interfaces, mock robot, C++ node stubs, conformance test shape) was generated following the architecture I'd already committed to, then reviewed by me for whether it actually represents the contract described in this document.
- One explicit limitation: no ROS2 environment or real OEM hardware was available while writing this, so the ROS2/C++ code is architecturally representative, not built-and-tested against real middleware. In an actual first 30 days, validating that gap — bench-testing the adapter against a real robot — is genuine engineering work, and I'm not claiming it as done here.
- For the proof-of-concept iteration (second OEM adapter, conformance suite, platform-agnosticism tests, demo script, visualization) — unlike the ROS2/C++ code above, this part is fully runnable, and I verified it by actually running it, not by reading it. That process caught two real bugs (detailed in `README.md`): I had AI explain each failure, checked the root cause against the actual code myself rather than trusting the stack trace, and reviewed each fix before accepting it. The first version of the demo's timeline chart was also visibly wrong — bars overlapping unreadably — which I only caught by looking at the generated image and having it reworked.
