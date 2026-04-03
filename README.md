# Autonomous Monitoring Hackathon Starter

This project gives you a complete starting system from zero:
- A small production-like API (`backend/main.py`)
- Failure injection to simulate real incidents
- AI-like triage with confidence score
- Runbook matching
- Human approval flow (HITL)
- Execution simulation and full audit log
- Lightweight dashboard (`frontend/`)

## 1) Quick Start

### Backend
1. Open terminal in project root.
2. Create and activate virtual environment.
3. Install dependencies.
4. Run API.

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

### Frontend
Open a second terminal:

```powershell
cd frontend
python -m http.server 5500
```

Open: `http://localhost:5500`

## 2) Demo Flow
1. Click `Inject db_down ON`.
2. Generate traffic using `Create Sample Order` a few times.
3. Click `Run Monitor Scan`.
4. Open incident card and review proposed fix with confidence.
5. Click `Approve`, then `Execute`.
6. Failure is toggled off and audit logs capture every step.

## 3) Team Split (3 Members)
- Member A: expand API + telemetry realism.
- Member B: improve triage confidence and runbook retrieval.
- Member C: improve dashboard UX and pitch storyline.

## 4) Next Improvements
- Replace in-memory storage with PostgreSQL.
- Add vector database retrieval for runbooks.
- Integrate Prometheus/Grafana metrics.
- Add authentication and role-based approvals.
