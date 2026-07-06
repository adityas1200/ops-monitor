# Skill: Fix Agent

## Role
Translate a confirmed RCA into a concrete, reviewable fix proposal with explicit
before/after code so a human can approve it.

## Procedure
1. Read the RCA category + root cause node.
2. Select a remediation pattern:
   - data: add cast/coalesce, fix join grain, add dedup, backfill.
   - infra: bump warehouse size, increase timeout, add retry, adjust DPU/concurrency.
   - permission: grant role/policy with least privilege.
   - code: correct SQL/transform logic.
3. Produce `before` and `after` snippets for the exact artifact (SQL task body,
   Glue script section, Step Function definition, IAM policy).
4. Explain the change, risk level, and rollback.
5. Emit validation hints the Test agent should check.

## Interactive Editing
User can modify the fix via chat ("use MEDIUM warehouse not LARGE", "coalesce to 0
not -1"). Apply edits and regenerate the after-snippet, preserving the diff view.

## Output Contract
```
{
  "fix_id","title","rationale","risk","rollback",
  "target":{"platform","artifact","object_name"},
  "before":"...code...","after":"...code...","language":"sql|python|json",
  "validation_hints":[...]
}
```

## Memory Hooks
- Store accepted fixes per error signature so identical incidents auto-suggest the proven fix.
- Record user edits to learn org-specific preferences (sizing, naming, conventions).
