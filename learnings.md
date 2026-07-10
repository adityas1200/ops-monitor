# RCA Learnings Log

This file is the **memory** of the `rca-snowflake` skill.
Every completed investigation appends one entry here.
The skill reads this file at the start of every new RCA.

Format: entries are numbered RCA-001, RCA-002, … in chronological order.
The skill increments the counter automatically.

---

## RCA-001 · 2026-04 · WHC CENCORA_SOURCE_ID null — join key gap

**Symptom:** `CENCORA_SOURCE_ID` was NULL for a significant portion of
members in `ANALYTICS_V2.WHC_ASD_MEMBERSHIP_NEW`, causing downstream
account enrichment to fail.

**Tables involved:** `ANALYTICS_V2.WHC_ASD_MEMBERSHIP_NEW`,
`ANALYTICS_V2.BAYER_ACCOUNT_HIN_EXTRACT`, `PHCDW.PHCDW_STG.MDM_STG_ACCT_XREF`,
`PHCDW.PHCDW_STG.MDM_STG_ACCT_PRFL`

**Root cause:** Join C (BHO_ID_ASD_ID → MEMBERS_BAYER_ID) failed for a
subset of members because those BHO IDs were not present as `BHO_ID_ASD_ID`
in the HIN extract, even though they existed in `MDM_STG_ACCT_XREF` with
THR/ASD source codes. The xref SOURCE_IDs were also not present as
`ACCT_ACCT_ID` in the HIN extract, so no secondary path worked either.

**Investigation path:**
1. Checked object metadata (TABLE_TYPE, ROW_COUNT) — confirmed both tables exist.
2. Ran null audit on `CENCORA_SOURCE_ID` → ~X% null.
3. Classified null rows into `NO_MATCH_IN_HIN_EXTRACT` / `MATCH_BHO_BUT_ACCT_NULL`
   / `MATCH_WITH_ACCT` via LEFT JOIN breakdown.
4. Tested five alternative keys (BHO, HIN, MDM_HIN, DEA, xref) — each recovered
   a distinct but overlapping subset.
5. Built full waterfall COALESCE chain measuring projected coverage.
6. Confirmed xref SOURCE_IDs mostly absent from HIN_EXTRACT (universe mismatch).

**Key query / pattern used:**
```sql
-- Classify join outcomes for null rows
SELECT
    CASE
        WHEN h.BHO_ID_ASD_ID IS NULL     THEN 'NO_MATCH_IN_HIN_EXTRACT'
        WHEN h.ACCT_ACCT_ID  IS NULL     THEN 'MATCH_BHO_BUT_ACCT_NULL'
        ELSE                                  'MATCH_WITH_ACCT'
    END AS match_category,
    COUNT(*) AS member_count
FROM (SELECT DISTINCT MEMBERS_BAYER_ID FROM WHC WHERE CENCORA_SOURCE_ID IS NULL) w
LEFT JOIN (
    SELECT BHO_ID_ASD_ID, MAX(ACCT_ACCT_ID) AS ACCT_ACCT_ID
    FROM BAYER_ACCOUNT_HIN_EXTRACT GROUP BY 1
) h ON w.MEMBERS_BAYER_ID = h.BHO_ID_ASD_ID
GROUP BY 1 ORDER BY 2 DESC;
```

**Resolution / recommendation:** Proposed a waterfall join strategy:
1. Primary: current join C (BHO ID → HIN extract).
2. Fallback 1: xref SOURCE_ID (THR/ASD) as CENCORA_SOURCE_ID.
3. Fallback 2: BHO_ID_HIN via BAYER_HIN or ASD_HIN.
4. Fallback 3: CUST_MSTR_HIST join via HIN.

**Reuse signal:** `null-coverage`, `join-failure`, `waterfall-recovery`,
`account-enrichment`, `WHC`, `HIN-extract`

---

## RCA-002 · 2026-04 · HCP_PRMRY_ZIP cross-source mismatch (Blink vs LAAD)

**Symptom:** 38 HCPs had different `HCP_PRMRY_ZIP` values across
`BLINKRX_LYN` and `IQV_LAAD_EZN` rows in `ANLT_BASE_FACT_CLM_RX`,
causing territory/alignment issues for the IC HCP list.

**Tables involved:** `ANALYTICS_V2.ANLT_BASE_FACT_CLM_RX`,
`ANALYTICS_V2.ANLT_BASE_DIM_HCP_BRAND_PRFL_LYN`,
`ANALYTICS_V2.ANLT_BASE_DIM_CUSTOMER_HCP_PROFILE`,
`MODEL_V2.DIM_SOURCE_CUSTOMER_HCP`

**Root cause:** `HCP_PRMRY_ZIP` in the fact is sourced from the brand
profile dim (`ANLT_BASE_DIM_HCP_BRAND_PRFL_LYN`) — a single MDM-resolved
ZIP applied to all sources. However, each source delivers its own
`SOURCE_HCP_ZIP_CD`. The Blink proc populates `HCP_PRMRY_ZIP` from the
brand profile (MDM preferred), while the LAAD proc derives it from the
source-level dim (`DIM_SOURCE_CUSTOMER_HCP`). When those two disagree, the
fact records for the same HCP carry different primary ZIPs by source,
rather than both using the MDM canonical value.

**Investigation path:**
1. Identified 38 HCPs with >1 distinct `HCP_PRMRY_ZIP` across sources.
2. Verified within-source consistency (0 HCPs had multi-ZIP within a single
   source) → confirmed this is a cross-source, not within-source problem.
3. Regex-scanned LAAD and Blink proc definitions for `HCP_PRMRY_ZIP`
   assignment context.
4. Compared brand profile ZIP vs fact ZIP for the 38 HCPs — Blink always
   matched the brand profile; LAAD matched its own dim.
5. Validated lineage for a sample HCP (BH10123009) end-to-end.

**Key query / pattern used:**
```sql
-- Find HCPs with cross-source ZIP mismatch
SELECT CUST_HCP_ID, COUNT(DISTINCT HCP_PRMRY_ZIP) AS zip_count
FROM ANALYTICS_V2.ANLT_BASE_FACT_CLM_RX
WHERE UPPER(PROD_BRAND_NM) = 'LYNKUET'
  AND SYS_DATA_SOURCE IN ('BLINKRX_LYN', 'IQV_LAAD_EZN')
GROUP BY 1
HAVING COUNT(DISTINCT SYS_DATA_SOURCE) = 2
   AND COUNT(DISTINCT HCP_PRMRY_ZIP) > 1;
```

**Resolution / recommendation:** Align the LAAD proc to use the brand
profile ZIP (`ANLT_BASE_DIM_HCP_BRAND_PRFL_LYN`) as the primary source for
`HCP_PRMRY_ZIP`, matching the Blink proc behaviour. For the IC HCP list,
use the brand profile ZIP as the authoritative value in the interim.

**Reuse signal:** `cross-source-mismatch`, `zip-address`, `proc-logic`,
`LAAD`, `Blink`, `HCP_PRMRY_ZIP`

---

## RCA-003 · 2026-05 · Lynkuet overlap match % drop — WoW decline

**Symptom:** Lynkuet overlap match % fell from 94.9% (WK 20260424) →
87.6% (WK 20260501) → 74.0% (WK 20260508), a 20.9 pp total drop over
three weeks.

**Tables involved:** `ANALYTICS_V2.ANLT_BASE_FACT_CLM_RX`

**Root cause:** Two distinct mechanisms drove the decline:
- **WK1→WK2 (−7.3 pp):** Both numerator (LAAD overlap) and denominator
  (Blink paid) fell, but numerator fell faster (−266 vs −188 patients).
  Priority/tie-break losses (Rules 4–6) rose from 23% to 39% of residual
  mismatches.
- **WK2→WK3 (−13.6 pp):** Blink paid volume spiked +230 patients (+19%)
  while LAAD overlap count was essentially flat (+8). Grain failures
  worsened across NPI coverage, age alignment, date tolerance, and priority
  competition. Rules 4–6 reached 42% of post-R1/R3 mismatches.

**Investigation path:**
1. Confirmed match % formula: LAAD overlap PTNT_IDs / Blink paid PTNT_IDs.
2. Verified Blink date grain = `ORDR_CRTD_DT` (not `DATE_ID`) — using
   `DATE_ID` inflated Rule 3 (process gap) cases from 161 → 16.
3. Decomposed numerator and denominator week-over-week.
4. Attributed unmatched Blink patients to failure buckets (Rules 1–8).
5. Simulated date tolerance relaxation: ±3 days recovers +1.3 pp, ±4 days
   +2.2 pp per week.

**Key query / pattern used:**
```sql
-- Numerator / denominator trend
SELECT
    WK_ID,
    COUNT(DISTINCT CASE WHEN SYS_DATA_SOURCE='IQV_LAAD_EZN'
                         AND PAID_CLM_FLG=1 AND OVRLP_FLG=1
                        THEN PTNT_ID END) AS laad_overlap,
    COUNT(DISTINCT CASE WHEN SYS_DATA_SOURCE='BLINKRX_LYN'
                         AND PAID_CLM_FLG=1
                         AND COALESCE(PHRMCY_NM,'k') <> 'Blink Health Pharmacy'
                        THEN PTNT_ID END) AS blink_paid,
    ROUND(100.0 *
        COUNT(DISTINCT CASE WHEN SYS_DATA_SOURCE='IQV_LAAD_EZN'
                              AND PAID_CLM_FLG=1 AND OVRLP_FLG=1
                             THEN PTNT_ID END)
        / NULLIF(COUNT(DISTINCT CASE WHEN SYS_DATA_SOURCE='BLINKRX_LYN'
                                      AND PAID_CLM_FLG=1
                                      AND COALESCE(PHRMCY_NM,'k') <> 'Blink Health Pharmacy'
                                     THEN PTNT_ID END), 0), 2) AS match_pct
FROM ANALYTICS_V2.ANLT_BASE_FACT_CLM_RX
WHERE UPPER(PROD_BRAND_NM) = 'LYNKUET'
  AND WK_ID IN ('20260424','20260501','20260508')
GROUP BY WK_ID ORDER BY WK_ID;
```

**Resolution / recommendation:** Short term — widen date tolerance from ±2
to ±3 days (highest leverage, +1.3 pp). Long term — investigate the NPI
structural gap (~25–35% of LAAD NPIs absent from Blink) as a persistent
ceiling on match %. Age ±1 year adds negligible recovery (~0.1 pp).

**Reuse signal:** `match-pct-drop`, `overlap`, `LAAD`, `Blink`, `Lynkuet`,
`rule-attribution`, `date-tolerance`

---

## RCA-004 · 2026-05 · Blink dossier LAST_DISPENSE_DATE wrong for Rejected/Abandoned

**Symptom:** `LAST_DISPENSE_DATE` and `NEXT_REFILL_DATE` in the Blink
dossier view (`REP_GEO_BRND_SEG_SPEC_BLINK_MTR_VW`) showed non-null dates
for patients whose `LATEST_PATIENT_STATUS` was Script Rejected or Script
Abandoned — logically incorrect, as these patients never received a
dispense.

**Tables involved:** `ANALYTICS_V2.REP_GEO_BRND_SEG_SPEC_BLINK_MTR_VW`
(and its `COMBINED_UNIVERSE` CTE)

**Root cause:** The `COMBINED_UNIVERSE` CTE in the view definition did not
filter `LAST_DISPENSE_DATE` / `NEXT_REFILL_DATE` by patient status. The
status is resolved via
`COALESCE(B.LATEST_PATIENT_STATUS, A.LATEST_PATIENT_STATUS, 'Not Dispensed')`
only downstream; the date columns were populated unconditionally.

**Investigation path:**
1. Fetched live DDL with `GET_DDL('VIEW', ...)`.
2. Located `LAST_DISPENSE_DATE` and `NEXT_REFILL_DATE` assignments in
   `COMBINED_UNIVERSE`.
3. Confirmed `LATEST_PATIENT_STATUS` resolution in the same CTE.
4. Patched the view DDL to wrap both date columns in a CASE:
   `CASE WHEN status NOT IN ('Script Rejected','Script Abandoned')
         THEN LAST_DISPENSE_DATE END`
5. Materialised as a session temp table and ran before/after validation.

**Key query / pattern used:**
```sql
-- Before/after validation for a sample HCP + territory
SELECT TIME_BUCKET, LATEST_PATIENT_STATUS,
       LAST_DISPENSE_DATE, NEXT_REFILL_DATE
FROM TMP_REP_GEO_BRND_SEG_SPEC_BLINK_MTR_VW_FIX
WHERE HCP_ID = 'BH14219497'
  AND TERR_ID = '12300'
ORDER BY TIME_BUCKET;
```

**Resolution / recommendation:** Wrap `LAST_DISPENSE_DATE` and
`NEXT_REFILL_DATE` in a CASE guard on `LATEST_PATIENT_STATUS` inside
`COMBINED_UNIVERSE`. Deploy as a view DDL update (requires human approval
before `CREATE OR REPLACE`).

**Reuse signal:** `proc-logic`, `view-ddl`, `Blink`, `dossier`,
`date-column-wrong`, `LATEST_PATIENT_STATUS`

---

## RCA-005 · 2026-06 · Lynkuet MA table — Approved Paid Insured count discrepancy

**Symptom:** `ANLT_BASE_FACT_LYN_MKT_SALES` showed a different count of
"Approved Paid Insured" patients than `ANLT_BASE_FACT_CLM_RX` for the
same time window, causing report discrepancies.

**Tables involved:** `ANALYTICS_V2.ANLT_BASE_FACT_LYN_MKT_SALES`,
`ANALYTICS_V2.ANLT_BASE_FACT_CLM_RX`

**Root cause:** The MA table (`ANLT_BASE_FACT_LYN_MKT_SALES`) is a
separate feed with its own `CLAIM_ID` space and `PSS_SUPPORT_TYPE`
vocabulary that does not map 1:1 to the L3 `CLM_RX` table. A FULL OUTER
JOIN on `claim_id` between the two tables showed minimal overlap, meaning
the MA table covers a distinct claim universe (market-level, not
rep-level), not a subset of `CLM_RX`.

**Investigation path:**
1. Inspected schema — confirmed both tables exist separately.
2. Ran weekly breakdown in MA table (`pss_support_type` = 'BAP' /
   'PAID INSURED').
3. Attempted FULL OUTER JOIN on `claim_id` with CLM_RX — near-zero overlap.
4. Concluded the two tables represent different claim populations; comparison
   requires explicit grain alignment, not a direct count comparison.

**Key query / pattern used:**
```sql
SELECT
    ma.pss_support_type,
    COUNT(DISTINCT ma.claim_id) AS ma_claim_cnt,
    COUNT(DISTINCT l3.claim_id) AS l3_claim_cnt
FROM analytics_v2.anlt_base_fact_lyn_mkt_sales ma
FULL OUTER JOIN analytics_v2.anlt_base_fact_clm_rx l3
    ON ma.claim_id = l3.claim_id
    AND l3.sys_data_source = 'BLINKRX_LYN'
    AND l3.prod_brand_cd   = '50419-475'
GROUP BY 1 ORDER BY 2 DESC;
```

**Resolution / recommendation:** Treat `ANLT_BASE_FACT_LYN_MKT_SALES` as
a market-level fact separate from the rep-facing L3. Do not compare raw
counts. Align on a common grain (week + PSS type + claim_id) or use only
one source per report.

**Reuse signal:** `metric-discrepancy`, `MA-table`, `CLM-RX`, `Blink`,
`Lynkuet`, `table-universe-mismatch`

---

<!-- New entries are appended below this line by the skill after each RCA -->
