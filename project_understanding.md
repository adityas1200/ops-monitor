# ops-monitor — Project Understanding & Onboarding Guide

A guide for developers new to this codebase. It explains **what problem the project
solves**, how an incident flows end-to-end, what data we pull from Snowflake, the SQL we
run, the data-quality checks involved, how Root Cause Analysis (RCA) is produced, how the
agents collaborate, and the key Snowflake concepts you need to know.

> This is a conceptual guide, not a line-by-line code walkthrough. File paths are given so
> you can dive in where needed.

---

## 1. The business problem

Data teams run large numbers of **Snowflake tasks** and **AWS jobs** (Glue, Step Functions,
etc.) that move and transform data on a schedule. When one of these pipelines **fails or runs
late**, the on-call engineer has to manually:

1. notice the failure,
2. dig through task history and logs to find the *actual* root cause (often an **upstream**
   task, not the one that visibly failed),
3. understand the **blast radius** — which downstream tables, pipelines, and reports are now
   stale or wrong,
4. write a fix,
5. test the fix safely without corrupting production data.

This is slow, requires deep tribal knowledge, and doesn't scale.

**ops-monitor automates this loop.** It behaves like an always-on data-ops engineer that
monitors pipelines, performs lineage-aware RCA, proposes concrete fixes, and validates them
on a disposable clone of the database — all steerable through a chat interface.

### Guiding design principles

- **Snowflake is the source of truth.** The app keeps almost no pipeline data of its own; it
  reconstructs everything live from Snowflake system catalogs.
- **Agents are skill-driven Python classes**, each backed (optionally) by Claude. If no LLM key
  is configured, every agent falls back to deterministic logic, so the tool always works.
- **Production is protected.** All SQL is guardrailed and audited; writes only ever hit
  disposable clone / pre-prod databases.

---

## 2. End-to-end project flow

```
                        ┌─────────────────────────────────────────┐
                        │  React UI: Dashboard / Workbench / Chat   │
                        └───────────────────┬───────────────────────┘
                                            │ REST (/api/*)
                        ┌───────────────────▼───────────────────────┐
                        │  FastAPI (main.py) → Orchestrator           │
                        │  (owns one instance of each agent)          │
                        └───────────────────┬───────────────────────┘
        ┌───────────────┬───────────────────┼───────────────┬────────────────┐
        ▼               ▼                   ▼               ▼                ▼
 ConnectivityAgent  MonitoringAgent      RCAAgent        FixAgent         TestAgent
   health probes   pull telemetry     root cause      before/after     clone + validate
                    + auto-log         + lineage        code fix         + teardown
        │               │                   │               │                │
        └───────────────┴─────────┬─────────┴───────────────┴────────────────┘
                                  ▼
                    Connectors (Snowflake / AWS / DQ / Lineage)
                                  ▼
                              Snowflake
```

### The incident lifecycle (happy path)

1. **Detect** — `MonitoringAgent.collect()` pulls live task/job telemetry. Any `FAILED` or
   `DELAYED` pipeline is automatically written to the shared **incident log**.
2. **Analyze (RCA)** — `RCAAgent.analyze(pipeline_id)` loads the task dependency graph, walks
   **upstream** to the earliest failing ancestor, classifies the failure, resolves affected
   tables, gathers evidence, and (optionally) asks Claude to sharpen the explanation.
3. **Fix** — `FixAgent.suggest()` produces a **before/after** code diff from the RCA context
   (LLM-generated, or category-based templates as fallback).
4. **Validate** — `TestAgent.validate()` creates a **zero-copy clone** of the database, runs a
   suite of test cases against it, tears the clone down, and stamps the incident with a
   PASS/FAIL verdict. **This update is what marks the incident "resolved."**

The same four steps can be triggered individually from the **Workbench**, chained end-to-end via
`POST /api/remediate/{id}`, or driven conversationally through **Chat** (which detects intent and
calls the relevant agent).

### Key entry points

| Layer | Entry point |
|-------|-------------|
| Backend server | `uvicorn app.main:app` (port 8001) |
| Backend hub | `orchestrator` singleton in `backend/app/agents/orchestrator.py` |
| Frontend | `frontend/src/main.jsx` → `App.jsx` (port 5173) |

---

## 3. What data we receive from Snowflake

Snowflake data falls into three buckets. **The majority is operational metadata** — data
Snowflake keeps *about itself* — not business rows.

### A. Operational metadata (Snowflake system catalogs)

| Source | Data received |
|--------|---------------|
| `SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY` + `INFORMATION_SCHEMA.TASK_HISTORY` | Every scheduled task run: name, database, schema, `STATE` (SUCCEEDED/FAILED/RUNNING/SCHEDULED), `QUERY_ID`, scheduled/start/completed times, `ERROR_CODE`, `ERROR_MESSAGE`, duration |
| `SNOWFLAKE.ACCOUNT_USAGE.TASKS` | Task definitions: name, db, schema, `PREDECESSORS` (JSON — the dependency graph), `DEFINITION` (the task's SQL body) |
| `SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY` | The actual SQL text (`query_text`) of a past/failed query — parsed to discover which tables were touched |
| `INFORMATION_SCHEMA.VIEWS` / `INFORMATION_SCHEMA.PROCEDURES` | Source code (`VIEW_DEFINITION`, `PROCEDURE_DEFINITION`) — to read failing code and find downstream consumers |

### B. Data-quality tables (your configured business tables)

Configured in `backend/app/core/config.py`, overridable via the Settings screen:

| Source (default) | Data received |
|------------------|---------------|
| `DQM_VALIDATION_SUMMARY` (e.g. `CPH_DB_PRE_PROD.MODEL_V2.DQM_VALIDATION_SUMMARY`) | DQ check results: `RUN_DATE`, `QC_ID`, `SUBJECT_AREA`, `CHECK_TYPE`, `PASS_COUNT`, `FAIL_COUNT`, `STATUS`, `QC_DESCRIPTION` |
| DQ rules table (e.g. `CPH_DB_PROD.MODEL_V2.CONFIG_LYNKUET`) | Rule definitions, importantly the `SQL_CODE` column (the check's actual SELECT statement) |

### C. Actual business data (touched transiently only)

- When **executing a DQ rule's SQL** to fetch the real failing rows (read-only).
- Inside a **zero-copy clone** during fix validation (never against production directly).

### Logs

There is **no separate log store**. "Logs" in this project are effectively the
`ERROR_MESSAGE` on task runs plus the SQL text from `QUERY_HISTORY`. The Snowflake connector's
`get_logs()` currently returns a reference placeholder rather than streaming a log file.

### What is NOT stored in Snowflake

The app's own state — incidents, agent memory, settings, SQL audit trail — lives in **local
JSON files** under `backend/app/memory/store/` and `backend/app/data/`. Snowflake is a
**read source** (plus temporary clones); the application never writes its own data back into it.

---

## 4. SQL queries executed and what they retrieve

All SQL is routed through a **guardrail** (`connectors/sql_guardrail.py`): reads run and are
logged; production writes are blocked and queued for approval; every statement is appended to
`data/sql_audit_log.json`.

| # | Purpose | Query (essence) | Retrieves | Runs during |
|---|---------|-----------------|-----------|-------------|
| 1 | Connectivity probe | `SELECT CURRENT_VERSION()` / `SELECT 1` | version / heartbeat | health check |
| 2 | Task telemetry | `TASK_HISTORY` (historical `ACCOUNT_USAGE` ∪ future `INFORMATION_SCHEMA`) filtered by DB + `NAME ILIKE 'TASK%'` | task runs, states, errors, timing, query_id | Monitoring |
| 3 | Task dependency graph | `SELECT DATABASE_NAME, SCHEMA_NAME, NAME, PREDECESSORS FROM ACCOUNT_USAGE.TASKS WHERE DELETED IS NULL` | task edges (upstream/downstream) | RCA |
| 4 | Failed query text | `SELECT query_text FROM ACCOUNT_USAGE.QUERY_HISTORY WHERE query_id = ?` | SQL of the failed query → table names | RCA |
| 5 | Object source | `SELECT DEFINITION FROM ACCOUNT_USAGE.TASKS ...` / `PROCEDURE_DEFINITION` / `VIEW_DEFINITION` | task/proc/view source code | RCA |
| 6 | Downstream discovery | `... FROM {db}.INFORMATION_SCHEMA.VIEWS WHERE LOWER(VIEW_DEFINITION) LIKE '%table%'` (and PROCEDURES) | views/procs referencing an affected table (blast radius) | RCA |
| 7 | DQ results | `SELECT RUN_DATE, QC_ID, ... FROM {dq_table} WHERE SUBJECT_AREA = ?` | data-quality pass/fail per check | Monitoring (DQ) |
| 8 | DQ rule lookup | `SELECT * FROM {rules_table} WHERE QC_ID = ?` | rule row incl. `SQL_CODE` | RCA (DQ) |
| 9 | DQ rule execution | `SELECT * FROM ( <rule SQL_CODE> ) LIMIT N` (after `USE ROLE/WAREHOUSE/DATABASE/SCHEMA`) | actual failing rows | RCA (DQ) |
| 10 | Zero-copy clone | `CREATE OR REPLACE DATABASE {db}_PREPROD_CLONE CLONE {db}` | — (write, clone only) | Test |
| 11 | Teardown | `DROP DATABASE IF EXISTS {clone}` | — (write, clone only) | Test |

Relevant files: `connectors/snowflake_connector.py`, `connectors/lineage_service.py`,
`connectors/dq_connector.py`.

---

## 5. Data validation / data-quality checks

Data-quality (DQ) is a first-class concept, driven by the two configured DQ tables.

### How DQ works

1. **Results feed** — `DQConnector.read_results()` reads the DQ summary table filtered by
   `SUBJECT_AREA`, returning per-check `PASS_COUNT` / `FAIL_COUNT` / `STATUS`. Statuses are
   normalized (`PASS/FAIL/WARN/SKIP/...` → `SUCCESS/FAILED/DELAYED/SKIPPED/RUNNING`).
2. **Rule definition** — `DQConnector.fetch_dq_rule_sql()` looks up the rule for a `QC_ID` in the
   rules table and extracts its `SQL_CODE` (the check's SELECT statement).
3. **Rule execution** — `DQConnector.execute_dq_rule()` wraps and runs that SQL
   (`SELECT * FROM (<rule>) LIMIT 20`) using a **read-only role** where configured, to fetch the
   **actual failing rows** — real evidence, not just a count.

### Types of checks (as classified during RCA)

The RCA classifier (`_classify` in `rca_agent.py`) maps errors/DQ signals into categories that
include a dedicated **Data Quality Failure** bucket, triggered by signals such as:

- null / NaN values, failed `CAST` / numeric conversion
- schema mismatch
- duplicate / grain violations
- record-count / reconciliation mismatches
- threshold breaches, validation failures

### DQ correlation in RCA

`LineageService.correlate_dq()` links a failed **task** to failed **DQ checks** by scoring
**shared tables** and **time proximity** (within ~48h) and name overlap. This is how a task
failure and a DQ failure that stem from the same root cause get connected in the analysis.

DQ-specific incidents (pipeline ids starting with `dq_`) are analyzed by a dedicated
`RCAAgent.analyze_dq()` path that correlates the check to related failed tasks by shared tables.

---

## 6. How RCA is generated

`RCAAgent.analyze(pipeline_id)` (in `backend/app/agents/rca_agent.py`) is the core. It combines
deterministic lineage analysis with optional LLM enhancement. Conceptually:

1. **Identify the failed object** — collect live pipelines, enrich the target with its task key
   and resolved tables.
2. **Walk upstream to the true root cause** — `_walk_upstream_root()` follows the dependency
   graph (from `TASKS.PREDECESSORS`) backward to the **earliest failing ancestor**. The visible
   failure is often just a symptom of an upstream break.
3. **Classify the failure** — into one of six categories: Code, Data Quality, Dependency,
   Infrastructure, Data Availability, or Unknown.
4. **Compute the blast radius** — `_downstream_impact()` walks forward to collect impacted
   tasks; `discover_all_downstream()` finds views/procedures that reference affected tables
   (impacted reports/marts → severity rating).
5. **Resolve tables & lineage** — parse the failed query text and task/procedure SQL to build
   upstream and downstream lineage graphs (source tables → task → output tables → consumers).
6. **Read the code** — fetch the failing task's `DEFINITION`, follow any `CALL` into the
   procedure body, and (for DQ) fetch + execute the rule SQL to get failing rows.
7. **Gather evidence & confidence** — task metadata, error message, query id, resolved tables,
   correlated DQ failures, and prior similar cases from memory. Confidence is scored from how
   many of these signals are present.
8. **Recall + knowledge** — `recall()` finds prior RCA memories with the same signature/tables;
   `_match_knowledge_rules()` matches user-contributed rules in `skills/rca_knowledge.md`.
9. **LLM enhancement (optional)** — `self.think()` sends the full context (SQL, procedure chain,
   DQ results, priors, domain knowledge) through the `rca.md` skill to Claude, which returns a
   structured JSON RCA (summary, root cause, remediation). Without an API key, deterministic
   templates are used instead.
10. **Persist & learn** — the incident is logged (`source: "rca"`) and a memory record is written
    so future similar failures resolve faster and with higher confidence.

The result object includes: summary, category, confidence, evidence, root-cause object,
remediation, impact assessment, lineage graphs, an investigation "journey," and a natural-language
chat narrative.

---

## 7. How the agents collaborate

There is **no message bus or background workers**. Agents are Python classes that call each
other **synchronously**, passing plain dictionaries. Two coordination patterns exist:

### Pattern A — Orchestrator delegates (API routes)

`main.py` routes call `orchestrator.<method>()`, which forwards to the right agent. The
end-to-end pipeline is a simple chain:

```
auto_remediate(pipeline_id):
    rca  = RCAAgent.analyze(pipeline_id)
    fix  = FixAgent.suggest(pipeline_id, incident_id=rca.incident_id)
    test = TestAgent.validate(pipeline_id, fix.fix_id)
```

### Pattern B — Chat routes by intent

`ChatAgent.handle()` keyword-detects intent ("run rca", "suggest fix", "validate", "status",
"add issue") and calls the relevant agent directly, then narrates the structured result via
Claude.

### Shared substrate they collaborate through

| Mechanism | Role |
|-----------|------|
| **Incident log** (`memory/memory_store.py`) | The backbone — every agent appends/updates the same shared incident record |
| **AgentMemory** (per agent) | Each agent learns from and recalls its own past records |
| **LineageService** | Shared graph/table resolution used by RCA (and Fix/Test indirectly) |
| **Connectors** | Snowflake/AWS/DQ I/O, reused by Monitoring, RCA, Test |

### Cross-agent dependencies (worth knowing)

- `FixAgent.suggest()` re-derives context by calling `MonitoringAgent().collect()` and, if not
  given an `incident_id`, `RCAAgent().analyze()`.
- `TestAgent.validate()` calls `FixAgent().suggest()` (without an incident_id), which **re-runs
  RCA internally**. So in the full remediate flow, RCA effectively runs more than once — a known
  inefficiency, not a bug.

### The BaseAgent contract

Every agent inherits `BaseAgent` (`agents/base.py`), which provides:
`self.skill` (its Markdown contract), `self.harness` (Claude wrapper, degrades gracefully),
`self.memory` (JSON learning), and `self.incident_log` (shared state).

---

## 8. Key Snowflake concepts used

Understanding these Snowflake features makes the codebase click:

- **Tasks** — Snowflake's scheduled units of SQL execution. This project's entire "pipeline"
  concept maps to Snowflake tasks (name pattern `TASK%` by default).
- **Task DAG via `PREDECESSORS`** — tasks can declare predecessor tasks, forming a dependency
  graph. RCA walks this graph to find the true upstream root cause.
- **`ACCOUNT_USAGE` schema** — account-wide historical metadata (task history, task defs, query
  history). Note: `ACCOUNT_USAGE` views have **latency** (data can lag by minutes to hours) but
  provide long history.
- **`INFORMATION_SCHEMA`** — per-database, real-time metadata (views, procedures, and the
  table function `TASK_HISTORY()` used for near-future/scheduled runs).
- **`QUERY_HISTORY`** — the executed SQL text, keyed by `QUERY_ID` (which each task run exposes).
  Used to recover which tables a failed query referenced.
- **Views & stored procedures** — tasks often `CALL` a procedure; RCA reads
  `PROCEDURE_DEFINITION` / `VIEW_DEFINITION` to analyze the real transformation logic and to
  trace downstream consumers.
- **Zero-copy clone** (`CREATE DATABASE ... CLONE ...`) — instantly creates a metadata-only copy
  of a database that shares underlying storage until modified. This is what makes safe,
  cheap fix validation possible: clone → test → drop, with no impact on production.
- **Roles / read-only role** — DQ rule execution prefers a configured `read_only_role` so
  running a check can never mutate data.
- **Warehouses** — compute context; the app sets `USE WAREHOUSE` before executing DQ SQL.
- **Authenticators** — supports password and SSO / `externalbrowser` auth (see `core/config.py`).

---

## 9. Where state lives (quick reference)

| Store | Path | Purpose |
|-------|------|---------|
| Incident log | `backend/app/memory/store/incident_log.json` | Shared incidents across all agents |
| Agent memory | `backend/app/memory/store/<agent>_memory.json` | Per-agent learned patterns |
| Connection settings | `backend/app/data/connection_settings.json` | AWS + Snowflake creds (created on Save) |
| SQL audit log | `backend/app/data/sql_audit_log.json` | Every SQL statement executed/blocked |
| RCA knowledge | `backend/app/skills/rca_knowledge.md` | User-contributed failure patterns |
| LLM config | `.env` (project root) | `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `CLAUDE_MODEL` |

---

## 10. Getting started

```powershell
# Backend (port 8001)
cd backend
C:\...\python.exe -m venv .venv
.\.venv\Scripts\pip.exe install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8001

# Frontend (port 5173)
cd frontend
npm install
npm run dev
```

- API docs: `http://localhost:8001/docs` · UI: `http://localhost:5173`
- Verify LLM: `GET /api/health` → `llm.available: true` (works without it via fallbacks).
- Go live: enter Snowflake/AWS creds in **Settings**; add a `preprod_account` for real clone
  validation.

### Suggested reading order

1. `agents/orchestrator.py` (the whole API surface, tiny)
2. `agents/base.py` (skill + harness + memory contract)
3. `agents/monitoring_agent.py` → `agents/rca_agent.py::analyze`
4. `connectors/snowflake_connector.py::_live_telemetry` and
   `connectors/lineage_service.py::load_task_graph`

### Gotchas

- The README mentions a **MOCK mode / `mock_data.py`** that no longer exists — the backend is
  **live-only** and returns empty data when credentials aren't configured.
- `ANTHROPIC_BASE_URL` must point to the corporate gateway; without it chat silently falls back
  to templates.
- Restart the backend after code changes (no auto-reload by default).
