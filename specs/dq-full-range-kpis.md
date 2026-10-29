# DQ KPIs and rows use the full date-range source set

Status: Approved 2026-10-29

## Behaviour

- Dashboard Data Quality KPIs (Total, Success, Failed, Warning, Skipped, Running) are computed from every latest QC + subject-area row in the configured summary table for the applied date range.
- The dashboard row payload (`all_checks`) loads that same latest-per-check set for the date range so table filters and pagination can reach the full Total (not a smaller settings cap).
- Queries remain bounded by a fixed hard cap of 10,000 rows (not user-editable).
- Settings → Data Quality no longer exposes a Maximum DQ rows control; the active-query preview no longer advertises a configurable `LIMIT`.
- Existing saved `result_limit` values are ignored for dashboard fetches.
- DQ summary cache key is versioned so older capped payloads are not reused.

## Validation

- Connector fetch SQL uses the hard cap, not settings `result_limit`.
- Summary KPIs match counts over the returned latest-per-check rows when under the hard cap.
- Settings UI and save payload no longer require or edit Maximum DQ rows.
- Tests cover hard-cap fetch, KPI aggregation, and settings field removal compatibility.
