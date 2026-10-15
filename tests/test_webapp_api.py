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


def test_fleet_endpoint_includes_health_fields(client):
    res = client.get("/api/fleet")
    fleet = res.get_json()

    assert len(fleet) == 2
    for robot in fleet:
        assert "power_pct" in robot
        assert "network_condition" in robot
        assert "failed_sensors" in robot
        assert "extra_latency_polls" in robot
        assert robot["network_condition"] == "nominal"
        assert robot["failed_sensors"] == []


def test_run_multistep_mission_via_steps_field(client):
    res = client.post(
        "/api/missions",
        json={"steps": [{"capability": "navigate", "target": "wp1"}, {"capability": "inspect_visual", "target": "wp1"}]},
    )
    data = res.get_json()

    assert res.status_code == 200
    assert data["step_count"] == 2
    assert len(data["history"]) >= 2


def test_set_robot_conditions_offline_then_mission_fails(client):
    res = client.post("/api/robots/spot-01/conditions", json={"offline": True})
    assert res.status_code == 200

    res = client.post("/api/missions", json={"capability": "inspect_visual"})
    data = res.get_json()

    assert data["failure_reason"] == "connectivity_lost"


def test_set_robot_conditions_unknown_robot_returns_404(client):
    res = client.post("/api/robots/does-not-exist/conditions", json={"offline": True})

    assert res.status_code == 404
    assert "error" in res.get_json()


def test_existing_single_capability_mission_response_now_also_has_history(client):
    res = client.post("/api/missions", json={"capability": "detect_gas"})
    data = res.get_json()

    assert res.status_code == 200
    assert len(data["history"]) >= 1
    assert data["history"][-1]["status"] == "completed"


def test_live_mission_endpoint_starts_and_streams_to_terminal(client):
    res = client.post("/api/missions/live", json={"steps": [{"capability": "inspect_visual", "target": "wp1"}]})
    assert res.status_code == 200
    mission_id = res.get_json()["mission_id"]

    stream_res = client.get(f"/api/missions/{mission_id}/stream")
    assert stream_res.status_code == 200
    body = stream_res.get_data(as_text=True)

    assert '"status": "completed"' in body
    assert body.rstrip().endswith("event: done\ndata: {}")


def test_robot_missions_endpoint_returns_mission_log(client):
    mission_res = client.post("/api/missions", json={"capability": "inspect_visual"})
    mission_id = mission_res.get_json()["mission_id"]

    res = client.get("/api/robots/spot-01/missions")
    assert res.status_code == 200
    missions = res.get_json()

    matching = [m for m in missions if m["mission_id"] == mission_id]
    assert len(matching) == 1
    assert matching[0]["final_status"] == "completed"


def test_robot_missions_endpoint_unknown_robot_returns_404(client):
    res = client.get("/api/robots/does-not-exist/missions")

    assert res.status_code == 404
    assert "error" in res.get_json()


def test_fleet_stats_and_robot_stats_endpoints(client):
    client.post("/api/missions", json={"capability": "inspect_visual"})

    fleet_stats = client.get("/api/stats/fleet").get_json()
    robot_stats = client.get("/api/stats/robots/spot-01").get_json()

    assert fleet_stats["total_missions"] > 0
    assert robot_stats["missions_run"] > 0
    assert robot_stats["total_steps_executed"] > 0


def test_stats_query_endpoint_returns_template_answer_by_default(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    res = client.post("/api/stats/query", json={"question": "what's the fleet success rate?"})
    data = res.get_json()

    assert res.status_code == 200
    assert data["engine"] == "template"
    assert data["answer"]


def test_stats_query_endpoint_reports_anthropic_fallback_when_api_call_fails(client, monkeypatch):
    from integration_layer.core.stats_query import AnthropicStatsQueryEngine

    class _AlwaysFailClient:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            raise RuntimeError("simulated API outage")

    failing_engine = AnthropicStatsQueryEngine(api_key="fake", client=_AlwaysFailClient())
    monkeypatch.setattr("webapp.app.get_query_engine", lambda: failing_engine)

    res = client.post("/api/stats/query", json={"question": "what's the fleet success rate?"})
    data = res.get_json()

    assert res.status_code == 200
    assert data["engine"] == "anthropic_fallback"
    assert data["answer"]  # still a real, useful answer -- never a 500


def test_swap_visual_provider_switches_oem_adapter_class(client):
    before = client.get("/api/fleet").get_json()
    before_visual = next(r for r in before if "inspect_visual" in r["capabilities"])
    assert before_visual["robot_id"] == "spot-01"
    assert before_visual["adapter_type"] == "MockRobotAdapter"

    res = client.post("/api/admin/swap_visual_provider")
    data = res.get_json()

    assert res.status_code == 200
    assert data["swapped_from"] == {"robot_id": "spot-01", "adapter_type": "MockRobotAdapter"}
    assert data["swapped_to"] == {"robot_id": "spot-01-cloud", "adapter_type": "CloudFleetAdapter"}

    after = client.get("/api/fleet").get_json()
    after_visual = next(r for r in after if "inspect_visual" in r["capabilities"])
    assert after_visual["robot_id"] == "spot-01-cloud"
    assert after_visual["adapter_type"] == "CloudFleetAdapter"

    # The SAME platform call, zero code changes, now reaches the new adapter.
    mission_res = client.post("/api/missions", json={"capability": "inspect_visual"})
    mission_data = mission_res.get_json()
    assert mission_data["robot_id"] == "spot-01-cloud"
    assert mission_data["adapter_type"] == "CloudFleetAdapter"
    assert mission_data["status"] == "completed"


def test_swap_visual_provider_toggles_back_on_second_call(client):
    client.post("/api/admin/swap_visual_provider")  # Mock -> Cloud

    res = client.post("/api/admin/swap_visual_provider")  # Cloud -> Mock
    data = res.get_json()

    assert data["swapped_from"] == {"robot_id": "spot-01-cloud", "adapter_type": "CloudFleetAdapter"}
    assert data["swapped_to"] == {"robot_id": "spot-01", "adapter_type": "MockRobotAdapter"}


def test_swap_visual_provider_never_touches_fleetx(client):
    client.post("/api/admin/swap_visual_provider")

    fleet = client.get("/api/fleet").get_json()
    fleetx = next(r for r in fleet if r["robot_id"] == "fleetx-01")
    assert fleetx["adapter_type"] == "CloudFleetAdapter"
    assert "detect_gas" in fleetx["capabilities"]


def test_reset_restores_default_visual_provider(client):
    client.post("/api/admin/swap_visual_provider")  # Mock -> Cloud

    client.post("/api/reset")

    fleet = client.get("/api/fleet").get_json()
    visual = next(r for r in fleet if "inspect_visual" in r["capabilities"])
    assert visual["robot_id"] == "spot-01"
    assert visual["adapter_type"] == "MockRobotAdapter"


def test_reset_also_clears_stats(client):
    client.post("/api/missions", json={"capability": "inspect_visual"})
    assert client.get("/api/stats/fleet").get_json()["total_missions"] > 0

    client.post("/api/reset")

    assert client.get("/api/stats/fleet").get_json()["total_missions"] == 0


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
