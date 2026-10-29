# Ops Monitor

Ops Monitor is a web application for monitoring data operations, investigating failures, and suggesting fixes. It reads live telemetry from Snowflake and, when configured, AWS. It combines that telemetry with lineage-aware root-cause analysis (RCA), an operator workbench, and an optional Claude-powered assistant.

> **Current operating mode:** live data only. The application does not include mock telemetry. Configure at least Snowflake before expecting monitoring data.

## Contents

- [What the application does](#what-the-application-does)
- [Current system architecture](#current-system-architecture)
- [Technology stack](#technology-stack)
- [How the system works](#how-the-system-works)
- [Prerequisites](#prerequisites)
- [Configuration](#configuration)
- [Run locally](#run-locally)
- [Run with Docker](#run-with-docker)
- [Testing and builds](#testing-and-builds)
- [Deployment and infrastructure](#deployment-and-infrastructure)
- [Data, persistence, and security](#data-persistence-and-security)
- [API overview](#api-overview)
- [Troubleshooting](#troubleshooting)
- [Known gaps](#known-gaps)

## What the application does

Ops Monitor provides:

- A dashboard for Snowflake task status, data-quality (DQ) results, KPIs, and date/status filtering.
- Optional AWS telemetry, with the implemented monitoring path focused on AWS Glue job runs.
- Root-cause analysis using task history, DQ evidence, and upstream/downstream lineage.
- A workbench that presents RCA evidence, a lineage graph, and suggested before/after fixes.
- A knowledge base for reusable RCA guidance.
- A chat interface (“AMQ Assist”) that routes requests to monitoring, RCA, and fix workflows.
- Connection management for Snowflake and AWS.
- SQL guardrails, an audit log, and an API-based approval flow for potentially risky SQL.

The application currently supports **RCA and fix suggestions**. It does **not** contain the previously documented Test Agent, `/api/validate` endpoint, or zero-copy-clone validation workflow.

## Current system architecture

### Local development

```text
Browser
  |
  | http://localhost:5173
  v
React + Vite development server
  |
  | /api/* proxy
  v
FastAPI / Uvicorn on http://localhost:8001
  |
  +-- Snowflake: task history, DQ data, metadata, and lineage
  +-- AWS: Glue telemetry and AWS connectivity checks (optional)
  +-- Anthropic/Claude: enhanced reasoning and chat (optional)
  +-- Local JSON: settings, agent memory, incidents, and SQL audit
```

### Container and production design

The production image is a **single container**:

1. Node.js builds the React application into `frontend/dist`.
2. Python runs FastAPI/Uvicorn on port `8001`.
3. FastAPI serves both `/api/*` and the compiled React single-page application.

There is no separately deployed frontend in the checked-in design. The frontend and backend are served by the same container and the same hostname.

```text
Browser
  |
  v
Load balancer / container endpoint
  |
  v
Ops Monitor container :8001
  +-- React static files
  +-- FastAPI /api/*
```

## Technology stack

### Backend

- Python 3.12
- FastAPI 0.115.0
- Uvicorn 0.30.6
- Pydantic 2.9.2
- Snowflake Connector for Python 4.6.0
- boto3 1.35.24
- Anthropic Python SDK 0.116.0 or newer
- File-based JSON persistence

See `backend/requirements.txt` for the authoritative dependency list.

### Frontend

- React 18.3
- React DOM 18.3
- React Markdown 9
- Vite 5.4
- Plain CSS and browser storage for UI preferences

See `frontend/package.json` and `frontend/package-lock.json`.

### Packaging and hosting

- Multi-stage Docker build using Node 20 Alpine and Python 3.12 Slim
- Docker Compose for a local production-style run
- AWS ECS/Fargate task-definition template
- Amazon ECR as the documented container registry
- AWS Application Load Balancer (ALB) in the documented target architecture
- AWS Secrets Manager for the Anthropic key
- CloudWatch Logs for ECS logs
- Optional EFS mounts for durable application state
- Optional Cloudflare quick tunnel for temporary sharing

## How the system works

### 1. Startup

- FastAPI creates the agent orchestrator.
- A background thread warms the shared Snowflake connection.
- If `frontend/dist` exists, FastAPI mounts the compiled frontend and provides SPA fallback routing.
- Claude is enabled only when a usable `ANTHROPIC_API_KEY` is available; otherwise agents use deterministic fallbacks.

### 2. Connection setup

In the UI, open **Settings** and configure:

- Snowflake using password, browser SSO, or key-pair authentication.
- AWS credentials and region if AWS monitoring is needed.
- Task and DQ monitoring targets.

Settings are saved server-side to `backend/app/data/connection_settings.json`. Secret values are masked when settings are read through the API.

### 3. Monitoring

- `GET /api/summary` collects live Snowflake and optional AWS telemetry.
- The monitoring layer accepts only records marked as live; no mock fallback is used.
- Failed and delayed records are added to the incident log with in-process deduplication.
- `GET /api/dq/summary` reads the configured DQ summary table.
- Operators can request live DQ revalidation from the dashboard.

### 4. RCA and fix suggestion

1. The operator selects **Run RCA** for a task or DQ failure.
2. `POST /api/rca` collects evidence, inspects lineage, checks prior knowledge/cache, and optionally asks Claude to improve the explanation.
3. The Workbench displays root cause, evidence, impact, and lineage.
4. `POST /api/fix` produces a grounded before/after suggestion and can incorporate Workbench RCA context.
5. The operator reviews the suggestion. The current application does not automatically apply or validate it.

`POST /api/remediate/{pipeline_id}` performs the available automated sequence: **RCA → fix suggestion**.

### 5. Chat

The chat agent classifies the operator’s intent and routes it to monitoring, RCA, fix, or general assistance. `/api/chat/stream` streams responses using Server-Sent Events (SSE). Chat sessions are held in process and are lost when the process restarts.

### 6. Agent memory and knowledge

- Per-agent JSON memory is stored under `backend/app/memory/store/`.
- Incidents are stored in `incident_log.json`.
- RCA knowledge is stored in `backend/app/skills/rca_knowledge.md`.
- SQL decisions are recorded in `backend/app/data/sql_audit_log.json`.

## Prerequisites

Choose either native development tools or Docker.

### Native development

- Python 3.12
- Node.js 20 recommended
- npm
- PowerShell commands are shown below; equivalent commands work on macOS/Linux.
- Network access to configured Snowflake, AWS, and Anthropic endpoints.

### Container run

- Docker Desktop or another Docker Engine with Compose support.

### Optional deployment tools

- AWS CLI authenticated to the target AWS account.
- Permission to use ECR, ECS, IAM, Secrets Manager, CloudWatch Logs, networking, and optionally EFS.
- `cloudflared` only when using the temporary public tunnel script.

## Configuration

### Environment file

From the repository root, copy the example file:

```powershell
Copy-Item .env.example .env
```

Do not commit `.env`. The backend loads the root `.env` automatically.

### LLM settings

```dotenv
ANTHROPIC_API_KEY=
ANTHROPIC_BASE_URL=https://chat.int.bayer.com/anthropic
CLAUDE_MODEL=claude-sonnet-4.5
```

- `ANTHROPIC_API_KEY` is optional. Without it, deterministic agent fallbacks remain available.
- `ANTHROPIC_BASE_URL` is required when using a key issued for the configured corporate gateway.
- `CLAUDE_MODEL` selects the model.

The application currently initializes the Anthropic SDK with the API key and relies on SDK environment handling for `ANTHROPIC_BASE_URL`.

### Monitoring settings

The following environment variables provide defaults. Most connection and monitoring values can also be updated in the Settings UI.

```dotenv
DQ_TABLE_FQN=CPH_DB_PRE_PROD.MODEL_V2.DQM_VALIDATION_SUMMARY
DQ_SUBJECT_AREA=Lynkuet LAAD
DQ_RULES_TABLE_FQN=CPH_DB_PROD.MODEL_V2.CONFIG_LYNKUET
TASK_MONITOR_DATABASE=CPH_DB_PROD
TASK_NAME_PATTERN=TASK%
TASK_MONITOR_WAREHOUSE=PROD_ETL_DAYLIGHT_WH_XL
TASK_TELEMETRY_STATEMENT_TIMEOUT_S=180
```

Additional optional backend settings include:

- `TASK_HISTORICAL_MONTHS`
- `TASK_FUTURE_DAYS`
- `LINEAGE_SOURCE_SCHEMAS`
- `LINEAGE_SOURCE_TABLE_PREFIXES`
- `LINEAGE_SOURCE_TAG`
- `LINEAGE_SOURCE_TAG_VALUES`
- `LINEAGE_MAX_DEPTH`
- `LINEAGE_MAX_LOOKUPS`
- `LLM_CACHE_CONTROL`
- `PORT`
- `OPS_MONITOR_RELOAD`

AWS and Snowflake credentials are normally entered through the UI and are not defined in `.env.example`.

## Run locally

### 1. Set up and start the backend

From the repository root:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8001
```

Backend endpoints:

- API: `http://localhost:8001`
- Health: `http://localhost:8001/api/health`
- Interactive OpenAPI docs: `http://localhost:8001/docs`

### 2. Set up and start the frontend

Open a second terminal at the repository root:

```powershell
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`.

Vite proxies `/api` calls to `http://localhost:8001`, so no frontend API URL configuration is required for normal local development.

### 3. Configure a live connection

1. Open **Settings**.
2. Enter Snowflake connection details.
3. Optionally enter AWS connection details.
4. Test connectivity.
5. Save the settings.
6. Return to the Dashboard and refresh.

Without a working live connection, the dashboard may be empty. That is expected.

### Quick health check

```powershell
Invoke-RestMethod http://localhost:8001/api/health | ConvertTo-Json -Depth 5
Invoke-WebRequest http://localhost:5173 -UseBasicParsing | Select-Object StatusCode
```

## Run with Docker

Docker runs the same single-container shape intended for ECS:

```powershell
docker compose up --build
```

Open `http://localhost:8001`.

Compose creates named volumes for:

- `/app/backend/app/data`
- `/app/backend/app/memory/store`

Stop the application:

```powershell
docker compose down
```

To also delete the local persisted settings and memory volumes:

```powershell
docker compose down --volumes
```

## Testing and builds

### Backend tests

Tests use pytest, but pytest is not currently pinned in `backend/requirements.txt`. Install it in the development environment before running tests:

```powershell
cd backend
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m pytest tests
```

Most tests are unit tests and do not require live Snowflake/AWS access. End-to-end behavior against external systems must be verified separately.

### Frontend production build

```powershell
cd frontend
npm ci
npm run build
```

The output is written to `frontend/dist`.

### Available npm commands

- `npm run dev` — start Vite development server.
- `npm run build` — create the production bundle.
- `npm run preview` — preview the built frontend.

There are currently no repository-defined lint/format commands and no checked-in CI/CD workflow.

## Deployment and infrastructure

### What is confirmed by this repository

The repository contains:

- A production `Dockerfile`.
- `docker-compose.yml` for local image validation.
- `deploy/ecs/task-definition.json` for AWS ECS/Fargate.
- `deploy/ecs/secret.example.json` for the Anthropic secret shape.
- Documentation for an intended AWS ECS/Fargate deployment.
- `scripts/deploy-public.ps1` for a local single-port run and optional Cloudflare quick tunnel.

### Where the frontend and backend are deployed

In the checked-in container/ECS design:

- **Frontend:** compiled into `frontend/dist` and served by FastAPI.
- **Backend:** FastAPI/Uvicorn in the same container.
- **Port:** `8001`.
- **Public entry point:** expected to be an ALB DNS name or another reverse proxy/tunnel URL.

The repository does **not** contain a verified live production URL, AWS account/region, running ECS service identifier, or other evidence that identifies a currently active deployment. Therefore, this README cannot truthfully name a live host. Confirm the active environment in the AWS console or with the owning platform team.

### Infrastructure services already represented

The current deployment design uses or anticipates:

- **Amazon ECR** — container image registry.
- **Amazon ECS on Fargate** — container runtime.
- **Application Load Balancer** — HTTP entry point and health routing.
- **AWS IAM** — task execution and application task roles.
- **AWS Secrets Manager** — Anthropic API key injection.
- **Amazon CloudWatch Logs** — container logs, using `/ecs/ops-monitor-prod` in the task template.
- **Amazon EFS (recommended, not provisioned here)** — durable settings and memory.
- **VPC and subnets** — ALB and Fargate task networking.
- **Snowflake** — external operational data source, DQ source, and lineage source.
- **AWS Glue** — optional monitored workload.
- **AWS STS, Step Functions, CloudWatch Logs, and DynamoDB clients** — used by AWS connectivity/integration code.
- **Anthropic-compatible gateway** — optional LLM provider.
- **Cloudflare Tunnel** — optional temporary external access; not the primary production design.

### Important deployment limitation

`deploy/ecs/README.md` references these files, but they are not present in the repository:

- `deploy/ecs/cloudformation.yml`
- `deploy/ecs/deploy.ps1`
- `deploy/ecs/deploy.sh`

As a result, the documented CloudFormation deployment is **not currently runnable end-to-end from this checkout**. Do not run those referenced commands until the missing files are restored or recreated.

### Manual ECS deployment using the available task definition

The available task definition can be used only after infrastructure has been created separately:

1. Build the image.
2. Push it to ECR.
3. Replace every placeholder in `deploy/ecs/task-definition.json`, including:
   - `ACCOUNT_ID`
   - `REGION`
   - Image URI
   - Execution role ARN
   - Task role ARN
   - Secrets Manager ARN
4. Ensure the CloudWatch log group exists.
5. Register the task definition.
6. Update an existing ECS service.

Example image build:

```powershell
docker build -t ops-monitor:latest .
```

Example registration and rollout after all placeholders are replaced:

```powershell
aws ecs register-task-definition --cli-input-json file://deploy/ecs/task-definition.json
aws ecs update-service `
  --cluster ops-monitor-prod `
  --service ops-monitor-prod `
  --task-definition ops-monitor `
  --force-new-deployment
```

This assumes the ECR repository, roles, VPC, subnets, security groups, load balancer, target group, ECS cluster/service, log group, and secret already exist.

### ECS runtime configuration

The task definition template uses:

- Fargate with `awsvpc` networking.
- `512` CPU units and `1024` MiB memory.
- Container port `8001`.
- Health check: `GET /api/health`.
- CloudWatch Logs with the `awslogs` driver.
- Anthropic key from Secrets Manager.

For persistent settings and agent memory, mount EFS at:

```text
/app/backend/app/data
/app/backend/app/memory/store
```

Without persistent storage, replacing a Fargate task removes locally saved connection settings, audit data, and agent memory.

### Temporary LAN/public sharing

The PowerShell helper builds the frontend and starts the unified application:

```powershell
.\scripts\deploy-public.ps1
```

To request an ephemeral Cloudflare URL:

```powershell
.\scripts\deploy-public.ps1 -Tunnel
```

This is intended for temporary sharing, not a production deployment.

## Data, persistence, and security

### Local state

Ops Monitor does not use an application database. It persists state in files:

- `backend/app/data/connection_settings.json`
- `backend/app/data/sql_audit_log.json`
- `backend/app/memory/store/*.json`
- `backend/app/skills/rca_knowledge.md`

The first three locations should be treated as runtime data and backed up or mounted on durable storage where needed.

### Caches

The application uses short-lived in-process/file caches for monitoring, AWS telemetry, RCA, and DQ revalidation. Cache contents are performance aids, not a source of record.

### Authentication and exposure

The FastAPI application currently has:

- No application-level login, JWT, or API-key middleware.
- Permissive CORS (`*`).
- Server-side storage of configured AWS and Snowflake credentials.

Do not expose the service directly to the public internet. Production deployments should put it behind organization-approved authentication and network controls, such as a private network/VPN, authenticated reverse proxy, or ALB-integrated access layer. Restrict security groups and IAM permissions to the minimum required.

### Secrets

- Never commit `.env` or `connection_settings.json`.
- Prefer Secrets Manager or another approved secret store in deployed environments.
- Rotate any key that has been copied into documentation, logs, tickets, or source control.
- Snowflake key-pair material and passwords saved through Settings are sensitive.

### SQL guardrails

The backend includes SQL classification, audit, and approval endpoints. These controls reduce risk but do not replace least-privilege Snowflake roles. Run the application with a role limited to the required databases, schemas, views, and safe operations.

## API overview

Interactive and authoritative API documentation is available at `/docs` while the backend is running.

### Health and connectivity

- `GET /api/health`
- `GET /api/connectivity`
- `POST /api/connectivity`
- `GET /api/snowflake/warmup`
- `GET /api/snowflake/session`
- `GET /api/settings`
- `POST /api/settings`

### Monitoring and details

- `GET /api/summary`
- `GET /api/dq/summary`
- `GET /api/dq/details/{qc_id}`
- `GET /api/pipelines/{pipeline_id}/logs`

### RCA, fixes, and knowledge

- `POST /api/rca`
- `POST /api/fix`
- `POST /api/remediate/{pipeline_id}`
- `GET /api/rca/knowledge`
- `POST /api/rca/knowledge`

### Chat and memory

- `POST /api/chat`
- `POST /api/chat/stream`
- `POST /api/chat/activity-error`
- `GET /api/incidents`
- `POST /api/issues`
- `GET /api/memory/{agent}`

### SQL guardrail

The backend also exposes `/api/guardrail/*` endpoints for SQL audit and approval operations. Use `/docs` for their current request/response schemas.

There is no `/api/validate` endpoint in the current implementation.

## Repository structure

```text
ops-monitor/
├── backend/
│   ├── app/
│   │   ├── agents/          # Connectivity, monitoring, RCA, fix, and chat agents
│   │   ├── connectors/      # Snowflake, DQ, lineage, AWS, and SQL guardrail
│   │   ├── core/            # Configuration and settings
│   │   ├── data/            # Runtime settings and SQL audit files
│   │   ├── memory/          # JSON memory and chat session handling
│   │   ├── models/          # API schemas
│   │   ├── skills/          # Agent instructions and RCA knowledge
│   │   └── main.py          # FastAPI application and routes
│   ├── tests/
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── api/
│   │   ├── components/
│   │   └── App.jsx
│   └── package.json
├── deploy/ecs/              # ECS notes, task definition, secret examples
├── scripts/                 # Local/public launch helper
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

## Troubleshooting

### Dashboard is empty

- This application is live-only; there is no mock fallback.
- Open Settings and verify Snowflake connectivity.
- Check monitoring database/table names, warehouse access, and role permissions.
- Inspect `GET /api/health` and `GET /api/connectivity`.

### Port 8001 is already in use

```powershell
Get-NetTCPConnection -LocalPort 8001 |
  Select-Object -ExpandProperty OwningProcess -Unique |
  ForEach-Object { Stop-Process -Id $_ -Force }
```

### Claude is unavailable

```powershell
(Invoke-RestMethod http://localhost:8001/api/health).llm | ConvertTo-Json
```

Check:

- `ANTHROPIC_API_KEY` exists in the root `.env`.
- The key has not expired.
- `ANTHROPIC_BASE_URL` is correct for the key issuer.
- Corporate VPN/proxy access is available.
- The backend was restarted after changing `.env`.

The core workflows still provide deterministic responses when Claude is unavailable.

### Snowflake SSO fails

Browser SSO requires a browser session on the machine running the backend and may not be suitable for headless containers. Use an approved password or key-pair service identity for deployed workloads.

### Settings disappear after a container restart

The container filesystem is ephemeral unless a Docker volume or EFS mount is attached to the data and memory paths documented above.

### ECS task cannot start

Verify:

- The image URI and region are correct.
- Execution and task roles exist.
- The task can reach ECR, Secrets Manager, Snowflake, and the LLM gateway.
- The CloudWatch log group exists.
- The secret ARN and JSON key are correct.
- Port `8001` is allowed from the load balancer security group.

## Known gaps

- No checked-in CI/CD workflow.
- No checked-in lint or formatting configuration.
- `pytest` is not included in `backend/requirements.txt`.
- Referenced CloudFormation and deployment scripts are missing.
- No application-level authentication.
- Runtime settings and memory are file-based rather than database-backed.
- No verified production hostname or active deployment metadata is stored in the repository.
- AWS telemetry is not equally complete for every probed AWS service; Glue is the primary implemented monitoring path.
- The Test Agent, `/api/validate`, and zero-copy validation flow are not part of the current codebase.
