# Fleet scaling on Kubernetes

Each physical robot in a real deployment runs this repo's Docker image at
the edge. This directory models that: every robot becomes a Kubernetes pod
running `fleet_worker/app.py` (the same image as the GUI, just a different
container command), and a fleet-stats-driven controller scales same-OEM-type
pools of those pods up and down based on real mission load.

## Why pools, not one Deployment per named robot

Robots aren't fungible -- you can't "scale up" by adding a replica of
`spot-01`, because `spot-01` is a specific, unique robot. What IS fungible
is a pool of robots of the *same OEM type* (e.g. three identical
MockRobotAdapter units all capable of visual inspection). So scaling here
means replica count on a **per-OEM-type pool** Deployment
(`mockrobot-pool`, `cloudfleet-pool`), not on an individual robot's
identity. Two different robot *types* stay two independently-scalable
pools, which is the actual heterogeneity story.

## Architecture

```
┌─────────────────┐       discovers pods via          ┌──────────────────┐
│  fleet-manager   │ ◄──── the Kubernetes Pods API ────│  mockrobot-pool   │
│  (webapp/app.py, │       (k8s/fleet_discovery.py)    │  (N pods, each    │
│  FLEET_SOURCE=k8s)│                                   │  fleet_worker/    │
│                  │ ────── HTTP missions ─────────────►│  app.py)          │
└────────┬─────────┘                                   └──────────────────┘
         │ real /api/fleet, /api/stats/robots/<id>              ▲
         │ (same API the GUI reads)                             │ scales replicas
         ▼                                                      │
┌──────────────────┐                                            │
│   fleet-scaler    │ ──── patches Deployment/scale ─────────────┘
│ (k8s/fleet_scaler  │      (Kubernetes apps/v1 API)
│  .py)              │
└──────────────────┘
```

- **Worker pods** (`mockrobot-pool.yaml`, `cloudfleet-pool.yaml`): each
  pod runs `fleet_worker/app.py`, wrapping exactly one `MockRobotAdapter`
  or `CloudFleetAdapter` instance, exposed over HTTP.
- **`RemoteRobotAdapter`** (`integration_layer/core/remote_adapter.py`):
  drives a worker pod over HTTP, implementing the same `RobotAdapter`
  interface every in-process adapter does -- `OrchestrationService` never
  knows or cares that a step's execution crossed a network boundary.
- **fleet-manager** (`fleet-manager.yaml`): the real `webapp/app.py`, with
  `FLEET_SOURCE=k8s` set so it discovers and tracks worker pods instead of
  seeding two fixed local adapters. Every existing GUI feature works
  unmodified against whatever pods currently exist.
- **fleet-scaler** (`fleet-scaler.yaml`, `fleet_scaler.py`): the autonomous
  piece. Polls fleet-manager's own `/api/fleet` and `/api/stats/robots/<id>`
  endpoints -- the exact data the GUI's Fleet Analytics panel already
  shows -- computes per-pool mission throughput, and scales that pool's
  replica count up or down. Deliberately plain, inspectable Python instead
  of a metrics-server-backed HorizontalPodAutoscaler.

## Run it

Requires `kind` (`brew install kind`) and `kubectl`.

```bash
# 1. Build the image once, load it into a local kind cluster.
docker build -t iac-robot-integration:local .
kind create cluster --name iac-fleet
kind load docker-image iac-robot-integration:local --name iac-fleet

# 2. Apply everything.
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/rbac.yaml
kubectl apply -f k8s/mockrobot-pool.yaml
kubectl apply -f k8s/cloudfleet-pool.yaml
kubectl apply -f k8s/fleet-manager.yaml
kubectl apply -f k8s/fleet-scaler.yaml

# 3. Watch it come up.
kubectl -n iac-fleet get pods -w

# 4. Reach the GUI (same dashboard as local/Docker, now backed by pods).
kubectl -n iac-fleet port-forward svc/fleet-manager 5055:5055
# open http://127.0.0.1:5055

# 5. Watch the scaler's decisions.
kubectl -n iac-fleet logs -f deploy/fleet-scaler

# Teardown.
kind delete cluster --name iac-fleet
```

## What's genuinely demonstrated here vs. what isn't

This models the control plane honestly: real pods, a real `RobotAdapter`
driven over a real network boundary, real Kubernetes scaling triggered by
real mission-throughput data. It does **not** model real edge networking
(a real robot fleet would be on unreliable WAN links to a central cluster,
not pods in the same local cluster), real robot hardware behind
`fleet_worker/app.py` (still the same simulated `MockRobotAdapter`/
`CloudFleetAdapter` as everywhere else in this repo), or a production
autoscaling policy (the thresholds in `fleet-scaler.yaml` are tuned to be
observable in a short demo, not load-tested for a real fleet).
