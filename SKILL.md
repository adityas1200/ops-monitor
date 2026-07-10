---
name: rca-snowflake
description: >-
  Conducts structured Root Cause Analysis (RCA) on Snowflake data issues in the
  CPH_DB_PROD / PHCDW environment. Use when the user reports a data discrepancy,
  metric drop, null coverage gap, join failure, match-% decline, or any "why is
  this wrong?" question about Lynkuet, Blink, LAAD, overlap, WHC, or SPCL data.
  Covers the full investigation lifecycle: schema triage → coverage profiling →
  join trace → proc/DDL inspection → waterfall recovery → conclusion write-up.
  After every RCA this skill appends a structured learning entry to learnings.md
  so future investigations benefit from prior findings.
---

# RCA — Snowflake Data Investigation Skill

## Pre-flight (run every time)

1. **Read `learnings.md`** (this file's sibling). Prior RCAs are stored there.
   Scan for any entry whose *symptom* matches the current issue before writing
   a single query — you may already have the answer.

2. **Verify connection** via `connection.py` only:

   ```python
   from connection import get_snowflake_connection
   conn = get_snowflake_connection()
   cur  = conn.cursor()
   cur.execute("SELECT CURRENT_USER(), CURRENT_DATABASE(), CURRENT_SCHEMA(), CURRENT_WAREHOUSE()")
   print(cur.fetchone())
   ```

   Stop and surface the full error if this fails.

3. **Never run** DELETE / DROP / ALTER / UPDATE / CREATE OR REPLACE / TRUNCATE
   without explicit human approval. Read-only until told otherwise.

---

## Investigation lifecycle (6 phases)

### Phase 0 — Problem framing

Before any query, write out in one paragraph:
- **Symptom**: what the user observed (metric, table, date range, magnitude).
- **Hypothesis list**: 2–4 ranked guesses (draw from `learnings.md` first).
- **Scope**: which tables/views/procs are likely involved.

### Phase 1 — Schema triage

Always inspect objects before querying data.

```sql
-- Object type + row count + freshness
SELECT TABLE_TYPE, ROW_COUNT, CREATED, LAST_ALTERED
FROM <db>.INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = '<schema>'
  AND TABLE_NAME IN ('<table1>', '<table2>');

-- Column inventory
SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, ORDINAL_POSITION
FROM <db>.INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = '<schema>'
  AND TABLE_NAME = '<table>'
ORDER BY ORDINAL_POSITION;

-- DDL (views and procedures)
SELECT GET_DDL('VIEW',      '<db>.<schema>.<view>');
SELECT GET_DDL('PROCEDURE', '<db>.<schema>.<proc>');
```

Use regex on the proc body to find the exact logic around a suspect column:

```python
import re
for m in re.finditer(r'.{0,250}COLUMN_NAME.{0,250}', proc_def, re.I | re.S):
    print(m.group(0)[:500])
    print('---')
```

### Phase 2 — Coverage and null profiling

Standard null audit template (adapt columns):

```sql
SELECT
    COUNT(*)                                          AS total_rows,
    COUNT(<key_col>)                                  AS populated,
    COUNT(*) - COUNT(<key_col>)                       AS null_count,
    ROUND(100.0 * COUNT(<key_col>) / NULLIF(COUNT(*),0), 2) AS pct_populated,
    COUNT(DISTINCT <key_col>)                         AS distinct_values
FROM <table>;
```

Always break down by a categorical dimension if the null rate is unexpected:

```sql
SELECT <category_col>, COUNT(*), COUNT(<key_col>),
       ROUND(100.0 * COUNT(<key_col>) / NULLIF(COUNT(*),0), 2) AS pct
FROM <table>
GROUP BY 1 ORDER BY 2 DESC;
```

### Phase 3 — Join failure diagnosis

When a column that should be populated by a join is NULL:

```sql
-- Classify every row by its join outcome
SELECT
    CASE
        WHEN right_table.key IS NULL      THEN 'NO_MATCH_IN_RIGHT'
        WHEN right_table.value IS NULL    THEN 'MATCH_BUT_VALUE_NULL'
        ELSE                                   'FULL_MATCH'
    END AS match_category,
    COUNT(*) AS row_count
FROM left_table l
LEFT JOIN right_table r ON l.join_key = r.join_key
GROUP BY 1 ORDER BY 2 DESC;
```

For multi-key join failures, test each key independently:

```sql
SELECT
    SUM(CASE WHEN r1.key IS NOT NULL THEN 1 ELSE 0 END) AS match_via_key1,
    SUM(CASE WHEN r2.key IS NOT NULL THEN 1 ELSE 0 END) AS match_via_key2,
    SUM(CASE WHEN r3.key IS NOT NULL THEN 1 ELSE 0 END) AS match_via_key3,
    COUNT(*)                                             AS total
FROM left_table l
LEFT JOIN right_table r1 ON l.k1 = r1.k1
LEFT JOIN right_table r2 ON l.k2 = r2.k2
LEFT JOIN right_table r3 ON l.k3 = r3.k3
WHERE l.target_col IS NULL;
```

### Phase 4 — Waterfall recovery

Build COALESCE chains to measure how much coverage each alternative key
recovers. Always show current vs. proposed side by side:

```sql
SELECT
    COUNT(*)                                                          AS total_rows,
    COUNT(current_col)                                                AS current_populated,
    COUNT(COALESCE(current_col, alt1_col))                           AS after_alt1,
    COUNT(COALESCE(current_col, alt1_col, alt2_col))                 AS after_alt2,
    COUNT(COALESCE(current_col, alt1_col, alt2_col, alt3_col))       AS after_alt3,
    ROUND(100.0 * COUNT(COALESCE(current_col, alt1_col, alt2_col, alt3_col))
          / NULLIF(COUNT(*), 0), 2)                                  AS pct_if_waterfall
FROM base_table
LEFT JOIN alt1_source ON ...
LEFT JOIN alt2_source ON ...
LEFT JOIN alt3_source ON ...;
```

### Phase 5 — Match % / metric reconciliation

Used when a KPI count or match-% has dropped unexpectedly.

```sql
-- Numerator / denominator decomposition by week
SELECT
    WK_ID,
    COUNT(DISTINCT CASE WHEN <numerator_condition> THEN <id_col> END) AS numerator,
    COUNT(DISTINCT CASE WHEN <denominator_condition> THEN <id_col> END) AS denominator,
    ROUND(100.0 * COUNT(DISTINCT CASE WHEN <numerator_condition> THEN <id_col> END)
                / NULLIF(COUNT(DISTINCT CASE WHEN <denominator_condition> THEN <id_col> END), 0), 2)
        AS match_pct
FROM <fact_table>
WHERE <filters>
GROUP BY WK_ID ORDER BY WK_ID;
```

Rule attribution — count how many mismatches each rule explains:

```sql
SELECT
    rule_bucket,
    COUNT(*) AS mismatch_count,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct_of_mismatches
FROM (
    SELECT
        CASE
            WHEN <rule_1_condition> THEN 'Rule 1 — ...'
            WHEN <rule_2_condition> THEN 'Rule 2 — ...'
            ELSE                        'Unclassified'
        END AS rule_bucket
    FROM unmatched_records
) GROUP BY 1 ORDER BY 2 DESC;
```

### Phase 6 — Sampling and manual validation

Always pull concrete sample rows before concluding:

```sql
SELECT <key_cols>, <suspect_cols>
FROM <table>
WHERE <condition_under_investigation>
LIMIT 15;
```

Cross-validate by checking a known-good entity (a BH ID, a specific week,
a specific HCP) through every layer of the pipeline.

---

## Known tables and schemas (CPH_DB_PROD environment)

| Alias | Full path | Common use |
|-------|-----------|------------|
| CLM_RX | `ANALYTICS_V2.ANLT_BASE_FACT_CLM_RX` | Claim/Rx facts — primary grain |
| SALES_ALIGNED | `ANALYTICS_V2.ANLT_BASE_FACT_SALES_ALIGNED` | Sales-aligned fact |
| HCP_PROFILE | `ANALYTICS_V2.ANLT_BASE_DIM_CUSTOMER_HCP_PROFILE` | HCP master dim |
| BRAND_PRFL | `ANALYTICS_V2.ANLT_BASE_DIM_HCP_BRAND_PRFL_LYN` | Brand-level HCP dim |
| WHC | `ANALYTICS_V2.WHC_ASD_MEMBERSHIP_NEW` | WHC account membership |
| HIN_EXTRACT | `ANALYTICS_V2.BAYER_ACCOUNT_HIN_EXTRACT` | Account ↔ HIN mapping |
| BLINK_VW | `ANALYTICS_V2.REP_GEO_BRND_SEG_SPEC_BLINK_MTR_VW` | Blink dossier view |
| DIM_SRC_HCP | `MODEL_V2.DIM_SOURCE_CUSTOMER_HCP` | Source-level HCP attributes |
| ACCT_XREF | `PHCDW.PHCDW_STG.MDM_STG_ACCT_XREF` | MDM account cross-reference |
| ACCT_PRFL | `PHCDW.PHCDW_STG.MDM_STG_ACCT_PRFL` | MDM account profile |
| CUST_MSTR | `CPH_DB_PROD.ENRICH.STG_ASD_WHC_CUST_MSTR_HIST_DATA` | Customer master hist |

**Primary data sources:** `BLINKRX_LYN`, `IQV_LAAD_EZN`, `SHS_APLD`

**Common join keys:** `BHO_ID_ASD_ID`, `BH*` (HCP IDs), `NPI_NUM`, `BAYER_HIN`,
`ASD_HIN`, `VISTEX_CONTRACT_ID`, `MEMBERS_BAYER_ID`

---

## Query runner convention

Every script in this project follows this pattern — keep it consistent:

```python
"""<one-line description of investigation>."""
from connection import get_snowflake_connection

def q(cur, sql, label, limit=60):
    print(f"\n{'='*70}\n{label}\n{'='*70}")
    cur.execute(sql)
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    print(" | ".join(cols))
    for r in rows[:limit]:
        print(" | ".join(str(x) for x in r))
    if len(rows) > limit:
        print(f"... {len(rows)-limit} more")
    return cols, rows

def main():
    conn = get_snowflake_connection()
    cur  = conn.cursor()
    try:
        q(cur, "SELECT ...", "Step N — <label>")
    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    main()
```

---

## Self-learning protocol (MANDATORY after every RCA)

After reaching a conclusion — or even a partial conclusion — **append a new
entry to `learnings.md`** using this exact template:

```markdown
---

## RCA-<NNN> · <YYYY-MM-DD> · <Issue title (≤10 words)>

**Symptom:** <What the user observed — metric, table, magnitude, date>

**Tables involved:** `<table1>`, `<table2>`, ...

**Root cause:** <One or two sentences — the actual finding>

**Investigation path:**
1. <First thing checked and what it showed>
2. <Second thing checked and what it showed>
3. <How the root cause was confirmed>

**Key query / pattern used:**
```sql
<the single most reusable query from this RCA>
```

**Resolution / recommendation:** <What was done or recommended>

**Reuse signal:** <Tag — e.g., null-coverage, join-failure, match-pct-drop,
                   proc-logic, cross-source-mismatch, waterfall-recovery>
```

Increment `<NNN>` by reading the last entry number in `learnings.md`.

Before starting a new RCA, always check `learnings.md` for matching
**Reuse signals** or similar symptoms — this is how the skill learns.

---

## Reference files

- For query archetypes and reusable SQL blocks → [patterns.md](patterns.md)
- For all prior RCA conclusions → [learnings.md](learnings.md)
- For a blank starter script → [scripts/rca_template.py](scripts/rca_template.py)
