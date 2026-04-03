"""Observed service simulator on port 8001 with realistic failure logs."""

from __future__ import annotations

import random
import time
from datetime import datetime

from flask import Flask, jsonify, request

app = Flask(__name__)

SCENARIOS = ["db_down", "high_latency", "third_party_fail", "memory_leak", "queue_lag"]
failures = {name: False for name in SCENARIOS}

service_state = {
    "cpu_pct": 42.0,
    "memory_mb": 520.0,
    "p95_latency_ms": 180.0,
    "error_rate": 0.01,
    "queue_lag": 3,
    "requests_processed": 0,
    "errors": 0,
    "last_update": datetime.now().isoformat(),
    "logs": [],
    "alerts": [],
}

memory_leak_start_time: float | None = None


def now_iso() -> str:
    return datetime.now().isoformat()


def append_log(level: str, message: str) -> None:
    service_state["logs"].append({"timestamp": now_iso(), "level": level, "message": message})
    if len(service_state["logs"]) > 200:
        service_state["logs"] = service_state["logs"][-200:]


def append_alert(level: str, message: str) -> None:
    service_state["alerts"].append({"timestamp": now_iso(), "level": level, "message": message})
    if len(service_state["alerts"]) > 120:
        service_state["alerts"] = service_state["alerts"][-120:]


def simulate_tick() -> None:
    global memory_leak_start_time
    service_state["last_update"] = now_iso()
    service_state["requests_processed"] += random.randint(2, 6)

    cpu = 40 + random.randint(-4, 10)
    memory = 500 + random.randint(-15, 45)
    latency = 170 + random.randint(-30, 80)
    error_rate = 0.01
    queue_lag = max(0, service_state["queue_lag"] - random.randint(0, 2))

    if failures["high_latency"]:
        latency += random.randint(1200, 2300)
        cpu += random.randint(10, 22)
        error_rate = max(error_rate, 0.07)
        if random.random() < 0.6:
            append_log("WARN", "upstream timeout: gateway exceeded 2s deadline")
            append_alert("critical", "latency_high: p95 above SLO")

    if failures["db_down"]:
        latency += random.randint(450, 900)
        error_rate = max(error_rate, 0.28)
        service_state["errors"] += random.randint(1, 4)
        if random.random() < 0.8:
            msg = random.choice(
                [
                    "ERROR: connection refused",
                    "dial tcp 10.0.3.14:5432: connect: connection refused",
                    "psycopg2.OperationalError: could not connect to server",
                    "SQLSTATE[08001] connection failure",
                ]
            )
            append_log("ERROR", msg)
            append_alert("critical", "db_timeout")

    if failures["third_party_fail"]:
        latency += random.randint(200, 600)
        error_rate = max(error_rate, 0.12)
        if random.random() < 0.75:
            append_log("ERROR", "provider API returned 503 Service Unavailable")
            append_alert("critical", "provider_503")

    if failures["queue_lag"]:
        queue_lag += random.randint(12, 30)
        cpu += random.randint(3, 9)
        if random.random() < 0.7:
            append_log("WARN", "consumer lag increasing: partition backlog exceeded threshold")
            append_alert("warning", "queue_growth")

    if failures["memory_leak"]:
        if memory_leak_start_time is None:
            memory_leak_start_time = time.time()
        elapsed = time.time() - memory_leak_start_time
        memory = min(520 + (elapsed * 11), 1300)
        cpu = 55 if memory < 820 else min(99, 90 + random.randint(0, 9))
        error_rate = max(error_rate, 0.08 if memory > 860 else error_rate)
        if memory > 880:
            append_log("ERROR", "OutOfMemoryError: Java heap space")
            append_log("ERROR", "GC overhead limit exceeded")
            append_alert("critical", "cpu_spike_memory_pressure")

    service_state["cpu_pct"] = round(min(99.0, max(10.0, float(cpu))), 2)
    service_state["memory_mb"] = round(min(1400.0, max(250.0, float(memory))), 2)
    service_state["p95_latency_ms"] = round(min(4500.0, max(60.0, float(latency))), 2)
    service_state["error_rate"] = round(min(1.0, max(0.0, float(error_rate))), 3)
    service_state["queue_lag"] = int(min(1000, max(0, queue_lag)))


@app.route("/metrics", methods=["GET"])
def get_metrics():
    simulate_tick()
    payload = dict(service_state)
    payload["active_failures"] = failures
    payload["logs"] = service_state["logs"][-40:]
    payload["alerts"] = service_state["alerts"][-30:]
    return jsonify(payload)


@app.route("/inject-failure", methods=["POST"])
def inject_failure():
    global memory_leak_start_time
    payload = request.get_json(silent=True) or {}
    scenario = str(payload.get("scenario", "")).strip()
    enabled = bool(payload.get("enabled", False))
    if scenario not in failures:
        return jsonify({"ok": False, "error": f"unknown scenario: {scenario}"}), 400

    failures[scenario] = enabled
    if scenario == "memory_leak":
        memory_leak_start_time = time.time() if enabled else None

    if enabled:
        if scenario == "db_down":
            append_log("ERROR", "dial tcp 10.0.3.14:5432: connect: connection refused")
            append_alert("critical", "db_timeout")
        elif scenario == "high_latency":
            append_log("WARN", "request timeout near edge gateway; p95 breach")
            append_alert("critical", "latency_high")
        elif scenario == "third_party_fail":
            append_log("ERROR", "provider API returned 503 Service Unavailable")
            append_alert("critical", "provider_503")
        elif scenario == "memory_leak":
            append_log("WARN", "memory leak suspected: unbounded cache accumulation")
            append_alert("critical", "sustained_memory_growth")
        elif scenario == "queue_lag":
            append_log("WARN", "consumer lag increasing: retry storm detected")
            append_alert("warning", "queue_growth")
    else:
        append_log("INFO", f"failure scenario {scenario} disabled")

    return jsonify({"ok": True, "scenario": scenario, "enabled": enabled, "active_failures": failures})


@app.route("/reset", methods=["POST"])
def reset():
    global memory_leak_start_time
    for key in failures:
        failures[key] = False
    memory_leak_start_time = None
    service_state["cpu_pct"] = 42.0
    service_state["memory_mb"] = 520.0
    service_state["p95_latency_ms"] = 180.0
    service_state["error_rate"] = 0.01
    service_state["queue_lag"] = 3
    service_state["requests_processed"] = 0
    service_state["errors"] = 0
    service_state["logs"] = []
    service_state["alerts"] = []
    return jsonify({"ok": True, "status": "service reset"})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy", "active_failures": failures}), 200


if __name__ == "__main__":
    print("Observed Service running on http://localhost:8001")
    print("Endpoints:")
    print("  GET  /metrics")
    print("  POST /inject-failure  {scenario, enabled}")
    print("  POST /reset")
    print("  GET  /health")
    app.run(host="127.0.0.1", port=8001, debug=False)
