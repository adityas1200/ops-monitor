# Skill: Fix Agent

## Role
Translate a confirmed RCA into a concrete, reviewable fix proposal with explicit
before/after code so a human can approve it. Prefer RCA remediation over invention
or category templates.

## Procedure
1. Prefer `rca_context` from Workbench (do not discard RCA when `incident_id` is set).
2. Build the artifact from **real** SQL only:
   - DQ: prefer lineage `procedure_chain[].sql_body` (upstream load procedure) when present
   - RCA resolves writer procedures from failing tables even when no related task failed
   - Else DQ rule `SQL_CODE` / diagnostic SQL (before = current failing SQL)
   - Task: `code_analysis.task_sql` / procedure body
   - If none resolve → emit **Source SQL unavailable** (fail closed). Never invent ETL.
3. Read RCA `remediation` (`immediate_fix`, `permanent_fix`, `monitoring_recommendation`),
   `root_cause` (explanation / expected / actual), `summary`, and evidence facts.
4. **Seed from RCA only** — do **not** hardcode category SQL templates such as:
   - always `CREATE OR REPLACE` + `ROW_NUMBER` for duplicates
   - always `NULLIF` for division patterns
   - always warehouse resize for infra
   - always "Investigate trend" SELECT for WoW / `CURR_CNT` / `PREV_CNT`
5. If RCA remediation already embeds SQL (fenced / `{{code:…}}` / QUALIFY clause), use that
   as `after`. Otherwise leave `after` for the LLM to translate RCA into runnable SQL.
6. LLM must follow RCA remediation / root cause — never substitute a generic template
   that contradicts the RCA (e.g. table rewrite when RCA says market expansion /
   not data corruption).
7. **`before` / `after` are SQL only** — never append RCA Immediate/Permanent narrative,
   `-- RCA-guided proposal` footers, or `-- LLM polish discarded` notes into `after`.
   Put explanation in `rationale` / `validation_hints` only.
8. **Object allowlist (hard guardrail):**
   - `before`/`after` may only reference objects present in the artifact SQL,
     `procedure_chain`, `tables_inspected`, or diagnostic SQL.
   - Discard LLM polish that invents schemas/tables (e.g. `STAGING.RX_CLAIMS_RAW`)
     or brand-new `INSERT`/`MERGE` ETL when the seed was DQ-rule / unavailable.
9. If evidence exists and no safe SQL edit can be derived → leave `after` empty and explain
   in `rationale` (`rca_guided`). Do **not** invent fake SQL or comment dumps.
10. Explain change, risk, rollback; emit testable `validation_hints`.
11. Set `grounding` to one of: `rca_guided` | `llm_polished` | `template_fallback`.

## Interactive Editing
User can modify the fix ("use MEDIUM warehouse not LARGE", "coalesce to 0
not -1"). Apply edits and regenerate the after-snippet, preserving the diff view.

## Output Contract
```
{
  "fix_id","title","rationale","risk","rollback",
  "target":{"platform","artifact","object_name"},
  "before":"...code...","after":"...code...","language":"sql|python|json",
  "validation_hints":["testable check 1", "..."],
  "evidence_summary":"...",
  "evidence_facts":["..."],
  "grounding":"rca_guided|llm_polished|template_fallback"
}
```

## Memory Hooks
- Store accepted fixes per error signature so identical incidents auto-suggest the proven fix.
- Record user edits to learn org-specific preferences (sizing, naming, conventions).
