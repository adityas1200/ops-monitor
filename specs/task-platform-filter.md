# Task platform filter

Status: Approved 2026-10-29

## Behaviour

- The Dashboard Tasks tab shows a Platform dropdown.
- Options are All platforms, Snowflake, and AWS Glue.
- Filtering is applied by the backend over the full task inventory for the date range (`platform` query param on `/api/summary`).
- Platform combines with the existing status, search, and date filters.
- The default is All platforms on each page load.
- The empty-state message identifies the selected platform when applicable.
- The Data Quality tab does not show this filter.
