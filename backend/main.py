from __future__ import annotations

import json
import os
import random
import sqlite3
import statistics
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="Overwatch AI Monitoring", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = Path(__file__).parent / "overwatch.db"
ENV_PATH = Path(__file__).parent / ".env"
OBSERVED_SERVICE_URL = "http://127.0.0.1:8001"

SCENARIOS = ["db_down", "third_party_fail", "memory_leak", "queue_lag"]

failures = {name: False for name in SCENARIOS}
telemetry: list[dict[str, Any]] = []
incidents: dict[str, dict[str, Any]] = {}
audit_log: list[dict[str, Any]] = []
observed_seen_logs: set[str] = set()
observed_seen_alerts: set[str] = set()

local_observed_state = {
    "cpu_pct": 42.0,
    "memory_mb": 520.0,
    "p95_latency_ms": 180.0,
    "error_rate": 0.01,
    "queue_lag": 3,
    "requests_processed": 0,
    "errors": 0,
    "logs": [],
    "alerts": [],
}
local_memory_leak_start: float | None = None

state = {
    "requests": 0,
    "errors": 0,
    "cpu_pct": 24.0,
    "memory_mb": 300.0,
    "queue_lag": 0,
    "restart_count": 0,
    "recent_deploy": True,
}

historical_success = {
    "db_down": 0.91,
    "third_party_fail": 0.79,
    "memory_leak": 0.86,
    "queue_lag": 0.81,
}


class InjectFailureRequest(BaseModel):
    scenario: str = Field(description="Failure scenario")
    enabled: bool = Field(description="Enable or disable scenario")


class IngestSignalRequest(BaseModel):
    service: str = "order-api"
    cpu_pct: float
    memory_mb: float
    logs: list[str] = Field(default_factory=list)
    alerts: list[str] = Field(default_factory=list)


class OrderRequest(BaseModel):
    customer_id: str
    amount: float


class ApprovalRequest(BaseModel):
    approved_by: str = "operator"


class DenialRequest(BaseModel):
    denied_by: str = "operator"
    reason: str = "Not safe to execute"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_dotenv_file() -> None:
    """Load key/value pairs from backend/.env into process environment."""
    if not ENV_PATH.exists():
        return

    for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ[key] = value


def _append_local_observed_log(level: str, message: str) -> None:
    local_observed_state["logs"].append({"timestamp": now_iso(), "level": level, "message": message})
    if len(local_observed_state["logs"]) > 180:
        local_observed_state["logs"] = local_observed_state["logs"][-180:]


def _append_local_observed_alert(level: str, message: str) -> None:
    local_observed_state["alerts"].append({"timestamp": now_iso(), "level": level, "message": message})
    if len(local_observed_state["alerts"]) > 180:
        local_observed_state["alerts"] = local_observed_state["alerts"][-180:]


def set_local_observed_failure(scenario: str, enabled: bool) -> None:
    global local_memory_leak_start
    if scenario not in failures:
        return

    if scenario == "memory_leak":
        local_memory_leak_start = time.time() if enabled else None

    if enabled:
        if scenario == "db_down":
            _append_local_observed_log("ERROR", "dial tcp 10.0.3.14:5432: connect: connection refused")
            _append_local_observed_alert("critical", "db_timeout")
        elif scenario == "third_party_fail":
            _append_local_observed_log("ERROR", "provider API returned 503 Service Unavailable")
            _append_local_observed_alert("critical", "provider_503")
        elif scenario == "memory_leak":
            _append_local_observed_log("ERROR", "MEMORY LEAK DETECTED: Heap usage growing unbounded at 25MB/sec")
            _append_local_observed_log("WARN", "GC pause times increasing: 450ms → 890ms → 2100ms")
            _append_local_observed_alert("critical", "sustained_memory_growth")
            _append_local_observed_alert("critical", "gc_pressure_high")
        elif scenario == "queue_lag":
            _append_local_observed_log("WARN", "consumer lag increasing: retry storm detected")
            _append_local_observed_alert("warning", "queue_growth")
    else:
        _append_local_observed_log("INFO", f"failure scenario {scenario} disabled")


def simulate_local_observed_metrics() -> dict[str, Any]:
    cpu = 40 + random.randint(-4, 10)
    memory = 500 + random.randint(-15, 45)
    p95 = 180 + random.randint(-25, 75)
    error_rate = 0.01
    queue_lag = max(0, int(local_observed_state["queue_lag"]) - random.randint(0, 2))

    if failures["db_down"]:
        p95 += random.randint(600, 1200)  # High latency from blocked queries
        error_rate = max(error_rate, 0.65)  # Very high error rate (DB is down, all DB ops fail)
        local_observed_state["errors"] = int(local_observed_state["errors"]) + random.randint(8, 15)
        # Emit database-specific errors consistently
        for _ in range(random.randint(2, 4)):
            _append_local_observed_log(
                "ERROR",
                random.choice(
                    [
                        "ERROR: connection refused to database host postgres.default.svc.cluster.local:5432",
                        "dial tcp 10.0.3.14:5432: connect: connection refused - database is unavailable",
                        "psycopg2.OperationalError: could not connect to server: Connection refused",
                        "SQLSTATE[08001] could not connect to database server",
                        "ERROR: database query pool exhausted - no connections available",
                        "ERROR: database connection timeout after 30s - host unreachable",
                    ]
                ),
            )
        _append_local_observed_alert("critical", "database_connectivity_failed")
        _append_local_observed_alert("critical", "db_operations_failing")

    if failures["third_party_fail"]:
        p95 += random.randint(220, 620)
        error_rate = max(error_rate, 0.12)
        if random.random() < 0.7:
            _append_local_observed_log("ERROR", "provider API returned 503 Service Unavailable")
            _append_local_observed_alert("critical", "provider_503")

    if failures["queue_lag"]:
        queue_lag += random.randint(12, 30)
        cpu += random.randint(3, 10)
        if random.random() < 0.65:
            _append_local_observed_log("WARN", "consumer lag increasing: partition backlog exceeded threshold")
            _append_local_observed_alert("warning", "queue_growth")

    if failures["memory_leak"]:
        if local_memory_leak_start is None:
            set_local_observed_failure("memory_leak", True)
        elapsed = time.time() - (local_memory_leak_start or time.time())
        # Memory grows FAST: 25MB/sec, starting from 540MB
        memory = min(540 + (elapsed * 25), 1400)
        cpu = 60 + random.randint(5, 30) if memory < 800 else min(99, 85 + random.randint(0, 14))
        error_rate = max(error_rate, 0.04 if memory < 800 else (0.15 if memory < 1000 else 0.25))
        
        # Emit progressively worse memory/GC logs
        if memory > 600:
            _append_local_observed_log("WARN", f"Heap usage at {int(memory)}MB ({int(memory/1400*100)}% capacity)")
        if memory > 750:
            _append_local_observed_log("WARN", "GC pause time spiking: 890ms (was 120ms baseline)")
            _append_local_observed_alert("warning", "gc_pause_latency")
        if memory > 900:
            _append_local_observed_log("ERROR", "OutOfMemoryError imminent: heap almost exhausted")
            _append_local_observed_log("ERROR", "GC overhead limit exceeded - GC running 99% of the time")
            _append_local_observed_alert("critical", "oom_risk_critical")
        if memory > 1100:
            _append_local_observed_log("CRITICAL", "Application entering death spiral: GC unable to free memory")
            _append_local_observed_alert("critical", "memory_leak_failure_imminent")

    local_observed_state["cpu_pct"] = round(min(99.0, max(10.0, float(cpu))), 2)
    local_observed_state["memory_mb"] = round(min(1400.0, max(250.0, float(memory))), 2)
    local_observed_state["p95_latency_ms"] = round(min(4500.0, max(60.0, float(p95))), 2)
    local_observed_state["error_rate"] = round(min(1.0, max(0.0, float(error_rate))), 3)
    local_observed_state["queue_lag"] = int(min(1000, max(0, queue_lag)))
    local_observed_state["requests_processed"] = int(local_observed_state["requests_processed"]) + random.randint(2, 6)

    return {
        "cpu_pct": local_observed_state["cpu_pct"],
        "memory_mb": local_observed_state["memory_mb"],
        "p95_latency_ms": local_observed_state["p95_latency_ms"],
        "error_rate": local_observed_state["error_rate"],
        "queue_lag": local_observed_state["queue_lag"],
        "requests_processed": local_observed_state["requests_processed"],
        "errors": local_observed_state["errors"],
        "logs": local_observed_state["logs"][-40:],
        "alerts": local_observed_state["alerts"][-30:],
    }


def fetch_observed_metrics() -> dict[str, Any]:
    """
    Poll the observed service (port 8001) for live metrics.
    Falls back to internal state if service is unavailable.
    """
    try:
        req = urllib.request.Request(f"{OBSERVED_SERVICE_URL}/metrics", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        # Two-port mode fallback: simulate observed service internally
        return simulate_local_observed_metrics()


def forward_observed_failure(scenario: str, enabled: bool) -> bool:
    """Forward failure injection to observed service. Returns True if successful."""
    body = json.dumps({"scenario": scenario, "enabled": enabled}).encode("utf-8")
    req = urllib.request.Request(
        f"{OBSERVED_SERVICE_URL}/inject-failure",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5):
            return True
    except (urllib.error.URLError, TimeoutError):
        return False


def sync_observed_telemetry(observed_metrics: dict[str, Any]) -> None:
    """Ingest observed-service metrics/logs into Overwatch telemetry stream."""
    cpu = float(observed_metrics.get("cpu_pct", state["cpu_pct"]))
    memory = float(observed_metrics.get("memory_mb", state["memory_mb"]))
    p95 = float(observed_metrics.get("p95_latency_ms", 180))
    error_rate = float(observed_metrics.get("error_rate", 0.0))
    queue_lag = int(observed_metrics.get("queue_lag", state["queue_lag"]))

    state["cpu_pct"] = cpu
    state["memory_mb"] = memory
    state["queue_lag"] = queue_lag

    emit(
        "order_processed",
        {
            "service": "order-api",
            "latency_ms": max(30, int(p95 + random.randint(-80, 80))),
            "customer_id": "observed-service",
            "amount": round(random.uniform(20, 180), 2),
            "status": "error" if error_rate >= 0.05 else "ok",
            "error_signature": "observed_error_rate_high" if error_rate >= 0.05 else None,
            "cpu_pct": round(cpu, 2),
            "memory_mb": round(memory, 2),
            "queue_lag": queue_lag,
        },
    )

    for entry in observed_metrics.get("logs", []):
        if isinstance(entry, dict):
            key = f"{entry.get('timestamp', '')}|{entry.get('message', '')}"
            if key in observed_seen_logs:
                continue
            observed_seen_logs.add(key)
            emit(
                "log",
                {
                    "service": "order-api",
                    "message": str(entry.get("message", "")),
                    "level": str(entry.get("level", "error")).lower(),
                },
            )

    for entry in observed_metrics.get("alerts", []):
        if isinstance(entry, dict):
            key = f"{entry.get('timestamp', '')}|{entry.get('message', '')}"
            if key in observed_seen_alerts:
                continue
            observed_seen_alerts.add(key)
            emit(
                "alert",
                {
                    "service": "order-api",
                    "message": str(entry.get("message", "")),
                    "level": str(entry.get("level", "critical")).lower(),
                },
            )


def init_sqlite() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_time TEXT NOT NULL,
                action TEXT NOT NULL,
                details_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS incident_snapshots (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                scenario TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def persist_audit(action: str, details: dict[str, Any], event_time: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO audit_events(event_time, action, details_json) VALUES (?, ?, ?)",
            (event_time, action, json.dumps(details)),
        )
        conn.commit()
    finally:
        conn.close()


def persist_incident_snapshot(incident: dict[str, Any]) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """
            INSERT INTO incident_snapshots(id, created_at, status, scenario, payload_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              status=excluded.status,
              payload_json=excluded.payload_json
            """,
            (
                incident["id"],
                incident["created_at"],
                incident["status"],
                incident["scenario"],
                json.dumps(incident),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def log_audit(action: str, details: dict[str, Any]) -> None:
    event_time = now_iso()
    audit_log.append({"time": event_time, "action": action, "details": details})
    if len(audit_log) > 1000:
        del audit_log[:-1000]
    persist_audit(action, details, event_time)


def emit(event_type: str, data: dict[str, Any]) -> None:
    telemetry.append({"time": now_iso(), "type": event_type, **data})
    if len(telemetry) > 2000:
        del telemetry[:-2000]


def scenario_from_signals(error_rate: float, p95_latency: float) -> tuple[str, set[str]]:
    words: set[str] = set()

    if failures["db_down"]:
        words.update(["db_timeout", "5xx", "latency_high"])
        return "db_down", words
    if failures["memory_leak"]:
        words.update(["memory_growth", "oom_kill", "restart_spike"])
        return "memory_leak", words
    if failures["queue_lag"]:
        words.update(["consumer_lag", "delayed_jobs", "timeout_downstream"])
        return "queue_lag", words
    if failures["third_party_fail"]:
        words.update(["provider_503", "retry_storm", "queue_growth"])
        return "third_party_fail", words
    if p95_latency > 1200 and state["queue_lag"] > 20:
        words.update(["latency_high", "queue_growth", "consumer_lag"])
        return "queue_lag", words
    if p95_latency > 1200:
        words.update(["latency_high", "provider_impact", "retry_storm"])
        return "third_party_fail", words

    if error_rate > 0.06:
        words.update(["5xx", "provider_impact"])
        return "third_party_fail", words

    words.update(["low_signal"])
    return "queue_lag", words


def calculate_confidence(scenario: str, signal_strength: float) -> float:
    historical = historical_success.get(scenario, 0.75)
    deploy_correlation = 0.6
    score = (
        0.55 * signal_strength
        + 0.30 * historical
        + 0.15 * deploy_correlation
    )
    return round(max(0.35, min(0.99, score)), 2)


def severity_from_metrics(error_rate: float, p95_latency: float) -> str:
    if error_rate > 0.15 or p95_latency > 2500:
        return "P1"
    if error_rate > 0.08 or p95_latency > 1500:
        return "P2"
    if error_rate > 0.03 or p95_latency > 1000:
        return "P3"
    return "P4"


def filter_noise(alerts: list[str], logs: list[str]) -> dict[str, Any]:
    deduped_alerts = sorted({a.strip() for a in alerts if a.strip()})
    relevant_alerts = [a for a in deduped_alerts if "info" not in a.lower()]
    deduped_logs = sorted({l.strip() for l in logs if l.strip()})
    relevant_tokens = [
        "outofmemoryerror",
        "oom",
        "gc",
        "memory leak",
        "database",
        "db",
        "sqlstate",
        "psycopg2",
        "connection refused",
        "timeout",
        "provider",
        "503",
        "queue",
        "lag",
        "backlog",
    ]
    critical_logs = [
        line
        for line in deduped_logs
        if any(token in line.lower() for token in relevant_tokens)
    ]
    return {
        "alerts_raw": len(alerts),
        "alerts_filtered": len(relevant_alerts),
        "logs_raw": len(logs),
        "logs_filtered": len(critical_logs),
        "alerts": relevant_alerts,
        "logs": critical_logs,
    }


def call_groq_decision(prompt: str) -> dict[str, Any]:
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="GROQ_API_KEY is not set — LLM is required")
    model = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant").strip() or "llama-3.1-8b-instant"

    url = "https://api.groq.com/openai/v1/chat/completions"
    body = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an SRE AI assistant. Return ONLY a raw JSON object — no markdown, "
                    "no code fences, no explanation. Just the JSON."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "overwatch-ai/1.0",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = ""
        try:
            error_body = exc.read().decode("utf-8", errors="ignore")
        except Exception:
            error_body = ""
        if exc.code == 429:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Groq rate limit exceeded for model '{model}' (HTTP 429). "
                    "Wait 30-60 seconds and retry, or use a different API key/project quota."
                ),
            ) from exc
        detail = f"Groq API call failed for model '{model}': HTTP {exc.code}"
        if error_body:
            detail += f" | {error_body[:400]}"
        raise HTTPException(status_code=503, detail=detail) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=503, detail=f"Groq API call failed for model '{model}': {exc}") from exc

    choices = payload.get("choices", [])
    if not choices:
        raise HTTPException(status_code=503, detail="Groq returned no choices")

    text = str(choices[0].get("message", {}).get("content", "")).strip()
    # Strip markdown code fences if model wraps JSON in ```json ... ```
    if text.startswith("```"):
        text = "\n".join(
            line for line in text.splitlines()
            if not line.strip().startswith("```")
        ).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    raise HTTPException(status_code=503, detail="Groq response was not valid JSON")


def generate_incident_decision(signals: dict[str, Any]) -> dict[str, Any]:
    prompt = (
        "You are an SRE AI assistant. Analyse the following production signals and return STRICT JSON "
        "with exactly these keys: summary, root_cause, suggested_fix, verify, risk, confidence. "
        "suggested_fix must be concrete kubectl or shell commands. "
        "verify must describe how to confirm the fix worked. "
        "risk must list any side effects. "
        "confidence must be a float between 0 and 1. "
        "Prioritise explicit error signatures over generic CPU/memory symptoms. "
        "If database connectivity signatures are present (e.g. connection refused, SQLSTATE, psycopg2), "
        "the root cause MUST be database connectivity/unavailability and fixes MUST target DB/network/connectivity, "
        "not generic scaling. "
        f"Signals: {json.dumps(signals)}"
    )
    llm_response = call_groq_decision(prompt)
    return {
        "engine": "groq",
        "summary": str(llm_response.get("summary", "")),
        "root_cause": str(llm_response.get("root_cause", "")),
        "suggested_fix": str(llm_response.get("suggested_fix", "")),
        "verify": str(llm_response.get("verify", "")),
        "risk": str(llm_response.get("risk", "")),
        "confidence": float(llm_response.get("confidence", 0.80)),
    }


load_dotenv_file()
init_sqlite()


def _check_db() -> dict[str, Any]:
    """Ping the SQLite audit DB — mirrors what a real /health does against Postgres."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=2)
        conn.execute("SELECT 1")
        conn.close()
        return {"status": "ok", "latency_ms": 1}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


@app.get("/health")
def health() -> dict[str, Any]:
    """Multi-component health check — mirrors Kubernetes liveness + readiness probes."""
    db_check = _check_db()
    observed_metrics = fetch_observed_metrics()
    current_memory_mb = round(
        max(
            float(state["memory_mb"]),
            float(local_observed_state.get("memory_mb", state["memory_mb"])),
            float(observed_metrics.get("memory_mb", state["memory_mb"])),
        ),
        1,
    )

    # Simulate what happens when the upstream DB (e.g. Postgres for order-api)
    # is down: the app-level DB connection fails, making the pod unready.
    if failures["db_down"]:
        app_db = {
            "status": "error",
            "error": "dial tcp 10.0.3.14:5432: connect: connection refused",
            "note": "pod would be marked NotReady by kubelet — traffic stopped by load balancer",
        }
    else:
        app_db = {"status": "ok"}

    # Memory pressure check
    if current_memory_mb > 1100:
        memory_check = {
            "status": "error",
            "memory_mb": current_memory_mb,
            "note": "memory leak has pushed heap into critical territory",
        }
    elif failures["memory_leak"] or current_memory_mb > 850:
        memory_check = {
            "status": "warning",
            "memory_mb": current_memory_mb,
            "note": "memory pressure detected" if failures["memory_leak"] else None,
        }
    else:
        memory_check = {"status": "ok", "memory_mb": current_memory_mb}

    overall = "ok"
    if db_check["status"] == "error" or app_db["status"] == "error":
        overall = "unhealthy"
    elif memory_check["status"] == "error":
        overall = "unhealthy"
    elif memory_check["status"] == "warning":
        overall = "degraded"

    return {
        "status": overall,
        "time": now_iso(),
        "components": {
            "overwatch_db": db_check,
            "app_db": app_db,
            "memory": memory_check,
        },
        "active_failures": failures,
    }


@app.post("/orders")
def create_order(payload: OrderRequest) -> dict[str, Any]:
    state["requests"] += 1
    base_latency = random.randint(60, 180)

    if failures["memory_leak"]:
        state["memory_mb"] += random.uniform(4, 12)
        state["cpu_pct"] = min(99.0, state["cpu_pct"] + random.uniform(2.5, 7.5))
    else:
        state["memory_mb"] = max(300.0, state["memory_mb"] - random.uniform(0.5, 2.5))
        state["cpu_pct"] = max(18.0, state["cpu_pct"] - random.uniform(1.0, 3.0))

    if failures["queue_lag"]:
        state["queue_lag"] += random.randint(8, 20)
    else:
        state["queue_lag"] = max(0, state["queue_lag"] - random.randint(3, 10))

    is_error = False
    error_signature = None

    if failures["db_down"]:
        is_error = True
        error_signature = "database_connection_refused"
    elif failures["third_party_fail"] and random.random() < 0.7:
        is_error = True
        error_signature = "provider_503"

    emit(
        "order_processed",
        {
            "service": "order-api",
            "latency_ms": base_latency,
            "customer_id": payload.customer_id,
            "amount": payload.amount,
            "status": "error" if is_error else "ok",
            "error_signature": error_signature,
            "cpu_pct": round(state["cpu_pct"], 2),
            "memory_mb": round(state["memory_mb"], 2),
            "queue_lag": state["queue_lag"],
        },
    )

    if state["memory_mb"] > 880 or state["cpu_pct"] > 90:
        emit(
            "log",
            {
                "service": "order-api",
                "message": "OutOfMemoryError: Java heap space; GC overhead limit exceeded",
                "level": "error",
            },
        )
        emit(
            "alert",
            {
                "service": "order-api",
                "level": "critical",
                "message": "cpu_spike_memory_pressure",
            },
        )

    if is_error:
        state["errors"] += 1
        emit(
            "alert",
            {
                "service": "order-api",
                "level": "critical",
                "message": error_signature,
            },
        )
        raise HTTPException(status_code=503, detail=f"order failed: {error_signature}")

    return {
        "order_id": str(uuid.uuid4()),
        "status": "accepted",
        "latency_ms": base_latency,
        "active_failures": failures,
    }


@app.post("/inject-failure")
def inject_failure(payload: InjectFailureRequest) -> dict[str, Any]:
    if payload.scenario not in failures:
        raise HTTPException(status_code=400, detail=f"unknown scenario: {payload.scenario}")

    disabled_others: list[str] = []
    if payload.enabled:
        # Avoid cross-scenario contamination from old logs/alerts when switching failures.
        telemetry.clear()
        local_observed_state["logs"] = []
        local_observed_state["alerts"] = []
        # Keep demos deterministic: only one active injected failure at a time.
        for name, enabled in list(failures.items()):
            if name != payload.scenario and enabled:
                failures[name] = False
                disabled_others.append(name)
                if name == "memory_leak":
                    state["memory_mb"] = 320.0
                    state["cpu_pct"] = 26.0
                if name == "queue_lag":
                    state["queue_lag"] = 3
                forwarded = forward_observed_failure(name, False)
                if not forwarded:
                    set_local_observed_failure(name, False)

    failures[payload.scenario] = payload.enabled
    if payload.scenario == "memory_leak" and not payload.enabled:
        state["memory_mb"] = 320.0
        state["cpu_pct"] = 26.0
    if payload.scenario == "queue_lag" and not payload.enabled:
        state["queue_lag"] = 3

    forwarded_to_observed = forward_observed_failure(payload.scenario, payload.enabled)
    if not forwarded_to_observed:
        set_local_observed_failure(payload.scenario, payload.enabled)
    sync_observed_telemetry(fetch_observed_metrics())

    emit(
        "control",
        {
            "service": "monitoring-demo",
            "message": (
                f"failure {payload.scenario} set to {payload.enabled}; "
                f"observed_sync={'ok' if forwarded_to_observed else 'unavailable'}; "
                f"disabled_others={disabled_others}"
            ),
        },
    )
    log_audit(
        "failure_toggled",
        {
            "scenario": payload.scenario,
            "enabled": payload.enabled,
            "forwarded_to_observed": forwarded_to_observed,
            "disabled_others": disabled_others,
        },
    )
    return {
        "ok": True,
        "failures": failures,
        "forwarded_to_observed": forwarded_to_observed,
        "disabled_others": disabled_others,
    }


@app.post("/monitor/ingest")
def ingest_signals(payload: IngestSignalRequest) -> dict[str, Any]:
    state["cpu_pct"] = payload.cpu_pct
    state["memory_mb"] = payload.memory_mb

    emit(
        "order_processed",
        {
            "service": payload.service,
            "latency_ms": random.randint(120, 450),
            "customer_id": "external-ingest",
            "amount": round(random.uniform(20, 180), 2),
            "status": "ok",
            "error_signature": None,
            "cpu_pct": round(payload.cpu_pct, 2),
            "memory_mb": round(payload.memory_mb, 2),
            "queue_lag": state["queue_lag"],
        },
    )

    for line in payload.logs:
        emit("log", {"service": payload.service, "message": line, "level": "error"})
    for alert in payload.alerts:
        emit("alert", {"service": payload.service, "message": alert, "level": "critical"})

    log_audit(
        "signals_ingested",
        {
            "service": payload.service,
            "cpu_pct": payload.cpu_pct,
            "memory_mb": payload.memory_mb,
            "log_count": len(payload.logs),
            "alert_count": len(payload.alerts),
        },
    )
    return {"ok": True, "ingested": True}


@app.get("/telemetry")
def get_telemetry(limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 500))
    return telemetry[-limit:]


@app.post("/monitor/scan")
def monitor_scan() -> dict[str, Any]:
    # Fetch current metrics from the observed service
    observed_metrics = fetch_observed_metrics()
    sync_observed_telemetry(observed_metrics)

    recent = [e for e in telemetry[-300:] if e["type"] == "order_processed"]
    if not recent:
        return {"message": "not enough telemetry yet"}

    latencies = [e["latency_ms"] for e in recent]
    errors = [e for e in recent if e["status"] == "error"]

    error_rate = len(errors) / max(1, len(recent))
    p95_latency = statistics.quantiles(latencies, n=20)[18] if len(latencies) >= 20 else max(latencies)

    recent_cpu = [float(e.get("cpu_pct", observed_metrics.get("cpu_pct", state["cpu_pct"]))) for e in recent]
    recent_mem = [float(e.get("memory_mb", observed_metrics.get("memory_mb", state["memory_mb"]))) for e in recent]
    
    # Use observed service metrics as the primary source
    cpu_peak = max(recent_cpu) if recent_cpu else observed_metrics.get("cpu_pct", state["cpu_pct"])
    memory_peak = max(recent_mem) if recent_mem else observed_metrics.get("memory_mb", state["memory_mb"])

    recent_logs = [e.get("message", "") for e in telemetry[-200:] if e.get("type") == "log"]
    recent_alerts = [e.get("message", "") for e in telemetry[-200:] if e.get("type") == "alert"]
    noise = filter_noise(recent_alerts, recent_logs)
    log_joined = " ".join(noise["logs"]).lower()
    alert_joined = " ".join(noise["alerts"]).lower()

    cpu_spike = cpu_peak >= 90.0
    memory_high = memory_peak >= 850.0
    oom_log_seen = any("outofmemoryerror" in line.lower() for line in noise["logs"])
    db_signal_count = sum(
        1
        for line in noise["logs"]
        if any(token in line.lower() for token in ["database", "sqlstate", "psycopg2", "connection refused", "5432"])
    ) + sum(
        1
        for alert in noise["alerts"]
        if any(token in alert.lower() for token in ["db_", "database", "connectivity"])
    )
    memory_signal_count = sum(
        1
        for line in noise["logs"]
        if any(token in line.lower() for token in ["outofmemoryerror", "gc", "memory leak", "heap"])
    ) + sum(
        1
        for alert in noise["alerts"]
        if any(token in alert.lower() for token in ["memory", "oom", "gc"])
    )

    active_failure_names = [name for name, enabled in failures.items() if enabled]
    memory_signature_present = (
        memory_signal_count > 0
        or "outofmemoryerror" in log_joined
        or "gc" in log_joined
        or "memory leak" in log_joined
    )

    if len(active_failure_names) == 1:
        # Single active injection should drive incident scenario deterministically.
        scenario = active_failure_names[0]
    elif cpu_spike and memory_high and oom_log_seen:
        scenario = "memory_leak"
    elif memory_signature_present and (memory_peak >= 600 or cpu_peak >= 75):
        scenario = "memory_leak"
    else:
        scenario, _ = scenario_from_signals(error_rate, p95_latency)

    signal_strength = min(1.0, (error_rate * 3.5) + (p95_latency / 3000.0))
    confidence = calculate_confidence(scenario, signal_strength)
    if len(active_failure_names) == 1 and scenario == active_failure_names[0]:
        confidence = round(max(confidence, 0.88), 2)
    if scenario == "memory_leak" and cpu_spike and memory_high and oom_log_seen:
        confidence = round(max(confidence, 0.88), 2)

    signals = {
        "error_rate": round(error_rate, 3),
        "p95_latency": round(p95_latency, 2),
        "cpu_peak": round(cpu_peak, 2),
        "memory_peak": round(memory_peak, 2),
        "oom_log_seen": oom_log_seen,
        "db_signal_count": db_signal_count,
        "memory_signal_count": memory_signal_count,
        "log_signal_samples": noise["logs"][-12:],
        "alert_signal_samples": noise["alerts"][-12:],
        "noise_filter": {
            "alerts_raw": noise["alerts_raw"],
            "alerts_filtered": noise["alerts_filtered"],
            "logs_raw": noise["logs_raw"],
            "logs_filtered": noise["logs_filtered"],
        },
        "derived_hints": {
            "db_signatures_present": (
                db_signal_count > 0
                or "connection refused" in log_joined
                or "sqlstate" in log_joined
                or "psycopg2" in log_joined
                or "database_connectivity_failed" in alert_joined
            ),
            "memory_signatures_present": (
                memory_signal_count > 0
                or "outofmemoryerror" in log_joined
                or "gc" in log_joined
                or "memory leak" in log_joined
            ),
        },
    }
    severity = severity_from_metrics(error_rate, p95_latency)

    ten_mins_ago = datetime.now(timezone.utc) - timedelta(minutes=10)
    for inc in incidents.values():
        created = datetime.fromisoformat(inc["created_at"])
        if inc["scenario"] == scenario and created > ten_mins_ago and inc["status"] in {"open", "approved"}:
            ai_decision = generate_incident_decision(signals)
            inc["latest_scan"] = now_iso()
            inc["metrics"] = {
                "error_rate": round(error_rate, 3),
                "p95_latency": round(p95_latency, 2),
                "cpu_peak": round(cpu_peak, 2),
                "memory_peak": round(memory_peak, 2),
                "sample_size": len(recent),
            }
            inc["summary"] = ai_decision["summary"]
            inc["root_cause"] = ai_decision["root_cause"]
            inc["proposed_fix"] = {
                "action": ai_decision["suggested_fix"],
                "verify": ai_decision["verify"],
                "risk": ai_decision["risk"],
            }
            inc["ai"] = {
                "engine": ai_decision["engine"],
                "confidence": ai_decision["confidence"],
            }
            inc["signals"] = signals
            persist_incident_snapshot(inc)
            log_audit("incident_deduplicated", {"incident_id": inc["id"], "scenario": scenario})
            return {"incident": inc, "deduplicated": True}

    ai_decision = generate_incident_decision(signals)

    incident_id = str(uuid.uuid4())
    incident = {
        "id": incident_id,
        "created_at": now_iso(),
        "latest_scan": now_iso(),
        "service": "order-api",
        "scenario": scenario,
        "severity": severity,
        "confidence": confidence,
        "status": "open",
        "summary": ai_decision["summary"],
        "root_cause": ai_decision["root_cause"],
        "metrics": {
            "error_rate": round(error_rate, 3),
            "p95_latency": round(p95_latency, 2),
            "cpu_peak": round(cpu_peak, 2),
            "memory_peak": round(memory_peak, 2),
            "sample_size": len(recent),
        },
        "signals": signals,
        "proposed_fix": {
            "action": ai_decision["suggested_fix"],
            "verify": ai_decision["verify"],
            "risk": ai_decision["risk"],
        },
        "ai_output": {
            "engine": ai_decision["engine"],
            "confidence": round(float(ai_decision["confidence"]), 2),
        },
        "approval": None,
        "execution": None,
    }

    incidents[incident_id] = incident
    persist_incident_snapshot(incident)
    log_audit(
        "incident_created",
        {
            "incident_id": incident_id,
            "scenario": scenario,
            "severity": severity,
            "confidence": confidence,
        },
    )
    return {"incident": incident, "deduplicated": False}


@app.get("/incidents")
def list_incidents() -> list[dict[str, Any]]:
    return sorted(incidents.values(), key=lambda x: x["created_at"], reverse=True)


@app.post("/incidents/{incident_id}/approve")
def approve_incident(incident_id: str, payload: ApprovalRequest) -> dict[str, Any]:
    incident = incidents.get(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="incident not found")
    if incident["status"] not in {"open", "approved"}:
        raise HTTPException(status_code=400, detail="incident is not in approvable state")

    incident["status"] = "approved"
    incident["approval"] = {"approved_by": payload.approved_by, "approved_at": now_iso()}
    persist_incident_snapshot(incident)
    log_audit("incident_approved", {"incident_id": incident_id, "approved_by": payload.approved_by})
    return incident


@app.post("/incidents/{incident_id}/deny")
def deny_incident(incident_id: str, payload: DenialRequest) -> dict[str, Any]:
    incident = incidents.get(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="incident not found")
    if incident["status"] not in {"open", "approved"}:
        raise HTTPException(status_code=400, detail="incident is not in deniable state")

    incident["status"] = "denied"
    incident["approval"] = {
        "denied_by": payload.denied_by,
        "denied_at": now_iso(),
        "reason": payload.reason,
    }
    persist_incident_snapshot(incident)
    log_audit(
        "incident_denied",
        {"incident_id": incident_id, "denied_by": payload.denied_by, "reason": payload.reason},
    )
    return incident


@app.post("/incidents/{incident_id}/execute")
def execute_incident(incident_id: str) -> dict[str, Any]:
    incident = incidents.get(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="incident not found")
    if incident["status"] != "approved":
        raise HTTPException(status_code=400, detail="incident must be approved before execution")

    scenario = incident["scenario"]
    failures[scenario] = False
    forwarded = forward_observed_failure(scenario, False)
    if not forwarded:
        set_local_observed_failure(scenario, False)
    if scenario == "memory_leak":
        state["memory_mb"] = 340.0
        state["cpu_pct"] = 28.0
    if scenario == "queue_lag":
        state["queue_lag"] = max(0, state["queue_lag"] - 40)

    for _ in range(8):
        emit(
            "order_processed",
            {
                "service": "order-api",
                "latency_ms": random.randint(90, 250),
                "customer_id": "recovery-sample",
                "amount": round(random.uniform(20, 180), 2),
                "status": "ok",
                "error_signature": None,
                "cpu_pct": round(max(20.0, state["cpu_pct"] - random.uniform(0.8, 2.2)), 2),
                "memory_mb": round(max(300.0, state["memory_mb"] - random.uniform(2, 5)), 2),
                "queue_lag": max(0, state["queue_lag"] - random.randint(2, 8)),
            },
        )

    incident["status"] = "resolved"
    incident["execution"] = {
        "executed_at": now_iso(),
        "result": "success",
        "note": f"Applied remediation for {scenario} and switched failure off.",
    }

    persist_incident_snapshot(incident)
    log_audit("remediation_executed", {"incident_id": incident_id, "scenario": scenario})
    log_audit("incident_resolved", {"incident_id": incident_id})
    return incident


@app.get("/audit")
def get_audit(limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 500))
    return audit_log[-limit:]


@app.get("/audit/persisted")
def get_persisted_audit(limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 500))
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT event_time, action, details_json FROM audit_events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    result: list[dict[str, Any]] = []
    for event_time, action, details_json in rows[::-1]:
        try:
            details = json.loads(details_json)
        except json.JSONDecodeError:
            details = {"raw": details_json}
        result.append({"time": event_time, "action": action, "details": details})
    return result

