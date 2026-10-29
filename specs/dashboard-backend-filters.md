# Dashboard search and filters run on the backend

Status: Approved 2026-10-29

## Behaviour

- DQ Status search and status filters are applied by `GET /api/dq/summary` (`q`, `status`) over the full latest-per-check set for the date range—not over the current UI page.
- Tasks search, status, and platform filters are applied by `GET /api/summary` (`q`, `status`, `platform`) over the full task set for the date range.
- There is no Status dropdown in the toolbar. KPI cards are the status filter: clicking Success/Failed/… sets `status` on the summary request; Total (or re-clicking the active KPI) clears it to `ALL`.
- KPIs remain counts for the unfiltered date-range inventory.
- `all_checks` / `all_pipelines` stay the full inventory (chat/RCA context).
- `table_checks` / `table_pipelines` are the backend-filtered rows the table renders.
- `match_count` is the filtered row count used for pagination `of N` when filters are active.
- Numeric DQ search (`q` all digits) matches QC ID exactly; otherwise substring match on QC ID, subject area, and check type.
- Toolbar search text and date edits are applied only when the user presses **Search** (or Enter in the search box); they are not sent on every keystroke.

## Validation

- Filter helpers cover status aliases, numeric QC ID, text search, and platform.
- Cached DQ payloads still apply `q`/`status` on read without requiring a Snowflake round-trip.
- Frontend table views do not re-filter by search/status/platform.
