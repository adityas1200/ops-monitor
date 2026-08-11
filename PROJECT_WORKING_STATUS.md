# ops-monitor — Working Status

**As of:** 2026-07-29  
**Branch state:** Local uncommitted work on top of `improvements_20260709`  
**Mode:** Live-only (no mock telemetry). Empty results when Snowflake/AWS are not configured.

This file is the single snapshot of **what the project does today**, **what works**, **what is in progress**, and **what is planned but not built**.

---

## 1. One-line summary

ops-monitor is an agentic data-ops platform that monitors **Snowflake tasks** and **DQ checks** (plus optional AWS), runs **lineage-aware RCA**, proposes **before/after fixes**, and can **validate on a zero-copy clone** — steered from a React UI and chat.

---

## 2. Runtime architecture

```
React UI (Dashboard / Workbench / Settings / Chat)
        │  REST /api/*
        ▼
FastAPI (main.py) → Orchestrator
        │
        ├── ConnectivityAgent
        ├── MonitoringAgent
        ├── RCAAgent
        ├── FixAgent
        ├── TestAgent
        └── ChatAgent
                │
                ▼
        Connectors: Snowflake · AWS · DQ · Lineage · SQL Guardrail
                │
                ▼
            Snowflake (+ optional AWS)
```

| Layer | Entry |
|-------|--------|
| Backend | `uvicorn app.main:app` → port **8001** |
| Frontend | Vite React → port **5173** |
| Orchestration | `backend/app/agents/orchestrator.py` |
| Skills (agent contracts) | `backend/app/skills/*.md` |
| App state | Local JSON under `backend/app/memory/store/` and `backend/app/data/` |

Agents are synchronous Python classes. There is no message bus. LLM (Claude via corporate gateway) is optional; every agent has a deterministic fallback.

---

## 3. Capability status matrix

| Area | Status | Notes |
|------|--------|--------|
| Snowflake connectivity (password + SSO) | **Working** | Settings UI + health probes |
| AWS connectivity | **Partial** | Glue path live; Step Functions / DynamoDB fall back when unavailable |
| Task monitoring (`TASK_HISTORY` / `TASKS`) | **Working** | Needs Snowflake configured; filters by DB + `TASK%` pattern |
| DQ monitoring (summary + revalidate) | **Working** | `DQM_VALIDATION_SUMMARY` + rules table; live re-execution of rule SQL |
| Dashboard KPIs + filters | **Working** | Status / date range; Run RCA → Workbench |
| Workbench RCA → Fix → Validate | **Working** | Full UI flow; Fix now accepts `rca_context` from Workbench |
| Chat intent routing | **Working** | RCA / fix / validate / status / add issue; streaming supported |
| Task RCA (upstream walk + lineage + impact) | **Working** | Deterministic + optional LLM polish |
| DQ RCA (rule SQL, diagnostics, evidence rows) | **Working** | Strong recent focus; failing-row evidence + diagnostic SQL |
| Fix suggest (evidence-seeded + LLM) | **Working (enhanced, uncommitted)** | DQ seeds from RCA evidence; vague TODO guarded when evidence exists |
| Test / zero-copy clone validate | **Working** | Real clone when pre-prod configured; otherwise limited |
| SQL guardrail + audit log | **Working** | Blocks production DML; audits to `sql_audit_log.json` |
| Incident + agent memory | **Working** | File-based JSON store |
| MOCK demo mode | **Removed** | README still mentions it; code is `live_only: true` |
| Evidence-based SQL backtracking RCA | **Not built** | Spec in `conversation.txt`; stubs only (`rca_trace.py` empty) |

---

## 4. What works end-to-end today

### 4.1 Detect (Monitoring)

1. `MonitoringAgent.collect()` / `summary()` pulls Snowflake task runs.
2. `DQConnector.read_results()` + optional `revalidate` executes DQ rule SQL for live pass/fail.
3. Dashboard shows KPIs and failed/delayed rows; DQ has its own summary endpoints.

### 4.2 Analyze (RCA)

**Task path** (`RCAAgent.analyze`):

1. Load pipelines + task graph (`PREDECESSORS`).
2. Walk upstream to earliest failing ancestor.
3. Classify failure (Code / DQ / Dependency / Infrastructure / Data Availability / Unknown).
4. Build upstream/downstream lineage, affected tables, blast radius (views/procs).
5. Correlate related DQ failures; gather structured evidence; optional LLM narrative.
6. Persist incident + memory.

**DQ path** (`RCAAgent.analyze_dq` for `dq_*` ids):

1. Load DQ check + resolve tables.
2. Correlate failed tasks sharing tables.
3. Fetch rule `SQL_CODE`, execute for failing rows, run diagnostic SQL when needed.
4. Evidence pack (facts, grain columns, offender summaries) feeds Fix and UI.
5. Same lineage / impact / LLM / memory pattern as task RCA.

### 4.3 Fix (Identify & Suggest)

`FixAgent.suggest()`:

1. Prefer Workbench-passed `rca_context` (avoids discarding RCA / re-running blindly).
2. Build artifact from RCA (DQ rule SQL or task/procedure SQL when available).
3. Seed deterministic fixes for known DQ patterns when evidence exists.
4. LLM polishes when available; otherwise keep seed / guardrail “insufficient mapping”.
5. Returns before/after, rationale, risk, rollback, validation hints, grounding label.

### 4.4 Validate (Test)

`TestAgent.validate()` creates a clone (when configured), runs validation cases, tears down, updates incident. Marks resolution when tests pass.

### 4.5 Chat

`ChatAgent` keyword-routes intents to the agents above and narrates results (streaming via `/api/chat/stream`).

---

## 5. Frontend surfaces

| Tab / pane | Working behavior |
|------------|------------------|
| **Dashboard** | Task + DQ summaries, filters, open in Workbench / Run RCA |
| **Workbench** | Select failed item → RCA report + lineage → Suggest Fix (passes RCA context) → Validate |
| **Settings** | AWS + Snowflake + monitoring (task DB pattern, DQ tables) |
| **Chat** | Docked assistant; can drive agents and report activity errors |

Key UI pieces: `Dashboard.jsx`, `Workbench.jsx`, `LineageGraph` / `TableLineageGraph` / `LineageTable`, `RichRootCause`, `Settings`, `ChatWindow`.

---

## 6. Key API endpoints

| Method | Path | Purpose | Status |
|--------|------|---------|--------|
| GET | `/api/health` | Health + LLM + platforms | Working |
| GET | `/api/connectivity` | Connection report | Working |
| GET/POST | `/api/settings` | Masked read / save | Working |
| GET | `/api/summary` | Task monitoring KPIs | Working |
| GET | `/api/dq/summary` | DQ KPIs | Working |
| GET | `/api/dq/details/{qc_id}` | DQ detail | Working |
| POST | `/api/rca` | Run RCA | Working |
| GET/POST | `/api/rca/knowledge` | Knowledge rules | Working |
| POST | `/api/fix` | Suggest fix (`rca_context` supported) | Working |
| POST | `/api/validate` | Clone + tests | Working |
| POST | `/api/remediate/{id}` | RCA → Fix → Test chain | Working |
| POST | `/api/chat` / `/api/chat/stream` | Chat | Working |
| GET | `/api/incidents` | Incident log | Working |
| GET/POST | `/api/guardrail/*` | Audit / approve / reject | Working |

---

## 7. Uncommitted local work (current WIP)

Large local diff (~4k lines net) not yet committed. Main themes:

| Area | Change |
|------|--------|
| `rca_agent.py` | Deeper DQ evidence: diagnostics, failing-row preference, grain helpers, richer remediation |
| `dq_connector.py` | Rule table discovery, diagnostic execution, revalidation, interpretation helpers |
| `fix_agent.py` | Evidence-seeded DQ/task fixes, `rca_context`, grounding / vague-after guards |
| `orchestrator.py` / `main.py` / `schemas.py` | Pass `rca_context` through fix API |
| Frontend Workbench / client | Send full RCA into fix; UI polish |
| Skills `rca.md` / `fix.md` / `rca_knowledge.md` | Prompt/contract updates |
| `rca_trace.py` | **Placeholder only (empty file)** |
| `tests/test_rca_agent_backtrack.py` | **Placeholder only (empty file)** |

Design notes for Fix evolution (not all necessarily shipped):  
`fix_suggest_comparison_current_vs_options.md`, `fix_suggest_option1_dq_only.md`, `fix_suggest_option2_full_e2e.md`.

---

## 8. Planned — not implemented: backtracking RCA

Specified in `conversation.txt`. Goal: for duplicate / grain DQ issues, **prove** where bad data first appears by replaying procedure SQL steps with failing business keys (read-only), then recurse into culprit join inputs.

| Component | Intended location | Status |
|-----------|-------------------|--------|
| `FailurePredicate` / `DuplicateFailurePredicate` | new module | Not started |
| `ProducerResolver` | new module | Not started |
| `SqlStepParser` | new module | Not started |
| `SqlReplayService` | new module | Not started |
| `GrainAnalyzer` | new module | Not started |
| `BacktrackingRCAAgent` | new agent | Not started |
| Hook from `RCAAgent` on duplicate/DQ grain | `rca_agent.py` | Not started |
| Unit tests | `test_rca_agent_backtrack.py` | Empty stub |

Until this lands, RCA for duplicates relies on DQ diagnostics + lineage + LLM/templates — **not** step-by-step SQL replay proof.

---

## 9. Known gaps and gotchas

1. **No MOCK mode** — without credentials, monitoring/RCA return empty / not-found. README still describes mock; trust `live_only` in `/api/health`.
2. **Backtracking RCA** — specified, not coded.
3. **`auto_remediate` / some chat paths** may still re-run or under-use RCA context vs Workbench’s `rca_context` path.
4. **TestAgent → FixAgent** can re-trigger RCA internally (extra cost, known inefficiency).
5. **AWS** coverage is uneven vs Snowflake/DQ.
6. **LLM** needs `.env`: `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL` (corporate gateway), `CLAUDE_MODEL`. Without them, templates/seeds still run.
7. **Production writes** are guardrailed; only clone DDL is intended for writes.
8. App state is **local JSON**, not Snowflake.

---

## 10. Where state lives

| Store | Path |
|-------|------|
| Incident log | `backend/app/memory/store/incident_log.json` |
| Per-agent memory | `backend/app/memory/store/<agent>_memory.json` |
| Connections | `backend/app/data/connection_settings.json` |
| SQL audit | `backend/app/data/sql_audit_log.json` |
| RCA knowledge rules | `backend/app/skills/rca_knowledge.md` |
| LLM config | `.env` (project root) |

Default DQ / task monitoring targets (overridable in Settings):

- DQ summary: `CPH_DB_PRE_PROD.MODEL_V2.DQM_VALIDATION_SUMMARY`
- DQ rules: `CPH_DB_PROD.MODEL_V2.CONFIG_LYNKUET`
- Tasks: database `CPH_DB_PROD`, name pattern `TASK%`

---

## 11. How to run

```powershell
# Backend — port 8001
cd c:\Users\EKGAH\Documents\project\ops-monitor\backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8001

# Frontend — port 5173
cd c:\Users\EKGAH\Documents\project\ops-monitor\frontend
npm run dev
```

- UI: http://localhost:5173  
- API docs: http://localhost:8001/docs  
- Health: `GET http://localhost:8001/api/health`  
- Configure Snowflake (and optional AWS / pre-prod) in **Settings**, then use Dashboard → Workbench.

---

## 12. Suggested code reading order

1. `backend/app/agents/orchestrator.py` — API surface  
2. `backend/app/agents/base.py` — skill + Claude harness + memory  
3. `backend/app/agents/monitoring_agent.py` → `rca_agent.py` (`analyze` / `analyze_dq`)  
4. `backend/app/agents/fix_agent.py` → `test_agent.py`  
5. `backend/app/connectors/snowflake_connector.py`, `dq_connector.py`, `lineage_service.py`  
6. `frontend/src/components/Workbench.jsx` — operator happy path  

Deeper conceptual onboarding (Snowflake catalogs, SQL inventory, agent collaboration): see `project_understanding.md`.

---

## 13. Bottom line

| Question | Answer |
|----------|--------|
| Is the core product usable? | **Yes** — with live Snowflake (and DQ tables configured) |
| Strongest area today? | **DQ + task RCA with evidence**, Workbench lineage, chat |
| Strongest recent improvement? | **Evidence-seeded Fix** using Workbench RCA context |
| Biggest unfinished item? | **Generic backtracking / SQL-replay RCA** for duplicates |
| Demo without credentials? | **No** — mock mode removed; configure Settings first |
