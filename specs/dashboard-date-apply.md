# Explicit dashboard Search apply

Status: Approved 2026-10-29 (updated: Search replaces Apply dates)

## Behaviour

- Editing the search box, From, or To does not reload Tasks or Data Quality.
- A **Search** button applies the search text and both dates together and reloads dashboard data.
- Search is disabled when either date is missing, From is after To, values are unchanged, or a load is in progress.
- Pressing Enter in the search box runs the same apply as Search.
- Refresh is an icon button (tooltip: Refresh) and reloads using the currently applied search text and date range.
- KPI status and platform filters still apply immediately (they are not gated by Search).
