"""Basic tests for the GUI's Flask API layer.

Deliberately thin: webapp/app.py is a wrapper, not where the logic lives.
These tests exist to catch wiring mistakes (wrong field name serialized,
reset not actually resetting) -- the actual orchestration behavior is
already covered by test_adapter_contract.py and test_platform_agnosticism.py
against the same underlying classes.
"""

from __future__ import annotations

import pytest

from webapp.app import app as flask_app


@pytest.fixture
def client():
    flask_app.config.update(TESTING=True)
    with flask_app.test_client() as client:
        yield client
    # Reset shared module-level state between tests so test order doesn't matter.
    client.post("/api/reset")


def test_index_serves_html(client):
    res = client.get("/")
    assert res.status_code == 200
    assert b"IAC Robot Integration Layer" in res.data


def test_fleet_endpoint_lists_both_oems(client):
    res = client.get("/api/fleet")
    fleet = res.get_json()

    robot_ids = {r["robot_id"] for r in fleet}
    adapter_types = {r["adapter_type"] for r in fleet}

    assert robot_ids == {"spot-01", "fleetx-01"}
    assert adapter_types == {"MockRobotAdapter", "CloudFleetAdapter"}


def test_run_mission_routes_to_correct_oem(client):
    res = client.post("/api/missions", json={"capability": "detect_gas"})
    data = res.get_json()

    assert res.status_code == 200
    assert data["robot_id"] == "fleetx-01"
    assert data["adapter_type"] == "CloudFleetAdapter"
    assert data["status"] == "completed"


def test_run_mission_unknown_capability_returns_400(client):
    res = client.post("/api/missions", json={"capability": "fly_to_the_moon"})

    assert res.status_code == 400
    assert "error" in res.get_json()


def test_audit_endpoint_reflects_mission_runs(client):
    client.post("/api/missions", json={"capability": "inspect_visual"})

    res = client.get("/api/audit")
    events = res.get_json()

    assert any(e["event_type"] == "mission_assigned" for e in events)
    assert any(e["robot_id"] == "spot-01" for e in events)


def test_reset_clears_audit_log_but_keeps_fleet(client):
    client.post("/api/missions", json={"capability": "inspect_visual"})

    res = client.post("/api/reset")
    data = res.get_json()

    assert len(data["fleet"]) == 2
    audit_after_reset = client.get("/api/audit").get_json()
    assert audit_after_reset == []
