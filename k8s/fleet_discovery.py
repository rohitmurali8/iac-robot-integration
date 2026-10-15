"""Keeps an AdapterRegistry in sync with the robot-worker pods actually
running in a Kubernetes namespace. Polls the Pods API directly (not a
Service/Endpoints VIP) so each individual pod's own IP becomes its own
RemoteRobotAdapter entry -- the same way a real fleet of distinct
physical robots would each get their own registry entry, rather than
being hidden behind a single load-balanced address.

Runs as a background thread inside webapp/app.py when FLEET_SOURCE=k8s
(see that file's _start_k8s_discovery()). The `kubernetes` client is
imported lazily, inside run_forever(), not at this module's top level
-- so every other run mode of webapp/app.py never requires the package
to be installed, the same lazy-import discipline already used for
`anthropic` in stats_query.py.
"""

from __future__ import annotations

import logging
import time

from integration_layer.core.registry import AdapterRegistry
from integration_layer.core.remote_adapter import RemoteRobotAdapter

logger = logging.getLogger(__name__)

WORKER_LABEL_SELECTOR = "iac/role=fleet-worker"
WORKER_PORT = 6000


def sync_once(registry: AdapterRegistry, namespace: str, core_v1) -> None:
    pods = core_v1.list_namespaced_pod(namespace, label_selector=WORKER_LABEL_SELECTOR)

    seen_robot_ids: set[str] = set()
    for pod in pods.items:
        if pod.status.phase != "Running" or not pod.status.pod_ip:
            continue
        container_statuses = pod.status.container_statuses or []
        ready = any(c.name == "worker" and c.ready for c in container_statuses)
        if not ready:
            continue

        robot_id = pod.metadata.name
        seen_robot_ids.add(robot_id)
        if registry.get(robot_id) is not None:
            continue  # already registered under this exact pod identity

        base_url = f"http://{pod.status.pod_ip}:{WORKER_PORT}"
        registry.register(RemoteRobotAdapter(robot_id=robot_id, base_url=base_url))
        logger.info("fleet_discovery: registered %s at %s", robot_id, base_url)

    # Deregister pods that no longer exist (scaled down, rescheduled) --
    # ONLY entries this discovery loop itself registered (identified by
    # type), so it never touches a non-K8s adapter some other code path
    # might have registered.
    for robot_id in list(registry.all_robot_ids()):
        adapter = registry.get(robot_id)
        if isinstance(adapter, RemoteRobotAdapter) and robot_id not in seen_robot_ids:
            registry.unregister(robot_id)
            logger.info("fleet_discovery: unregistered %s (pod gone)", robot_id)


def run_forever(registry: AdapterRegistry, namespace: str, interval_s: float = 5.0) -> None:
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()  # local dev: use the current kubectl context (e.g. a kind cluster)

    core_v1 = client.CoreV1Api()
    while True:
        try:
            sync_once(registry, namespace, core_v1)
        except Exception:
            logger.exception("fleet_discovery: sync failed, will retry")
        time.sleep(interval_s)
