"""Autonomous, fleet-stats-driven scaling controller.

Polls the orchestrator's own real API (the exact same /api/fleet and
/api/stats/robots/<id> endpoints the GUI's Fleet Analytics panel reads)
for every pod currently in a worker pool, computes recent per-pool
mission throughput from MissionAnalytics' real missions_run counters --
no new tracking infrastructure invented for this -- and scales that
pool's Kubernetes Deployment replica count up or down accordingly.

This is the autonomous piece: a decision loop acting on the platform's
own observed load on a polling interval, not a human running `kubectl
scale`. It is deliberately NOT a standard metrics-server-backed
HorizontalPodAutoscaler -- that would hide the scaling decision inside
an opaque Kubernetes-native resource; this keeps it as plain,
inspectable Python reading the same fleet-stats data the GUI shows,
which is the point being demonstrated.

Run (inside the cluster, via k8s/fleet-scaler.yaml):
    python k8s/fleet_scaler.py
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://fleet-manager:5055")
NAMESPACE = os.environ.get("POOL_NAMESPACE", "default")
POLL_INTERVAL_S = float(os.environ.get("SCALER_POLL_INTERVAL_S", "10"))

# Missions completed per pod, per poll interval, that triggers scaling
# up/down. Deliberately low defaults so the behavior is observable on a
# short demo timescale -- a real deployment would tune these against
# actual mission duration statistics.
SCALE_UP_THRESHOLD = float(os.environ.get("SCALE_UP_MISSIONS_PER_POD", "2"))
SCALE_DOWN_THRESHOLD = float(os.environ.get("SCALE_DOWN_MISSIONS_PER_POD", "0.5"))
MIN_REPLICAS = int(os.environ.get("MIN_REPLICAS", "1"))
MAX_REPLICAS = int(os.environ.get("MAX_REPLICAS", "5"))

POOLS = ["mockrobot-pool", "cloudfleet-pool"]


def _get_json(url: str) -> object:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read())


def fetch_fleet() -> list[dict]:
    return _get_json(f"{ORCHESTRATOR_URL}/api/fleet")


def fetch_robot_stats(robot_id: str) -> dict:
    return _get_json(f"{ORCHESTRATOR_URL}/api/stats/robots/{robot_id}")


def pool_of(robot_id: str) -> str | None:
    for pool in POOLS:
        if robot_id.startswith(pool):
            return pool
    return None


def decide_and_scale(apps_v1, pool: str, per_pod_load: float) -> None:
    # The Deployment's own spec.replicas, fetched fresh right now, is
    # the ONLY correct baseline for "what to scale from" -- NOT the
    # count of pods fleet-manager's discovery currently has registered.
    # Those two numbers legitimately disagree during a rollout/scaling
    # transition (a pod can be Running-and-counted by discovery a few
    # seconds before or after it's reflected here), and computing the
    # new target from the discovery count instead of this value was a
    # real bug: it made the controller's own decisions oscillate against
    # a moving, sometimes-stale baseline instead of the cluster's actual
    # state, independent of real load.
    scale = apps_v1.read_namespaced_deployment_scale(pool, NAMESPACE)
    current_replicas = scale.spec.replicas

    if per_pod_load >= SCALE_UP_THRESHOLD and current_replicas < MAX_REPLICAS:
        new_replicas = min(MAX_REPLICAS, current_replicas + 1)
    elif per_pod_load <= SCALE_DOWN_THRESHOLD and current_replicas > MIN_REPLICAS:
        new_replicas = max(MIN_REPLICAS, current_replicas - 1)
    else:
        print(f"[fleet_scaler] {pool}: load={per_pod_load:.2f} missions/pod, "
              f"replicas={current_replicas} -- no change")
        return

    scale.spec.replicas = new_replicas
    apps_v1.patch_namespaced_deployment_scale(pool, NAMESPACE, scale)
    print(f"[fleet_scaler] {pool}: load={per_pod_load:.2f} missions/pod -- "
          f"scaling {current_replicas} -> {new_replicas}")


def poll_once(apps_v1, last_missions_run: dict[str, int]) -> None:
    fleet = fetch_fleet()

    pool_totals: dict[str, int] = {}
    pool_robot_counts: dict[str, int] = {}
    for robot in fleet:
        pool = pool_of(robot["robot_id"])
        if pool is None:
            continue
        stats = fetch_robot_stats(robot["robot_id"])
        pool_totals[pool] = pool_totals.get(pool, 0) + stats["missions_run"]
        pool_robot_counts[pool] = pool_robot_counts.get(pool, 0) + 1

    for pool in POOLS:
        current_total = pool_totals.get(pool, 0)
        delta = current_total - last_missions_run.get(pool, current_total)
        last_missions_run[pool] = current_total

        # pool_robot_counts is used ONLY to average load per pod -- the
        # scaling decision's actual baseline is read fresh inside
        # decide_and_scale, see the comment there for why that split
        # matters.
        observed_pods = pool_robot_counts.get(pool, 0)
        if observed_pods == 0:
            print(f"[fleet_scaler] {pool}: no running pods observed, skipping this interval")
            continue

        decide_and_scale(apps_v1, pool, delta / observed_pods)


def main() -> None:
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    apps_v1 = client.AppsV1Api()
    last_missions_run: dict[str, int] = {}

    print(f"fleet_scaler starting, polling {ORCHESTRATOR_URL} every {POLL_INTERVAL_S}s "
          f"(scale up >= {SCALE_UP_THRESHOLD} missions/pod, scale down <= {SCALE_DOWN_THRESHOLD})")
    while True:
        try:
            poll_once(apps_v1, last_missions_run)
        except Exception as exc:
            # Broad on purpose: this is a long-running controller loop
            # that must survive a single bad poll (orchestrator
            # temporarily unreachable, a Kubernetes API error, a pool
            # Deployment not existing yet) and try again next interval,
            # not crash the whole controller pod over one transient
            # failure.
            print(f"[fleet_scaler] poll failed: {exc}")
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
