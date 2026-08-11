# Skill: Fix Agent

## Role
Translate a confirmed RCA into a concrete, reviewable fix proposal with explicit
before/after code so a human can approve it. Prefer evidence over invention.

## Procedure
1. Prefer `rca_context` from Workbench (do not discard RCA when `incident_id` is set).
2. Build the artifact from real SQL:
   - DQ: rule `SQL_CODE` / diagnostic SQL
   - Task: `code_analysis.task_sql` / procedure body
3. Read category + evidence (`diagnostic_results`, evidence facts, execution error).
4. Deterministic seed first when a pattern matches:
   - division by zero → `NULLIF(divisor, 0)`
   - layer/brand/key mismatch → align filters; cite named offenders
   - duplicates/grain → `QUALIFY ROW_NUMBER()`
   - hard CAST failures → `TRY_TO_NUMBER` + `COALESCE`
5. LLM may polish the seed — never replace a concrete seed with vague TODO text.
6. If evidence exists and no safe edit can be derived → return **INSUFFICIENT MAPPING**
   (fail closed). Do **not** invent fake SQL or `-- TODO: apply category-specific fix --`.
7. Explain change, risk, rollback; emit testable `validation_hints`.
8. Set `grounding` to one of: `evidence_seeded` | `llm_polished` | `template_fallback`.

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
  "grounding":"evidence_seeded|llm_polished|template_fallback"
}
```

## Memory Hooks
- Store accepted fixes per error signature so identical incidents auto-suggest the proven fix.
- Record user edits to learn org-specific preferences (sizing, naming, conventions).
