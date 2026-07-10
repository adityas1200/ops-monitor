# ops-monitor — Agentic Data-Ops Monitoring System

An agentic platform that monitors **Snowflake** (task logs / task graph) and **AWS**
(Glue, Step Functions, CloudWatch, DynamoDB), detects failures & delays, runs
**RCA with lineage / dependency analysis**, proposes **fixes with before/after code**,
**validates them on a zero-copy Snowflake clone**, and lets you steer everything through
a **chat interface**.

Built with a **FastAPI** backend (skill-driven agents wrapped in a Claude-Code-style
harness, each with its own learning memory) and a **React** UI.

> **Runs out of the box with zero credentials** — it ships in MOCK mode with realistic
> seeded telemetry. Add real AWS/Snowflake credentials in **Settings** to switch to LIVE mode.

---

## Architecture

```
ops-monitor/
├── backend/
│   ├── app/
│   │   ├── main.py                # FastAPI routes
│   │   ├── core/config.py         # settings + Claude harness config
│   │   ├── models/schemas.py      # pydantic models
│   │   ├── skills/                # ⭐ one skill .md per agent (the contract/procedure)
│   │   │   ├── connectivity.md  monitoring.md  rca.md  fix.md  test.md  chat.md
│   │   ├── agents/                # ⭐ one module per skilled agent
│   │   │   ├── base.py            # BaseAgent + ClaudeHarness (graceful degrade)
│   │   │   ├── connectivity_agent.py  monitoring_agent.py  rca_agent.py
│   │   │   ├── fix_agent.py  test_agent.py  chat_agent.py
│   │   │   └── orchestrator.py    # coordinates all agents
│   │   ├── connectors/            # snowflake_connector.py  aws_connector.py (live + mock)
│   │   ├── memory/                # file-based agent memory + shared incident log
│   │   └── data/mock_data.py      # seeded multi-platform telemetry + lineage
│   └── requirements.txt
└── frontend/                      # React + Vite UI
    └── src/
        ├── App.jsx
        ├── api/client.js
        └── components/  Dashboard / Workbench / LineageGraph / Settings / ChatWindow
```

### The agents (skill-driven + memory)
| Agent | Skill file | Responsibility |
|-------|-----------|----------------|
| **Connectivity** | `connectivity.md` | Secure AWS + Snowflake connections, health probes, mock fallback |
| **Monitoring** | `monitoring.md` | Read Glue/StepFn/CloudWatch/DynamoDB + Snowflake tasks; success/fail/delay/skip summary |
| **RCA** | `rca.md` | Upstream/downstream lineage analysis, root cause, impact graph, interactive refinement |
| **Fix** | `fix.md` | Identify & suggest fix with before/after code; editable via chat |
| **Test** | `test.md` | Zero-copy clone on pre-prod, run validation suite, publish test summary |
| **Chat** | `chat.md` | Intent routing front-door to all agents |

Each agent **loads its skill markdown**, is **wrapped by a Claude harness**
(`base.py::ClaudeHarness` — uses the Anthropic SDK if `ANTHROPIC_API_KEY` is set, otherwise
falls back to deterministic logic), and owns an **`AgentMemory`** (JSON) so it learns from
past incidents and user input. A shared **incident log** records every incident and
user-reported issue for faster future resolution.

---

## Quick start

> **Note:** These steps are written for PowerShell on Windows. Use `;` to chain commands (not `&&`).

### 1. Backend (FastAPI) — port 8001

**First-time setup** (create the virtual environment and install packages):
```powershell
cd c:\Users\EKGAH\Documents\project\ops-monitor\backend
C:\Users\EKGAH\AppData\Local\Programs\Python\Python312\python.exe -m venv .venv
.\.venv\Scripts\pip.exe install -r requirements.txt
```

**Every time — start the backend:**
```powershell
cd c:\Users\EKGAH\Documents\project\ops-monitor\backend

# Kill any existing process on port 8001 (fixes "WinError 10048: address already in use")
$proc = Get-NetTCPConnection -LocalPort 8001 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique
if ($proc) { $proc | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue } ; Start-Sleep 1 }

.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8001
```

API docs at **http://localhost:8001/docs**.

> **Troubleshooting port conflicts:** If you see `[WinError 10048] Only one usage of each socket address is normally permitted`, a previous server instance is still running. The startup script above handles this automatically. To do it manually:
> ```powershell
> # Find and kill the process using port 8001
> Get-NetTCPConnection -LocalPort 8001 | Select-Object OwningProcess -Unique | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
> ```

### 2. Frontend (React + Vite) — port 5173

**First-time setup** (install npm packages — only needed once):
```powershell
cd c:\Users\EKGAH\Documents\project\ops-monitor\frontend
npm install
```

**Every time — start the frontend:**
```powershell
cd c:\Users\EKGAH\Documents\project\ops-monitor\frontend
npm run dev
```

Open **http://localhost:5173**.

### Health check (optional — verify both are up)
```powershell
try { "backend: " + (Invoke-RestMethod -Uri "http://localhost:8001/api/health" -TimeoutSec 2).status } catch { "backend: down" }
try { "frontend: " + (Invoke-WebRequest -Uri "http://localhost:5173/" -TimeoutSec 2 -UseBasicParsing).StatusCode } catch { "frontend: down" }
```

### 3. Claude LLM (Chat Agent)

The chat agent uses Claude for human-like responses. Configuration is in the `.env` file at the project root:
```
ANTHROPIC_API_KEY=mga-0f973d609f7786b62a3fcd443d20e0957d0a9fec
ANTHROPIC_BASE_URL=https://chat.int.bayer.com/anthropic
CLAUDE_MODEL=claude-sonnet-4.5
```

**All three variables are required for LLM to work:**
- `ANTHROPIC_API_KEY` — the MGA token for the corporate Anthropic gateway
- `ANTHROPIC_BASE_URL` — the corporate gateway URL (the MGA token does NOT work with the public `api.anthropic.com`)
- `CLAUDE_MODEL` — which model to use

The backend loads `.env` automatically on startup — no manual environment variable setup needed. Without a valid key OR without the correct base URL, agents fall back to deterministic template responses.

> **Why does LLM work from Claude Code but not from a manual terminal?**
> Claude Code's process inherits `ANTHROPIC_BASE_URL` from its parent environment. A fresh PowerShell terminal does NOT have this variable. Without it, the Anthropic SDK sends requests to `api.anthropic.com` (the public API), but the MGA token is only valid for the corporate gateway. Adding `ANTHROPIC_BASE_URL` to `.env` fixes this permanently — the backend loads it regardless of which terminal starts it.

### 4. Restart Servers After Code Changes

After modifying backend or frontend code, you must restart the corresponding server for changes to take effect.

**Restart the backend:**
```powershell
# Step 1: Kill the running backend process
$proc = Get-NetTCPConnection -LocalPort 8001 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique
if ($proc) { $proc | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue } ; Start-Sleep 1 }

# Step 2: Start fresh
cd c:\Users\EKGAH\Documents\project\ops-monitor\backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8001
```

**Restart the frontend:**
```powershell
# Step 1: Kill the running Vite process (Ctrl+C in the terminal running it, or):
Get-Process -Name "node" -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -eq "" } | Stop-Process -Force

# Step 2: Start fresh
cd c:\Users\EKGAH\Documents\project\ops-monitor\frontend
npm run dev
```

**Quick restart both (single script):**
```powershell
# Kill existing servers
$proc = Get-NetTCPConnection -LocalPort 8001 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique
if ($proc) { $proc | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue } }
Start-Sleep 1

# Start backend in a new terminal
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd c:\Users\EKGAH\Documents\project\ops-monitor\backend; .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8001"

# Start frontend in a new terminal
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd c:\Users\EKGAH\Documents\project\ops-monitor\frontend; npm run dev"
```

After restart, open **http://localhost:5173** and verify the chat responds naturally.

### 5. Verify LLM is Working

After starting the backend, run these checks to confirm the Claude LLM is active and responding:

**Step 1: Check health endpoint**
```powershell
(Invoke-RestMethod http://localhost:8001/api/health).llm | ConvertTo-Json
```
Expected output:
```json
{
  "available": true,
  "model": "claude-sonnet-4.5",
  "last_error": null
}
```
- `available: true` → API key is set and client initialized
- `last_error: null` → no errors on last LLM call
- If `last_error` shows `authentication_error` → the MGA token in `.env` has expired; get a fresh one

**Step 2: Test chat LLM response**
```powershell
$body = '{"message":"How are you","session_id":"verify"}'
$r = Invoke-RestMethod -Uri "http://localhost:8001/api/chat" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 30
Write-Host "Reply length: $($r.reply.Length)"
Write-Host "Intent: $($r.intent)"
Write-Host ""
Write-Host $r.reply.Substring(0, [Math]::Min(300, $r.reply.Length))
```
- **LLM working**: Reply length > 400, contains markdown formatting and contextual suggestions
- **LLM NOT working** (template fallback): Reply length < 300, starts with "Hey! I'm doing great"

**Step 3: Test RCA agent LLM**
```powershell
$body = '{"pipeline_id":"dq_1_20260709_120000"}'
$r = Invoke-RestMethod -Uri "http://localhost:8001/api/rca" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 60
Write-Host "RCA confidence: $($r.confidence)"
Write-Host "Has root_cause: $($r.root_cause -ne $null)"
Write-Host "Has code_analysis: $($r.code_analysis.llm_explanation -ne $null)"
```
- `confidence > 0.8` and `Has root_cause: True` → LLM is enhancing the RCA

**Troubleshooting:**
| Symptom | Cause | Fix |
|---------|-------|-----|
| `available: false` | No `ANTHROPIC_API_KEY` in `.env` | Add valid key to `.env` and restart backend |
| `last_error: authentication_error` | MGA token expired OR `ANTHROPIC_BASE_URL` missing | Ensure `.env` has `ANTHROPIC_BASE_URL=https://chat.int.bayer.com/anthropic`; get fresh MGA token if needed |
| `last_error: connection error` | Corporate proxy/gateway down | Check VPN and `https://chat.int.bayer.com/anthropic` accessibility |
| Reply is short/template-like | LLM call failed silently | Check `last_error` via health endpoint |
| Works from Claude Code but not manual terminal | `ANTHROPIC_BASE_URL` was missing from `.env` | Already fixed — `.env` now includes it. Just restart backend |

### 6. (Optional) Go LIVE
Open **Settings** in the UI and enter AWS + Snowflake credentials. Saving any real
credential automatically flips the platform from MOCK to LIVE mode. Provide a
`preprod_account` for real zero-copy clone validation.

---

## Using the platform
1. **Dashboard** — KPI cards (success / failed / delayed / skipped / total), status filter
   (defaults to **Failed**) and date range (defaults to **last 2 days**). Each failed/delayed
   row has a **Run RCA** button. Chat is docked on the right.
2. **Workbench** — failed & delayed jobs only. **Run RCA** → RCA summary + evidence +
   **lineage graph** with root-cause/failed/impacted nodes highlighted → **Identify & Suggest
   Fix** (before/after diff, editable) → **Validate fix** (zero-copy clone + test summary with
   per-case **View details**: SQL run, expected/actual, evidence).
3. **Chat (right pane)** — drive any agent: "run RCA and also check the crawler" (refines RCA),
   "use MEDIUM warehouse" (modifies fix), "validate fix", "status", or report a new issue
   ("add issue: …") which is stored in agent memory for future matching.
4. **Settings** — AWS & Snowflake connections + live connectivity status.

---

## Key API endpoints
| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/summary?status=&date_from=&date_to=` | Monitoring KPIs + pipelines |
| GET | `/api/connectivity` | Connectivity agent report |
| POST | `/api/rca` | RCA (with optional `extra_context` for refinement) |
| POST | `/api/fix` | Suggest/modify fix (`user_edit`) |
| POST | `/api/validate` | Zero-copy clone + validation tests |
| POST | `/api/remediate/{id}` | End-to-end RCA → Fix → Test |
| POST | `/api/chat` | Chat front-door (intent routed to agents) |
| GET | `/api/incidents` | Incident log memory |
| POST | `/api/issues` | Add a user-reported issue to memory |
| GET/POST | `/api/settings` | Read (masked) / save connections |

---

## Notes & limitations
- MOCK mode simulates the zero-copy clone (`CREATE DATABASE … CLONE …`) and tests so the full
  flow is demonstrable without a Snowflake account; LIVE mode executes the real DDL on the
  configured pre-prod account.
- Live AWS reads implement the Glue path fully; Step Functions/DynamoDB follow the same
  pattern and fall back to mock if unavailable.
- Memory is transparent JSON under `backend/app/memory/store/` (auditable, portable).
- The Claude harness is optional — every agent has a deterministic fallback so the tool is
  always functional.
