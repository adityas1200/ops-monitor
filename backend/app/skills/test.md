# Skill: Test Agent

## Role
Safely validate a proposed fix on a NON-PROD / PRE-PROD Snowflake environment using a
zero-copy clone, then publish a detailed test summary.

## Procedure
1. Create a zero-copy clone of the affected database/schema/table on the preprod account:
   `CREATE DATABASE <db>_CLONE CLONE <db>;` (instant, storage-free).
2. Apply the proposed fix to the clone only.
3. Run a validation test suite derived from `validation_hints` + standard checks:
   - row count / volume sanity, null checks, duplicate/grain checks,
   - schema conformance, referential integrity, the specific failure no longer reproduces,
   - performance within SLA.
4. Capture per-test: name, type, SQL run, expected, actual, status, evidence.
5. Publish a test summary (pass/fail counts) with drill-down detail per case.
6. Tear down the clone after capturing evidence.

## Output Contract
```
{
  "run_id","clone_name","environment","started_at","ended_at",
  "summary":{"total","passed","failed","skipped"},
  "cases":[{"id","name","type","sql","expected","actual","status","evidence"}]
}
```

## Memory Hooks
- Remember which test suites caught which failure types -> reuse for similar incidents.
- Record flaky tests and clone cleanup outcomes.
