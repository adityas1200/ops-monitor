# Business Requirements Document (BRD)

## Agentic Data-Ops Monitoring System (Ops Monitor)

| Field | Detail |
|-------|--------|
| **Document Version** | 1.0 |
| **Date** | 2026-07-06 |
| **Status** | Draft |
| **Project Name** | Ops Monitor — Agentic Data-Ops Monitoring System |
| **Repository** | ops-monitor (initial-run branch) |

---

## 1. Executive Summary

The Ops Monitor is an AI-augmented operations monitoring platform designed to provide end-to-end visibility, automated root cause analysis, and guided remediation for data pipeline failures across Snowflake and AWS environments. The system reduces Mean Time to Resolution (MTTR) for data operations teams by combining real-time monitoring dashboards with intelligent AI agents that can diagnose failures, suggest fixes, validate solutions in isolated environments, and learn from past incidents.

---

## 2. Business Objectives

| # | Objective | Success Metric |
|---|-----------|----------------|
| BO-1 | Reduce pipeline failure resolution time | MTTR reduced by ≥50% compared to manual triage |
| BO-2 | Provide unified visibility across data platforms | Single-pane dashboard covering Snowflake tasks + AWS Glue/Step Functions |
| BO-3 | Enable proactive data quality monitoring | DQ validation failures surfaced before downstream consumers are impacted |
| BO-4 | Automate root cause analysis | ≥80% of failures receive an automated RCA classification without manual investigation |
| BO-5 | Ensure safe remediation | All fixes validated in zero-copy clone environments before production application |
| BO-6 | Build institutional knowledge | Agent memory captures resolution patterns for recurring incidents |

---

## 3. Scope

### 3.1 In Scope

| Area | Description |
|------|-------------|
| Pipeline Monitoring | Snowflake task history, AWS Glue jobs, Step Functions state machines |
| Data Quality Monitoring | DQ validation summary checks (pass/fail rates, subject areas, check types) |
| Automated Root Cause Analysis | Upstream dependency tracing, failure classification, impact assessment |
| Fix Suggestion & Editing | AI-generated code diffs with interactive user editing |
| Safe Validation | Zero-copy Snowflake clones for test execution |
| Lineage Visualization | Task-level and table-level dependency DAGs |
| Conversational Interface | Context-aware chat with intent routing across 14 intent categories |
| Agent Memory & Learning | Persistent per-agent memory and shared incident log |
| Multi-auth Platform Connectivity | Snowflake (SSO/password), AWS (IAM credentials) |

### 3.2 Out of Scope

| Area | Rationale |
|------|-----------|
| Multi-user authentication & RBAC | Designed as a single-user/team internal tool |
| Production fix deployment | Fixes are suggested and validated, not auto-deployed |
| Non-Snowflake/AWS platforms | Current integrations limited to Snowflake and AWS |
| Historical trend analytics | No long-term metric storage beyond Snowflake's native retention |
| Alerting & notification | No push notifications, email, or Slack integration |
| CI/CD integration | No pipeline triggering or deployment automation |

---

## 4. Stakeholders

| Role | Responsibility |
|------|----------------|
| Data Operations Engineers | Primary users — monitor pipelines, resolve failures, validate fixes |
| Data Quality Analysts | Monitor DQ validation status, investigate check failures |
| Data Platform Administrators | Configure connections, manage monitoring targets, oversee agent behavior |
| Engineering Leadership | Review operational KPIs, assess MTTR improvements |

---

## 5. Functional Requirements

### 5.1 Dashboard & Monitoring (FR-100)

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-101 | System SHALL display a summary dashboard with KPI cards showing total, succeeded, failed, and running pipeline counts | Must |
| FR-102 | System SHALL display a filterable and sortable task execution table with pipeline ID, name, platform, status, timestamps, duration, and SLA breach indicators | Must |
| FR-103 | System SHALL support filtering tasks by status (FAILED, SUCCEEDED, RUNNING, SCHEDULED, SKIPPED) and by date range | Must |
| FR-104 | System SHALL display Data Quality validation results in a dedicated sub-tab with check ID, subject area, check type, pass/fail counts, and status | Must |
| FR-105 | System SHALL provide a one-click "Run RCA" action from any failed pipeline row | Must |
| FR-106 | System SHALL calculate SLA breach by comparing task duration against configured SLA thresholds | Should |
| FR-107 | System SHALL support configurable monitoring scope via database name patterns, historical lookback period (months), and future scheduling window (days) | Must |

### 5.2 Connectivity & Configuration (FR-200)

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-201 | System SHALL support Snowflake connections via password authentication (account, user, password, warehouse, role, database, schema) | Must |
| FR-202 | System SHALL support Snowflake connections via SSO/External Browser authentication | Must |
| FR-203 | System SHALL support AWS connections via IAM access key credentials with optional session tokens | Must |
| FR-204 | System SHALL validate platform connectivity on demand and report connection status per platform | Must |
| FR-205 | System SHALL persist connection settings locally with secrets masked in API responses (first 2 + last 2 characters visible) | Must |
| FR-206 | System SHALL provide context-aware error hints when connection attempts fail (e.g., auth mode mismatch, network issues, role errors) | Should |
| FR-207 | System SHALL support configuring a separate pre-production Snowflake account for zero-copy clone validation | Should |

### 5.3 Root Cause Analysis (FR-300)

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-301 | System SHALL perform automated root cause analysis on failed pipelines by traversing the upstream task dependency graph | Must |
| FR-302 | System SHALL classify failures into categories: Code Error, Data Quality, Dependency Failure, Infrastructure, Data Availability, Unknown | Must |
| FR-303 | System SHALL identify the root cause task(s) in an upstream dependency chain and distinguish them from cascading failures | Must |
| FR-304 | System SHALL generate an impact assessment including affected downstream tasks and tables | Must |
| FR-305 | System SHALL correlate DQ validation failures with task failures via shared table references | Should |
| FR-306 | System SHALL produce a structured RCA report containing: summary, failure category, root cause, timeline, impact assessment, and remediation steps | Must |
| FR-307 | System SHALL discover downstream consumers (views, procedures) via Snowflake INFORMATION_SCHEMA to assess blast radius | Should |
| FR-308 | System SHALL support user-provided additional context to guide the AI analysis | Should |

### 5.4 Lineage Visualization (FR-400)

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-401 | System SHALL visualize task-level dependency graphs as directed acyclic graphs (DAGs) with topological layout | Must |
| FR-402 | System SHALL visualize table-level lineage graphs showing source, root cause, failed, impacted, and healthy nodes with color coding | Must |
| FR-403 | System SHALL provide a tabular lineage view showing affected tables with their associated DQ checks and tasks | Should |
| FR-404 | Lineage graphs SHALL highlight the failure propagation path from root cause through downstream dependencies | Must |

### 5.5 Fix Suggestion & Remediation (FR-500)

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-501 | System SHALL generate fix suggestions as before/after code diffs based on the identified failure category | Must |
| FR-502 | System SHALL support interactive editing of suggested fixes before validation | Must |
| FR-503 | System SHALL support an end-to-end remediation workflow: RCA → Fix → Validate in a single action | Should |
| FR-504 | Fix suggestions SHALL be informed by past incident resolutions stored in agent memory | Should |

### 5.6 Validation & Testing (FR-600)

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-601 | System SHALL create zero-copy Snowflake clones for safe validation of proposed fixes | Must |
| FR-602 | System SHALL execute validation test suites against the cloned environment | Must |
| FR-603 | System SHALL capture test evidence (pass/fail results, execution logs) before tearing down clones | Must |
| FR-604 | System SHALL automatically tear down cloned environments after validation completes | Must |
| FR-605 | System SHALL report validation results with clear pass/fail indicators and execution details | Must |

### 5.7 Conversational Interface (FR-700)

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-701 | System SHALL provide an always-visible chat interface with context-aware conversation | Must |
| FR-702 | System SHALL detect user intent across 14 categories including: ask_status, investigate_failure, ask_dq, fix_request, run_tests, explain_lineage, configure, and general_question | Must |
| FR-703 | Chat responses SHALL be context-aware, synchronized with the currently viewed dashboard tab and selected pipeline | Must |
| FR-704 | System SHALL provide quick-action buttons in the chat for common operations (check status, run RCA, suggest fix, validate) | Should |
| FR-705 | System SHALL provide guided resolution steps for task failures and DQ check failures | Must |
| FR-706 | System SHALL support activity error reporting from the UI for agent-guided troubleshooting | Should |

### 5.8 Agent Memory & Learning (FR-800)

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-801 | Each agent SHALL maintain a persistent memory store (JSON-based) that survives application restarts | Must |
| FR-802 | Agents SHALL recall relevant past resolutions when encountering similar failures | Must |
| FR-803 | System SHALL maintain a shared incident log accessible by all agents | Must |
| FR-804 | Users SHALL be able to report issues that are persisted into agent memory for future reference | Should |
| FR-805 | Memory recall SHALL support substring-based search for relevant past entries | Should |
| FR-806 | Agent memory files SHALL be human-readable (transparent JSON) for auditability | Must |

---

## 6. Non-Functional Requirements

### 6.1 Performance (NFR-100)

| ID | Requirement | Target |
|----|-------------|--------|
| NFR-101 | Dashboard initial load time | ≤ 3 seconds |
| NFR-102 | RCA analysis completion time | ≤ 30 seconds for typical pipeline failures |
| NFR-103 | Connectivity test response time | ≤ 10 seconds (120s timeout for SSO browser auth) |
| NFR-104 | Chat response latency | ≤ 5 seconds for non-analysis intents |

### 6.2 Reliability (NFR-200)

| ID | Requirement | Description |
|----|-------------|-------------|
| NFR-201 | Graceful degradation | System SHALL function in mock/demo mode when platform credentials are not configured |
| NFR-202 | AI fallback | System SHALL provide deterministic fallback logic when Anthropic API key is unavailable |
| NFR-203 | Error resilience | Connection failures to external platforms SHALL not crash the application |

### 6.3 Security (NFR-300)

| ID | Requirement | Description |
|----|-------------|-------------|
| NFR-301 | Credential masking | API responses SHALL mask sensitive credentials (show only first 2 + last 2 characters) |
| NFR-302 | Local storage | Credentials SHALL be stored in local files only, not transmitted to external services beyond their intended platform |
| NFR-303 | Git exclusion | Credential files and memory stores SHALL be excluded from version control via .gitignore |
| NFR-304 | SSO support | System SHALL support enterprise SSO authentication flows for Snowflake access |

### 6.4 Usability (NFR-400)

| ID | Requirement | Description |
|----|-------------|-------------|
| NFR-401 | Theme support | System SHALL support light and dark themes with user preference persistence |
| NFR-402 | Responsive layout | Dashboard SHALL be usable on standard desktop screen resolutions (≥1280px) |
| NFR-403 | Loading states | All async operations SHALL display appropriate loading indicators (skeletons, overlays) |
| NFR-404 | Error guidance | Connection and operation errors SHALL include actionable hints for resolution |

### 6.5 Deployability (NFR-500)

| ID | Requirement | Description |
|----|-------------|-------------|
| NFR-501 | Single-machine deployment | System SHALL run on a single Windows machine without external infrastructure dependencies |
| NFR-502 | Minimal prerequisites | System SHALL require only Python 3.x and Node.js as runtime dependencies |
| NFR-503 | Public sharing | System SHALL support optional public URL sharing via Cloudflare tunnels for demonstrations |
| NFR-504 | Pre-built frontend | A production-ready frontend bundle SHALL be available for zero-build deployment |

---

## 7. Technical Architecture Summary

### 7.1 System Components

```
┌─────────────────────────────────────────────────────────────┐
│                     Frontend (React + Vite)                   │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────┐   │
│  │Dashboard │ │Workbench │ │ Settings │ │ Chat Window  │   │
│  └──────────┘ └──────────┘ └──────────┘ └──────────────┘   │
└────────────────────────────┬────────────────────────────────┘
                             │ REST API (/api/*)
┌────────────────────────────▼────────────────────────────────┐
│                   Backend (FastAPI + Uvicorn)                 │
│  ┌───────────────────────────────────────────────────────┐  │
│  │                    Orchestrator                         │  │
│  │  ┌─────────┐ ┌────────┐ ┌─────┐ ┌────┐ ┌────┐ ┌────┐│  │
│  │  │Connect. │ │Monitor │ │ RCA │ │Fix │ │Test│ │Chat││  │
│  │  │Agent    │ │Agent   │ │Agent│ │Agt │ │Agt │ │Agt ││  │
│  │  └─────────┘ └────────┘ └─────┘ └────┘ └────┘ └────┘│  │
│  └───────────────────────────────────────────────────────┘  │
│  ┌───────────────┐  ┌──────────────┐  ┌────────────────┐   │
│  │  Connectors   │  │ Memory Store │  │  Skill Files   │   │
│  │(SF/AWS/DQ/Lin)│  │  (JSON-based) │  │  (Markdown)    │   │
│  └───────┬───────┘  └──────────────┘  └────────────────┘   │
└──────────┼──────────────────────────────────────────────────┘
           │
┌──────────▼──────────────────────────────────────────────────┐
│              External Services                                │
│  ┌──────────┐  ┌──────────┐  ┌───────────────────────────┐  │
│  │Snowflake │  │   AWS    │  │  Anthropic Claude API     │  │
│  │(Tasks/DQ)│  │(Glue/SF) │  │  (Optional AI reasoning) │  │
│  └──────────┘  └──────────┘  └───────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

### 7.2 Technology Stack

| Layer | Technology | Version |
|-------|-----------|---------|
| Frontend Framework | React | 18.3.1 |
| Frontend Build Tool | Vite | 5.4.2 |
| Backend Framework | FastAPI | 0.115.0 |
| ASGI Server | Uvicorn | 0.30.6 |
| Data Validation | Pydantic | 2.9.2 |
| Snowflake SDK | snowflake-connector-python | 4.6.0 |
| AWS SDK | boto3 | 1.35.24 |
| AI SDK | anthropic | 0.39.0 |
| AI Model | Claude Sonnet 4 | claude-sonnet-4-6 |
| Language (Backend) | Python | 3.x |
| Language (Frontend) | JavaScript (JSX) | ES2020+ |

### 7.3 Data Flow

1. **Monitoring Flow:** Snowflake TASK_HISTORY → Monitoring Agent → Dashboard KPIs + Table
2. **RCA Flow:** Failed Task → Dependency Graph Traversal → Upstream Root Cause → Classification → Impact Assessment → RCA Report
3. **Remediation Flow:** RCA Report → Fix Suggestion → User Edit → Zero-Copy Clone → Test Execution → Validation Results
4. **Chat Flow:** User Message → Intent Detection → Agent Routing → Context-Aware Response
5. **Memory Flow:** Agent Actions → Memory Persistence → Future Recall → Improved Responses

---

## 8. Integration Points

| Integration | Protocol | Purpose | Authentication |
|-------------|----------|---------|----------------|
| Snowflake Account Usage | SQL (snowflake-connector) | Task history, DAG structure, query history | Password or SSO |
| Snowflake Information Schema | SQL (snowflake-connector) | Downstream view/procedure discovery | Password or SSO |
| Snowflake DQ Table | SQL (snowflake-connector) | Data quality validation results | Password or SSO |
| Snowflake Zero-Copy Clone | SQL DDL (snowflake-connector) | Safe test environments | Password or SSO |
| AWS Glue | boto3 (REST) | Job run history and logs | IAM Access Key |
| AWS Step Functions | boto3 (REST) | State machine execution history | IAM Access Key |
| AWS CloudWatch | boto3 (REST) | Log retrieval for failed jobs | IAM Access Key |
| AWS DynamoDB | boto3 (REST) | Telemetry data | IAM Access Key |
| Anthropic Claude API | HTTPS (anthropic SDK) | AI-powered reasoning and analysis | API Key |
| Cloudflare Tunnel | Binary (cloudflared.exe) | Public URL sharing | Anonymous quick tunnel |

---

## 9. User Workflows

### 9.1 Pipeline Failure Investigation (Primary)

```
1. User opens Dashboard → sees failed pipeline highlighted in red
2. User clicks "Run RCA" → system traverses upstream dependencies
3. System presents RCA report on Workbench with:
   - Root cause identification
   - Failure classification
   - Impact assessment (downstream tasks + tables)
   - Lineage graph visualization
   - Recommended remediation steps
4. User reviews and optionally requests a fix suggestion
5. System generates code diff → user edits if needed
6. User triggers validation → system creates zero-copy clone + runs tests
7. Validation results confirm fix efficacy
8. User applies fix to production manually
```

### 9.2 Data Quality Investigation

```
1. User views DQ Status tab → identifies failing checks
2. User asks Chat agent about the failure
3. Chat agent correlates DQ failure with upstream task failures
4. System presents affected table lineage and impacted consumers
5. User follows guided resolution steps from chat
```

### 9.3 Proactive Monitoring

```
1. User configures monitoring targets in Settings (database, name pattern, lookback)
2. Dashboard auto-refreshes with current pipeline status
3. KPI cards provide at-a-glance health summary
4. SLA breach indicators flag slow-running tasks
5. Scheduled tasks visible in future window
```

---

## 10. Constraints & Assumptions

### 10.1 Constraints

| # | Constraint |
|---|-----------|
| C-1 | System runs on Windows environments (corporate VDI) |
| C-2 | No external database — all state is file-based (JSON) |
| C-3 | SSO authentication requires the backend to run on a machine with browser access |
| C-4 | AI reasoning requires an Anthropic API key (degrades gracefully without one) |
| C-5 | Snowflake ACCOUNT_USAGE views have up to 45-minute latency |
| C-6 | Single-user deployment model — no concurrent multi-user support |

### 10.2 Assumptions

| # | Assumption |
|---|-----------|
| A-1 | Users have network access to Snowflake and AWS from their machine |
| A-2 | Snowflake tasks follow naming conventions matchable by SQL LIKE patterns |
| A-3 | Task dependencies are defined via Snowflake predecessor relationships |
| A-4 | DQ validation results are stored in a standardized table structure (DQM_VALIDATION_SUMMARY) |
| A-5 | Users have sufficient Snowflake roles to query ACCOUNT_USAGE and create clones |
| A-6 | Python 3.x and Node.js are available on the deployment machine |

---

## 11. Risks & Mitigations

| # | Risk | Impact | Likelihood | Mitigation |
|---|------|--------|------------|------------|
| R-1 | Snowflake ACCOUNT_USAGE latency (up to 45 min) may show stale data | Medium | High | Supplement with INFORMATION_SCHEMA.TASK_HISTORY() for recent data; display data freshness indicator |
| R-2 | AI API unavailability degrades analysis quality | Medium | Low | Deterministic fallback logic in all agents; system remains functional |
| R-3 | SSO token expiration during long sessions | Low | Medium | Connectivity agent detects and prompts re-authentication |
| R-4 | Complex DAG traversal may timeout for very large dependency trees | Medium | Low | Configurable traversal depth limits; async processing |
| R-5 | File-based state loss if machine is reimaged | High | Medium | State files can be backed up; memory is supplementary, not critical |
| R-6 | Credential exposure via local file storage | High | Low | .gitignore exclusion; API masking; future consideration for OS keychain integration |

---

## 12. Future Considerations

| # | Enhancement | Business Value |
|---|-------------|---------------|
| F-1 | Multi-user deployment with RBAC | Enable team-wide adoption |
| F-2 | Push notifications (Slack, Teams, Email) | Proactive alerting for failures |
| F-3 | Historical trend analytics and reporting | SLA compliance tracking over time |
| F-4 | Additional platform connectors (dbt, Airflow, Databricks) | Broader pipeline coverage |
| F-5 | Automated fix deployment with approval workflows | Further reduce MTTR |
| F-6 | Incident correlation across time windows | Pattern detection for recurring failures |
| F-7 | OS keychain/vault integration for credentials | Enhanced security posture |
| F-8 | Docker containerization | Simplified deployment and portability |

---

## 13. Acceptance Criteria

| # | Criteria | Verification Method |
|---|----------|-------------------|
| AC-1 | Dashboard displays pipeline status within 3 seconds of page load | Performance test |
| AC-2 | RCA correctly identifies root cause for single-upstream-failure scenarios | Test with known failure patterns |
| AC-3 | Fix validation executes successfully on zero-copy clones without affecting production | Clone isolation verification |
| AC-4 | Chat agent correctly routes ≥12 of 14 defined intents | Intent classification test suite |
| AC-5 | System starts and displays mock data without any external credentials configured | Fresh install test |
| AC-6 | Credentials are never visible in full in API responses or browser dev tools | Security review |
| AC-7 | Agent memory persists across application restarts | Restart-and-recall test |
| AC-8 | Lineage graphs render correctly for DAGs with ≤50 nodes | Visual inspection |

---

## 14. Glossary

| Term | Definition |
|------|-----------|
| **DAG** | Directed Acyclic Graph — represents task dependency relationships |
| **DQ** | Data Quality — validation checks on data completeness and accuracy |
| **MTTR** | Mean Time to Resolution — average time from failure detection to fix |
| **RCA** | Root Cause Analysis — systematic identification of the primary failure source |
| **SLA** | Service Level Agreement — maximum acceptable duration for a pipeline |
| **Zero-Copy Clone** | Snowflake feature creating an instant, cost-free copy of data for testing |
| **SSO** | Single Sign-On — federated authentication via corporate identity provider |
| **KPI** | Key Performance Indicator — summary metric for operational health |

---

*Document prepared based on codebase assessment of the ops-monitor repository (initial-run branch) as of 2026-07-06.*
