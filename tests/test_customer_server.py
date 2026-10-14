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
