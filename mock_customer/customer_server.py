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
