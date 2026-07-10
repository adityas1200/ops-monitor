# RCA Query Pattern Library

Reusable SQL and Python snippets extracted from prior investigations.
Reference this file when a new RCA matches a known pattern type.

---

## Pattern 1 — Null audit (single column)

```sql
SELECT
    COUNT(*)                                                       AS total_rows,
    COUNT(<col>)                                                   AS populated,
    COUNT(*) - COUNT(<col>)                                        AS null_count,
    ROUND(100.0 * COUNT(<col>) / NULLIF(COUNT(*), 0), 2)          AS pct_populated,
    COUNT(DISTINCT <col>)                                          AS cardinality
FROM <db>.<schema>.<table>;
```

---

## Pattern 2 — Multi-column null audit (population matrix)

```sql
SELECT
    COUNT(*)                          AS total,
    COUNT(col_a)                      AS col_a_nn,
    COUNT(col_b)                      AS col_b_nn,
    COUNT(col_c)                      AS col_c_nn,
    COUNT(CASE WHEN col_a IS NULL AND col_b IS NULL THEN 1 END) AS both_null,
    COUNT(CASE WHEN col_a IS NOT NULL AND col_b IS NULL THEN 1 END) AS a_only,
    COUNT(CASE WHEN col_b IS NOT NULL AND col_a IS NULL THEN 1 END) AS b_only
FROM <table>;
```

---

## Pattern 3 — Join outcome classifier (3-way)

Diagnoses exactly *why* a left join produces NULLs.

```sql
SELECT
    CASE
        WHEN r.join_key IS NULL  THEN 'NO_MATCH_IN_RIGHT'
        WHEN r.value    IS NULL  THEN 'MATCH_BUT_VALUE_NULL'
        ELSE                          'FULL_MATCH'
    END AS outcome,
    COUNT(*) AS row_count,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct
FROM left_table l
LEFT JOIN right_table r ON l.join_key = r.join_key
GROUP BY 1 ORDER BY 2 DESC;
```

---

## Pattern 4 — Multi-key join recovery test

Tests how many rows can be recovered when the primary join key fails.

```sql
SELECT
    SUM(CASE WHEN r1.val IS NOT NULL THEN 1 ELSE 0 END) AS via_key1,
    SUM(CASE WHEN r2.val IS NOT NULL THEN 1 ELSE 0 END) AS via_key2,
    SUM(CASE WHEN r3.val IS NOT NULL THEN 1 ELSE 0 END) AS via_key3,
    SUM(CASE WHEN COALESCE(r1.val, r2.val, r3.val) IS NOT NULL THEN 1 ELSE 0 END) AS any_key,
    COUNT(*)                                             AS total
FROM null_rows l
LEFT JOIN ref r1 ON l.k1 = r1.k1
LEFT JOIN ref r2 ON l.k2 = r2.k2
LEFT JOIN ref r3 ON l.k3 = r3.k3;
```

---

## Pattern 5 — Waterfall coverage comparison

Measures progressive coverage gain from a COALESCE chain.

```sql
SELECT
    COUNT(*)                                                    AS total_rows,
    COUNT(current_col)                                          AS current_nn,
    COUNT(COALESCE(current_col, alt1))                          AS after_alt1,
    COUNT(COALESCE(current_col, alt1, alt2))                    AS after_alt2,
    COUNT(COALESCE(current_col, alt1, alt2, alt3))              AS after_alt3,
    ROUND(100.0 * COUNT(COALESCE(current_col, alt1, alt2, alt3))
                / NULLIF(COUNT(*), 0), 2)                       AS waterfall_pct
FROM base b
LEFT JOIN src1 s1 ON b.k = s1.k
LEFT JOIN src2 s2 ON b.k = s2.k
LEFT JOIN src3 s3 ON b.k = s3.k;
```

---

## Pattern 6 — Match % week-over-week trend

```sql
SELECT
    WK_ID,
    COUNT(DISTINCT CASE WHEN <numerator_cond> THEN <id> END)   AS numerator,
    COUNT(DISTINCT CASE WHEN <denominator_cond> THEN <id> END) AS denominator,
    ROUND(100.0 *
        COUNT(DISTINCT CASE WHEN <numerator_cond> THEN <id> END)
        / NULLIF(COUNT(DISTINCT CASE WHEN <denominator_cond> THEN <id> END), 0)
    , 2)                                                        AS match_pct
FROM <fact_table>
WHERE <filters>
  AND WK_ID IN (<week_list>)
GROUP BY WK_ID ORDER BY WK_ID;
```

---

## Pattern 7 — Rule attribution (mismatch bucketing)

```sql
SELECT
    rule_bucket,
    COUNT(*)                                                       AS cnt,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2)            AS pct
FROM (
    SELECT
        CASE
            WHEN <cond_rule1> THEN 'Rule 1 — <description>'
            WHEN <cond_rule2> THEN 'Rule 2 — <description>'
            WHEN <cond_rule3> THEN 'Rule 3 — <description>'
            ELSE                   'Unclassified'
        END AS rule_bucket
    FROM unmatched_records
)
GROUP BY 1 ORDER BY 2 DESC;
```

---

## Pattern 8 — DDL extraction + regex scan (Python)

Extracts proc/view logic and scans it for column usage context.

```python
import re
from connection import get_snowflake_connection

conn = get_snowflake_connection()
cur  = conn.cursor()

# Fetch proc definition
cur.execute("""
    SELECT PROCEDURE_DEFINITION
    FROM <db>.INFORMATION_SCHEMA.PROCEDURES
    WHERE PROCEDURE_SCHEMA = '<schema>'
      AND PROCEDURE_NAME   = '<proc_name>'
""")
proc_def = cur.fetchone()[0]

# Find all context windows around a suspect column
SEARCH_TERMS = ['COLUMN_NAME', 'RELATED_COLUMN']
for term in SEARCH_TERMS:
    print(f"\n=== Contexts for: {term} ===")
    for m in re.finditer(rf'.{{0,250}}{term}.{{0,250}}', proc_def, re.I | re.S):
        print(m.group(0).replace('\n', ' ')[:500])
        print('---')

cur.close(); conn.close()
```

---

## Pattern 9 — Cross-source attribute comparison (e.g., ZIP mismatch)

Finds entities with different attribute values across two data sources in
the same fact table.

```sql
SELECT
    <id_col>,
    COUNT(DISTINCT <attribute_col>)   AS distinct_values,
    MIN(<attribute_col>)              AS src1_val,
    MAX(<attribute_col>)              AS src2_val
FROM <fact_table>
WHERE <data_source_col> IN ('<src1>', '<src2>')
  AND <other_filters>
GROUP BY <id_col>
HAVING COUNT(DISTINCT <data_source_col>) = 2        -- present in both sources
   AND COUNT(DISTINCT <attribute_col>)   > 1        -- attribute differs
ORDER BY <id_col>;
```

---

## Pattern 10 — Object existence and lineage check

```sql
-- Is this a table or a view?
SELECT TABLE_TYPE, ROW_COUNT, CREATED, LAST_ALTERED
FROM <db>.INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = '<schema>'
  AND TABLE_NAME   = '<object>';

-- Procedures that reference this table
SELECT PROCEDURE_NAME, LEFT(PROCEDURE_DEFINITION, 200) AS def_snippet
FROM <db>.INFORMATION_SCHEMA.PROCEDURES
WHERE PROCEDURE_SCHEMA = '<schema>'
  AND UPPER(PROCEDURE_DEFINITION) LIKE UPPER('%<table_name>%')
LIMIT 20;

-- Related views
SHOW VIEWS LIKE '<pattern>%' IN SCHEMA <db>.<schema>;
```

---

## Pattern 11 — Lynkuet overlap match % (production formula)

*Validated against production — do not change the Blink exclusion filter.*

```sql
SELECT
    WK_ID,
    COUNT(DISTINCT CASE WHEN SYS_DATA_SOURCE='IQV_LAAD_EZN'
                         AND PAID_CLM_FLG=1 AND OVRLP_FLG=1
                         AND UPPER(PROD_BRAND_NM)='LYNKUET'
                        THEN PTNT_ID END)  AS laad_overlap,
    COUNT(DISTINCT CASE WHEN SYS_DATA_SOURCE='BLINKRX_LYN'
                         AND PAID_CLM_FLG=1
                         AND UPPER(PROD_BRAND_NM)='LYNKUET'
                         AND COALESCE(PHRMCY_NM,'k') <> 'Blink Health Pharmacy'
                        THEN PTNT_ID END)  AS blink_paid,
    ROUND(100.0 *
        COUNT(DISTINCT CASE WHEN SYS_DATA_SOURCE='IQV_LAAD_EZN'
                              AND PAID_CLM_FLG=1 AND OVRLP_FLG=1
                              AND UPPER(PROD_BRAND_NM)='LYNKUET'
                             THEN PTNT_ID END)
        / NULLIF(COUNT(DISTINCT CASE WHEN SYS_DATA_SOURCE='BLINKRX_LYN'
                                      AND PAID_CLM_FLG=1
                                      AND UPPER(PROD_BRAND_NM)='LYNKUET'
                                      AND COALESCE(PHRMCY_NM,'k')<>'Blink Health Pharmacy'
                                     THEN PTNT_ID END), 0)
    , 2) AS match_pct
FROM CPH_DB_PROD.ANALYTICS_V2.ANLT_BASE_FACT_CLM_RX
WHERE WK_ID IN (<weeks>)
GROUP BY WK_ID ORDER BY WK_ID;
```

Notes:
- Blink date grain for matching = `ORDR_CRTD_DT` (not `DATE_ID`).
- Date tolerance = 0 / ±1 / ±2 days (Rule 3).
- LAAD-only rows receive `OVRLP_FLG = 1`; Blink rows never get it.

---

## Pattern 12 — WHC/HIN waterfall (account enrichment)

*Validated against production (RCA-001). Key order matters — ASD preferred
over THR in xref.*

```sql
WITH xref AS (
    SELECT BHO_ID, SOURCE_ID
    FROM PHCDW.PHCDW_STG.MDM_STG_ACCT_XREF
    WHERE UPPER(SOURCE_CD) IN ('THR','ASD') AND SOURCE_ID IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY BHO_ID
        ORDER BY CASE WHEN UPPER(SOURCE_CD)='ASD' THEN 1 ELSE 2 END, SOURCE_ID
    ) = 1
),
hin_bho AS (SELECT BHO_ID_ASD_ID, MIN(ACCT_ACCT_ID) a FROM BAYER_ACCOUNT_HIN_EXTRACT
             WHERE BHO_ID_ASD_ID IS NOT NULL GROUP BY 1),
hin_hin AS (SELECT BHO_ID_HIN,    MIN(ACCT_ACCT_ID) a FROM BAYER_ACCOUNT_HIN_EXTRACT
             WHERE BHO_ID_HIN IS NOT NULL GROUP BY 1)
SELECT
    COUNT(*)                                                      AS total,
    COUNT(w.CENCORA_SOURCE_ID)                                    AS current,
    COUNT(COALESCE(w.CENCORA_SOURCE_ID, h1.a))                   AS +hin_bho,
    COUNT(COALESCE(w.CENCORA_SOURCE_ID, h1.a, x.SOURCE_ID))      AS +xref,
    COUNT(COALESCE(w.CENCORA_SOURCE_ID, h1.a, x.SOURCE_ID, h2.a)) AS +hin_hin
FROM ANALYTICS_V2.WHC_ASD_MEMBERSHIP_NEW w
LEFT JOIN hin_bho h1 ON w.MEMBERS_BAYER_ID = h1.BHO_ID_ASD_ID
LEFT JOIN xref    x  ON w.MEMBERS_BAYER_ID = x.BHO_ID
LEFT JOIN hin_hin h2 ON COALESCE(w.ASD_HIN, w.BAYER_HIN) = h2.BHO_ID_HIN;
```

---

## Pattern 13 — Sample rows for manual validation

Always pull 10–20 rows of the exact entities under investigation:

```sql
SELECT *
FROM <table>
WHERE <suspect_condition>
ORDER BY <meaningful_sort>
LIMIT 15;
```

And always verify a known-good entity through all layers:

```sql
-- Validate a single BH ID end to end
SELECT 'fact'        AS layer, HCP_PRMRY_ZIP, SOURCE_HCP_ZIP_CD
FROM ANALYTICS_V2.ANLT_BASE_FACT_CLM_RX
WHERE CUST_HCP_ID='<BH_ID>' AND SYS_DATA_SOURCE='<source>' LIMIT 1

UNION ALL

SELECT 'brand_prfl'  AS layer, CUST_PRMRY_ZIP, CUST_PRMRY_ADDR_LN_1
FROM ANALYTICS_V2.ANLT_BASE_DIM_HCP_BRAND_PRFL_LYN
WHERE BAYER_CUST_HCP_ID='<BH_ID>' AND PROD_BRAND_NM='<brand>';
```
