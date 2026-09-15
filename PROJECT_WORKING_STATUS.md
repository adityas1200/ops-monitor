# ops-monitor — Working Status

**As of:** 2026-08-13  
**Branch:** `Develop` @ `dfea219` (“Updated RCA Changes”) + **local uncommitted WIP**  
**Mode:** Live-only (no mock telemetry). Empty results when Snowflake/AWS are not configured.

This file is the single snapshot of **what the project does today**, **what works**, **what was completed recently**, and **what is still left**.

---

## 1. One-line summary

ops-monitor is an agentic data-ops platform that monitors **Snowflake tasks** and **DQ checks** (plus optional AWS), runs **lineage-aware RCA**, proposes **RCA-guided before/after SQL fixes**, and can **validate on a zero-copy clone** — steered from a React UI and chat.

---

## 2. Runtime architecture

```
React UI (Dashboard / Workbench / Knowledge / Settings / Chat)
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
| DQ monitoring (summary + revalidate) | **Working** | Live re-execution; client timeout ≠ data FAIL |
| Dashboard KPIs + filters | **Working** | Status / date range; Run RCA → Workbench |
| Workbench RCA → Fix → Validate | **Working** | Fix receives full `rca_context` (incl. `procedure_chain`) |
| Knowledge Base UI | **Working (uncommitted)** | New tab + add-from-Workbench form |
| Chat intent routing | **Working** | RCA / fix / validate / status / add issue; streaming supported |
| Task RCA (upstream walk + lineage + impact) | **Working** | Deterministic + optional LLM polish |
| DQ RCA (rule SQL, diagnostics, evidence rows) | **Working** | Evidence cite fix; L3/L2 cardinality RC; writer procs without failed task |
| Fix suggest (RCA-guided, SQL-only, grounded) | **Working (enhanced, uncommitted)** | No invented ETL; no hardcoded category templates |
| Test / zero-copy clone validate | **Working** | Real clone when pre-prod configured; otherwise limited |
| SQL guardrail + audit log | **Working** | Blocks production DML; audits to `sql_audit_log.json` |
| Incident + agent memory | **Working** | File-based JSON store |
| MOCK demo mode | **Removed** | README still mentions it; code is `live_only: true` |
| Evidence-based SQL backtracking RCA | **Not built** | Spec in `conversation.txt`; stubs empty (`rca_trace.py`, `test_rca_agent_backtrack.py`) |

---

## 4. Completed changes (since 2026-07-29 status / through 2026-08-13 WIP)

### 4.1 Fix Agent — grounding & RCA-only suggestions *(uncommitted)*

| Item | Status |
|------|--------|
| Stop inventing ETL objects (e.g. `STAGING.RX_CLAIMS_RAW`) | **Done** |
| Prefer real upstream procedure SQL from RCA `procedure_chain` | **Done** |
| Fail closed when source SQL unavailable (“source SQL unavailable”) | **Done** |
| Reject LLM polish that invents objects or contradicts RCA | **Done** |
| Remove hardcoded category templates (dedupe / NULLIF / trend / warehouse) | **Done** |
| Seed only from RCA remediation + root_cause + evidence | **Done** |
| Before/After are **SQL-only** (no Snowpark Python dumps, no RCA narrative footers) | **Done** |
| Extract focused `sc.sql("""...""")` snippets from Snowpark procs | **Done** |
| Duplicate detection tightened (no false match on `GRAIN1_VALUE`) | **Done** |
| Workbench passes `procedure_chain` / `affected_tables` into fix | **Done** |
| Unit tests: `backend/tests/test_fix_agent_grounding.py` | **Done (untracked)** |

### 4.2 RCA Agent — evidence quality *(partially uncommitted)*

| Item | Status |
|------|--------|
| Resolve writer procedures from failing tables even when no task failed | **Done** |
| Evidence cite accepts short counts (`L3_TGT_CNT=5`, column+value) | **Done** |
| Deterministic RC for L3/L2 segment cardinality (e.g. QC 308) | **Done** |
| Promote code_analysis into root_cause when it cites evidence | **Done** |
| Summarize NULL L2 measures alongside L3 facts | **Done** |
| Unit tests: `backend/tests/test_rca_evidence_cite.py` | **Done (untracked)** |

### 4.3 DQ / Snowflake — timeout handling *(in tree)*

| Item | Status |
|------|--------|
| Raise Snowflake `network_timeout` 60s → **600s** (avoid `000604` on long DQ SQL) | **Done** |
| Treat client timeout as inconclusive — **not** confirmed data FAIL | **Done** |
| RCA messaging when live re-exec times out (`000604`) | **Done** |

### 4.4 Lineage *(uncommitted)*

| Item | Status |
|------|--------|
| `resolve_writer_procedures_for_tables()` for Fix when no failed task correlates | **Done** |
| Table lineage graph UI polish (`TableLineageGraph.jsx`) | **Done** |

### 4.5 Knowledge Base UI *(uncommitted / new)*

| Item | Status |
|------|--------|
| New **Knowledge** tab (`KnowledgeBase.jsx`) listing `rca_knowledge.md` rules | **Done** |
| Workbench: Add to Knowledge + View Knowledge Base | **Done** |
| Extra knowledge rules appended in `rca_knowledge.md` | **Done** |

### 4.6 Earlier committed baseline (`dfea219` — Updated RCA Changes)

Already on `Develop`: deeper DQ evidence / diagnostics, `rca_context` through fix API, Workbench polish, Chat FAB, monitoring/DQ connector upgrades, skills updates, `PROJECT_WORKING_STATUS.md` introduced, fix-suggest design notes.

---

## 5. What works end-to-end today

### 5.1 Detect (Monitoring)

1. `MonitoringAgent.collect()` / `summary()` pulls Snowflake task runs.
2. `DQConnector.read_results()` + optional `revalidate` executes DQ rule SQL for live pass/fail.
3. Long DQ rules get a 10-minute client network timeout; timeouts are not scored as data mismatches.
4. Dashboard shows KPIs and failed/delayed rows; DQ has its own summary endpoints.

### 5.2 Analyze (RCA)

**Task path** (`RCAAgent.analyze`): upstream walk, failure classify, lineage/impact, DQ correlate, evidence, optional LLM.

**DQ path** (`RCAAgent.analyze_dq`):

1. Load DQ check + resolve tables (prefer FQN from rule SQL).
2. Correlate failed tasks; if none, still resolve **writer procedures** for Fix.
3. Execute rule SQL for failing rows / diagnostics.
4. Evidence pack feeds Fix and UI; cite-check + deterministic RC for thin cardinality cases.
5. Persist incident + memory.

### 5.3 Fix (Identify & Suggest)

`FixAgent.suggest()`:

1. Prefer Workbench-passed `rca_context` (incl. `procedure_chain`).
2. Build **SQL-only** Before from DQ rule SQL / focused procedure snippets — never invent objects.
3. Seed from RCA guidance only (no hardcoded category templates).
4. LLM may polish within allowlisted objects; invented/contradictory polish is rejected.
5. After is SQL-only (QUALIFY / embedded remediation SQL when present).

### 5.4 Validate / Chat

Unchanged happy path: clone validate when configured; chat routes to agents with streaming.

---

## 6. Frontend surfaces

| Tab / pane | Working behavior |
|------------|------------------|
| **Dashboard** | Task + DQ summaries, filters, open in Workbench / Run RCA |
| **Workbench** | RCA → Suggest Fix (`rca_context`) → Validate; Add/View Knowledge |
| **Knowledge** | Browse RCA knowledge rules *(new, uncommitted)* |
| **Settings** | AWS + Snowflake + monitoring (task DB pattern, DQ tables) |
| **Chat** | Docked assistant; can drive agents and report activity errors |

Key UI pieces: `Dashboard.jsx`, `Workbench.jsx`, `KnowledgeBase.jsx`, `LineageGraph` / `TableLineageGraph` / `LineageTable`, `RichRootCause`, `Settings`, `ChatWindow`.

---

## 7. Key API endpoints

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

## 8. Uncommitted local WIP (current)

Large local diff on top of `dfea219`. Main themes:

| Area | Change |
|------|--------|
| `fix_agent.py` | Grounding, RCA-only seeds, SQL-only before/after, invent-object guards (~1.5k lines changed) |
| `rca_agent.py` | Evidence cite, L3/L2 cardinality RC, writer-proc resolution without failed task |
| `lineage_service.py` | `resolve_writer_procedures_for_tables` |
| `fix.md` / `rca_knowledge.md` | Skill + knowledge rule updates |
| Workbench / App / styles | Knowledge tab wiring, RCA→Fix context, UI polish |
| `TableLineageGraph.jsx` | Graph improvements |
| `KnowledgeBase.jsx` | **New (untracked)** |
| `test_fix_agent_grounding.py` | **New (untracked)** |
| `test_rca_evidence_cite.py` | **New (untracked)** |
| `rca_trace.py` | **Still empty placeholder** |
| `test_rca_agent_backtrack.py` | **Still empty placeholder** |

Design notes (ideas, not all shipped):  
`fix_suggest_comparison_current_vs_options.md`, `fix_suggest_option1_dq_only.md`, `fix_suggest_option2_full_e2e.md`.

---

## 9. Left / not done

| Priority | Item | Notes |
|----------|------|--------|
| **P0** | **Commit & push current WIP** | Fix grounding, RCA cite, Knowledge UI, tests still local-only |
| **P0** | **Evidence-based SQL backtracking RCA** | Spec in `conversation.txt`. Components not started: `FailurePredicate`, `ProducerResolver`, `SqlStepParser`, `SqlReplayService`, `GrainAnalyzer`, `BacktrackingRCAAgent`, hook from `RCAAgent`, real tests |
| **P1** | Chat / `auto_remediate` parity with Workbench `rca_context` | Some paths may still re-run RCA or under-use context |
| **P1** | TestAgent → FixAgent efficiency | Can re-trigger RCA internally (extra cost) |
| **P2** | AWS coverage | Uneven vs Snowflake/DQ |
| **P2** | README cleanup | Still mentions MOCK; product is live-only |
| **P2** | Broader Fix quality on non-dup DQ types | Trend / threshold / join-key cases depend on RCA richness; no category templates anymore |
| **P3** | Persist app state beyond local JSON | Incidents/memory still file-based |

Until backtracking lands, duplicate/grain RCA relies on DQ diagnostics + lineage + LLM/templates — **not** step-by-step SQL replay proof.

---

## 10. Known gaps and gotchas

1. **No MOCK mode** — without credentials, monitoring/RCA return empty / not-found. Trust `live_only` in `/api/health`.
2. **Backtracking RCA** — specified, not coded (`rca_trace.py` empty).
3. **Uncommitted WIP** — restart backend after pull; Knowledge tab only exists in local tree.
4. **LLM** needs `.env`: `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `CLAUDE_MODEL`. Without them, templates/seeds still run.
5. **Production writes** are guardrailed; only clone DDL is intended for writes.
6. Long DQ rules need backend restart so Snowflake sessions pick up `network_timeout=600`.

---

## 11. Where state lives

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

## 12. How to run

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

## 13. Suggested code reading order

1. `backend/app/agents/orchestrator.py` — API surface  
2. `backend/app/agents/base.py` — skill + Claude harness + memory  
3. `backend/app/agents/monitoring_agent.py` → `rca_agent.py` (`analyze` / `analyze_dq`)  
4. `backend/app/agents/fix_agent.py` → `test_agent.py`  
5. `backend/app/connectors/snowflake_connector.py`, `dq_connector.py`, `lineage_service.py`  
6. `frontend/src/components/Workbench.jsx` — operator happy path  
7. `frontend/src/components/KnowledgeBase.jsx` — knowledge tab  

Deeper conceptual onboarding: see `project_understanding.md`.

---

## 14. Bottom line

| Question | Answer |
|----------|--------|
| Is the core product usable? | **Yes** — with live Snowflake (and DQ tables configured) |
| Strongest area today? | **DQ + task RCA with evidence**, Workbench lineage, RCA-guided Fix |
| Strongest recent improvement? | **Grounded Fix** (no invented ETL; SQL-only; RCA-seeded) + **DQ timeout honesty** + **Knowledge UI** |
| Biggest unfinished item? | **Generic backtracking / SQL-replay RCA** for duplicates |
| Immediate hygiene left? | **Commit/push uncommitted WIP** + empty backtrack stubs |
| Demo without credentials? | **No** — mock mode removed; configure Settings first |
