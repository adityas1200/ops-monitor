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

---
PATTERN: CENCORA_SOURCE_ID null join key WHC HIN
CATEGORY: Data Quality Failure
ROOT_CAUSE: Join between WHC_ASD_MEMBERSHIP_NEW and BAYER_ACCOUNT_HIN_EXTRACT fails for a subset of members because BHO IDs are not present as BHO_ID_ASD_ID in the HIN extract. The xref SOURCE_IDs are also absent as ACCT_ACCT_ID in HIN extract — universe mismatch between xref and HIN.
FIX: Implement waterfall COALESCE join strategy: (1) Primary join on BHO_ID→HIN extract, (2) Fallback xref SOURCE_ID (THR/ASD), (3) Fallback BHO_ID_HIN via BAYER_HIN/ASD_HIN, (4) Fallback CUST_MSTR_HIST via HIN. Each recovers a distinct subset.
ADDED_BY: RCA-001 learning
ADDED_ON: 2026-04
NOTES: Use Pattern 3 (join outcome classifier) to diagnose. Tables: WHC_ASD_MEMBERSHIP_NEW, BAYER_ACCOUNT_HIN_EXTRACT, MDM_STG_ACCT_XREF, MDM_STG_ACCT_PRFL
---

---
PATTERN: HCP_PRMRY_ZIP cross-source mismatch Blink LAAD
CATEGORY: Data Quality Failure
ROOT_CAUSE: HCP_PRMRY_ZIP in ANLT_BASE_FACT_CLM_RX differs between BLINKRX_LYN and IQV_LAAD_EZN sources. Blink proc uses brand profile dim (MDM canonical ZIP); LAAD proc uses source-level dim (DIM_SOURCE_CUSTOMER_HCP). When MDM and source disagree, same HCP gets different ZIPs by source.
FIX: Align LAAD proc to use ANLT_BASE_DIM_HCP_BRAND_PRFL_LYN as the source for HCP_PRMRY_ZIP (matching Blink proc). For interim, use brand profile ZIP as authoritative in IC HCP list.
ADDED_BY: RCA-002 learning
ADDED_ON: 2026-04
NOTES: Use Pattern 9 (cross-source attribute comparison) to identify affected HCPs. Tables: ANLT_BASE_FACT_CLM_RX, ANLT_BASE_DIM_HCP_BRAND_PRFL_LYN, DIM_SOURCE_CUSTOMER_HCP
---

---
PATTERN: overlap match percent drop Lynkuet LAAD Blink
CATEGORY: Data Quality Failure
ROOT_CAUSE: Match % decline driven by (1) Blink paid volume spike without corresponding LAAD overlap growth, and (2) Priority/tie-break rule failures (Rules 4-6) increasing from 23% to 42% of residual mismatches. NPI structural gap (~25-35% of LAAD NPIs absent from Blink) acts as persistent ceiling.
FIX: Short term: widen date tolerance from ±2 to ±3 days (+1.3 pp recovery). Long term: investigate NPI structural gap. Use ORDR_CRTD_DT (not DATE_ID) for Blink date grain. Age ±1 year adds negligible recovery.
ADDED_BY: RCA-003 learning
ADDED_ON: 2026-05
NOTES: Use Pattern 6 (match % WoW trend) and Pattern 7 (rule attribution) to diagnose. Blink exclusion: COALESCE(PHRMCY_NM,'k') <> 'Blink Health Pharmacy'
---

---
PATTERN: LAST_DISPENSE_DATE NEXT_REFILL_DATE wrong Rejected Abandoned Blink dossier
CATEGORY: Code Failure
ROOT_CAUSE: COMBINED_UNIVERSE CTE in REP_GEO_BRND_SEG_SPEC_BLINK_MTR_VW populates LAST_DISPENSE_DATE/NEXT_REFILL_DATE unconditionally. LATEST_PATIENT_STATUS is resolved downstream via COALESCE but date columns are not filtered by status — so Rejected/Abandoned patients show dispense dates.
FIX: Wrap both date columns in CASE guard: CASE WHEN status NOT IN ('Script Rejected','Script Abandoned') THEN LAST_DISPENSE_DATE END. Deploy as view DDL update (requires human approval for CREATE OR REPLACE).
ADDED_BY: RCA-004 learning
ADDED_ON: 2026-05
NOTES: Use Pattern 8 (DDL extraction + regex scan) to locate assignments. View: REP_GEO_BRND_SEG_SPEC_BLINK_MTR_VW
---

---
PATTERN: Approved Paid Insured count discrepancy MA table CLM_RX Lynkuet
CATEGORY: Data Quality Failure
ROOT_CAUSE: ANLT_BASE_FACT_LYN_MKT_SALES (MA table) and ANLT_BASE_FACT_CLM_RX have distinct claim universes with near-zero CLAIM_ID overlap. MA table covers market-level data, CLM_RX covers rep-level. Direct count comparison is invalid.
FIX: Do NOT compare raw counts between these tables. Align on common grain (week + PSS type + claim_id) or use only one source per report. Treat MA table as a separate market-level fact.
ADDED_BY: RCA-005 learning
ADDED_ON: 2026-06
NOTES: Use FULL OUTER JOIN on claim_id to confirm universe separation. Tables: ANLT_BASE_FACT_LYN_MKT_SALES, ANLT_BASE_FACT_CLM_RX
---

---
PATTERN: Statement reached its statement or warehouse timeout
CATEGORY: Infrastructure Failure
ROOT_CAUSE: Query exceeded warehouse statement timeout. Common causes: unoptimized joins on large tables, missing clustering keys, warehouse auto-suspend during long queries, or competing workloads consuming compute.
FIX: Re-run during off-peak hours on larger warehouse. Check for full table scans, missing micro-partition pruning, or bytes_spilled_to_remote_storage > 0. Consider multi-cluster auto-scaling or query splitting.
ADDED_BY: rca_memory patterns
ADDED_ON: 2026-06
NOTES: Seen repeatedly for TASK_GLXAI_KERENDIA_REFRESH and TASK_PROC_SALES_ALIGNED_LYNKUET
---

---
PATTERN: Cannot execute task USAGE privilege
CATEGORY: Code Failure
ROOT_CAUSE: Task owner role does not have USAGE privilege on the task or its required objects. Typically occurs after role/permission changes or when a task is created by a different role than the one scheduled to execute it.
FIX: GRANT USAGE ON TASK <task_name> TO ROLE <executing_role>. Verify task OWNER matches the WAREHOUSE role. Check if MANAGE GRANTS was revoked recently.
ADDED_BY: rca_memory patterns
ADDED_ON: 2026-06
NOTES: Seen repeatedly for TASK_TMP_PRIVILLAGE_CHECK in ENRICH_V2 schema
---

---
PATTERN: Uncaught exception STATEMENT_ERROR procedure
CATEGORY: Code Failure
ROOT_CAUSE: Stored procedure threw an unhandled SQL error. The procedure lacks TRY/CATCH around its SQL statements so compilation or runtime errors propagate as uncaught STATEMENT_ERROR. Check the procedure definition for the specific failing SQL statement.
FIX: Fetch procedure definition via INFORMATION_SCHEMA.PROCEDURES. Identify failing SQL by cross-referencing the error message with the procedure body. Add proper exception handling (BEGIN...EXCEPTION...END) around critical SQL blocks.
ADDED_BY: rca_memory patterns
ADDED_ON: 2026-07
NOTES: Seen for TASK_NON_PLD_L2, TASK_PROC_FACT_NUBEQA_VOUCHER, TASK_PROC_FACT_IC_LYNKUET, TASK_PROC_IQVIA_LAAD_WEEKLY_L2_LOAD
---



---
PATTERN: NULL L2_TGT_CNT indicates no matching count in L2 for the value 5 found in L3
CATEGORY: Data Quality Failure
ROOT_CAUSE: DQ check '308' fails because live evidence shows 1 offending row(s). Top finding: L3_TGT_CNT=5. Full evidence: 1 offending row(s) identified. Top findings: L3_TGT_CNT=5
FIX: Run diagnostic query to identify the actual distinct SEG_GRP_1_RPT_VAL values in both tables: {{code:SELECT SEG_GRP_1_RPT_VAL, COUNT(*) FROM ANLT_BASE_FACT_SALES_UNALIGNED WHERE PROD_BRAND_NM='KERENDIA' GROUP BY 1}} and {{code:SELECT SEG_GRP_1_RPT_VAL, COUNT(*) FROM DIM_TARGETS_HCP WHERE PROD_BRAND_NM='KERENDIA' GROUP BY 1}}. Compare the lists to identify which segments exist in L3 but not L2 (or vice versa). If L2 is missing segments, trigger a refresh of DIM_TARGETS_HCP from the source CONFIG_KERENDIA table. If L3 has segments not defined in the configuration, investigate why the fact table is being populated with undefined segments and correct the upstream ETL logic.
ADDED_BY: user
ADDED_ON: 2026-07-24
NOTES: 
---

---
PATTERN: Diagnostic query sampled 15 offending rows showing CLAIM_ID duplicates at (PTNT_ID, SOURCE_TYP) grain
CATEGORY: Data Quality Failure
ROOT_CAUSE: 1.6M duplicate CLAIM_ID rows detected in ANLT_BASE_FACT_CLM_BRAND_MKT for Kerendia (prod_brand_cd='50419-540') LAAD claims, with 15 sampled duplicates showing (CLAIM_ID, PTNT_ID, SOURCE_TYP) combinations appearing 2-3 times each.
FIX: 1. Truncate and reload {{table:ANLT_BASE_FACT_CLM_BRAND_MKT}} with deduplication: Add {{code:QUALIFY ROW_NUMBER() OVER (PARTITION BY CLAIM_ID, PTNT_ID, SOURCE_TYP ORDER BY SYS_LOAD_TS DESC) = 1}} to the final SELECT in {{procedure:PROC_ANLT_BASE_FACT_CLM_BRAND_MKT_ONC}} before INSERT. 2. Execute: {{code:DELETE FROM CPH_DB_PROD.ANALYTICS_V2.ANLT_BASE_FACT_CLM_BRAND_MKT WHERE (CLAIM_ID, PTNT_ID, SOURCE_TYP, SYS_LOAD_TS) NOT IN (SELECT CLAIM_ID, PTNT_ID, SOURCE_TYP, MAX(SYS_LOAD_TS) FROM CPH_DB_PROD.ANALYTICS_V2.ANLT_BASE_FACT_CLM_BRAND_MKT WHERE SYS_DATA_SOURCE like '%LAAD%' AND prod_brand_cd='50419-540' GROUP BY 1,2,3)}} to remove stale duplicates. 3. Rerun downstream aggregations to correct inflated metrics.
ADDED_BY: user
ADDED_ON: 2026-08-12
NOTES: 
---

---
PATTERN: Diagnostic sample: 15 concrete duplicate examples retrieved
CATEGORY: Data Quality Failure
ROOT_CAUSE: 1,609,356 duplicate CLAIM_ID records found in ANLT_BASE_FACT_CLM_BRAND_MKT for Kerendia LAAD claims, with specific examples including CLAIM_ID 10606286695306311210 (PTNT_ID 129669788) appearing 2 times and CLAIM_ID 10740259551306491498 (PTNT_ID 2853742987) appearing 3 times.
FIX: Add DISTINCT to the SELECT statement in ANLT_FACT_CUST_MKT_SLSORG_WK_PROC before inserting into ANLT_BASE_FACT_CLM_BRAND_MKT: {{code:SELECT DISTINCT CLM_KER_2.CLAIM_ID, CLM_KER_2.PTNT_ID, CLM_KER_2.SOURCE_TYP, ...}}. Alternatively, add GROUP BY on all non-aggregated columns to collapse duplicates. Then truncate and reload ANLT_BASE_FACT_CLM_BRAND_MKT.
ADDED_BY: user
ADDED_ON: 2026-08-12
NOTES: 
---

---
PATTERN: CLAIM_ID=10606286695306311210, PTNT_ID=129669788, SOURCE_TYP=RX_CLAIMS, COUNT(*)=2
CATEGORY: Data Quality Failure
ROOT_CAUSE: 1,609,356 duplicate CLAIM_ID records exist in ANLT_BASE_FACT_CLM_BRAND_MKT for Kerendia LAAD claims, with individual CLAIM_IDs appearing 2-3 times per PTNT_ID.
FIX: Add a {{code:QUALIFY ROW_NUMBER() OVER (PARTITION BY CLAIM_ID, PTNT_ID, SOURCE_TYP ORDER BY SYS_UPDATE_TS DESC) = 1}} clause to the final INSERT/MERGE statement in {{procedure:ANLT_FACT_CUST_MKT_SLSORG_WK_PROC}} to deduplicate records before loading {{table:ANLT_BASE_FACT_CLM_BRAND_MKT}}. Immediately rerun the procedure with a full historical backfill to correct the 1.6M duplicate records.
ADDED_BY: user
ADDED_ON: 2026-08-12
NOTES: 
---
