# Overwatch AI - Deployment Guide

Three-tier architecture with port constraints (8000, 8001, 3000):

## Architecture

- **Port 8001**: Observed Service (Flask) — simulates production service with metrics
- **Port 8000**: Overwatch AI (FastAPI) — incident detection, LLM reasoning, HITL approval workflow  
- **Port 3000**: Frontend (HTTP server) — monitoring dashboard UI

## Setup

### 1. Install dependencies
```bash
cd backend
pip install -r requirements.txt
```

### 2. Configure Gemini API (optional)
Create `.env` file in `backend/`:
```
GEMINI_API_KEY=your_key_here
```
If not set, uses deterministic fallback engine.

## Running

**Terminal 1 — Observed Service (port 8001):**
```bash
cd backend
python observed_service.py
```
Endpoints:
- `GET /metrics` — current service metrics
- `POST /trigger-memory-leak` — start memory leak simulation
- `POST /reset` — reset to normal state

**Terminal 2 — Overwatch AI (port 8000):**
```bash
cd backend
python -m uvicorn main:app --reload --port 8000
```

**Terminal 3 — Frontend (port 3000):**
```bash
cd frontend
python -m http.server 3000
```

Open browser: **http://localhost:3000**

## Demo Flow

1. Click **"Inject CPU+Memory+OOM Signals"** button
2. Click **"Run Monitor Scan"** to trigger incident detection
3. AI analyzes signals → generates summary, root cause, confidence
4. **Approve** incident → execute remediation
5. View **SQLite audit trail** confirming actions

## Data

- **Incidents**: In-memory cache + SQLite persistence (`backend/overwatch.db`)
- **Telemetry**: In-memory (last 2000 events)
- **Audit**: In-memory (last 1000 events) + SQLite log

## API Reference

### Overwatch AI

- `POST /inject-failure` — toggle failure mode
- `POST /orders` — simulate order traffic
- `POST /monitor/ingest` — direct signal injection
- `POST /monitor/scan` — analyze and detect incidents
- `GET /incidents` — list detected incidents
- `POST /incidents/{id}/approve` — approve incident
- `POST /incidents/{id}/deny` — reject incident
- `POST /incidents/{id}/execute` — execute remediation
- `GET /audit/persisted` — SQLite audit log

### Observed Service

- `GET /metrics` — service state
- `POST /trigger-memory-leak` — start memory leak
- `POST /reset` — reset state
