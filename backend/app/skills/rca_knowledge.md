# RCA Knowledge Base (User-contributed)

Rules added by operations team. The RCA agent consults these before running analysis.
When a rule matches the current failure (by error pattern, task name, or table), the agent
uses it to accelerate root cause identification and provide proven resolutions.

## Format

Each rule is a block delimited by `---`:

---
PATTERN: error message pattern or task name pattern (matched case-insensitively)
CATEGORY: Code Failure | Data Quality Failure | Dependency Failure | Infrastructure Failure | Data Availability Failure
ROOT_CAUSE: description of the known root cause
FIX: proven resolution steps
ADDED_BY: person who contributed this knowledge
ADDED_ON: date added
NOTES: any additional context
---

## Rules

(Rules will be appended below by the API when users click "Add to Knowledge Base" in the Workbench.)

---
PATTERN: ENRICH_V2.PROC_IQVIA
CATEGORY: Code Failure
ROOT_CAUSE: Procedure is in ANALYTICS_V2 schema, not ENRICH_V2
FIX: Update task definition to reference ANALYTICS_V2.PROC_IQVIA_LAAD_EZN_WEEKLY_L1
ADDED_BY: test_user
ADDED_ON: 2026-07-06
NOTES: 
---
