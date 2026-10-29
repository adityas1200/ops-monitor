# Paginated shared DataTable

Status: Approved 2026-10-29 (updated with full-range DQ)

## Behaviour

- Dashboard Tasks and Data Quality use a shared `DataTable` for grid chrome (card shell, header sort cues, optional checkboxes, pagination footer).
- Search, status, and platform filters run against the full dataset returned by the backend for the applied date range. Filtering is never limited to the currently visible page.
- Pagination slices the already-filtered row set for display.
- The footer range uses the selected page size for the `start-end` slice and the filtered/source total for `of N`.
- Per-page options are `10`, `25`, `50`, `100`, and `500` (last option is always 500).
- Checkboxes are optional via prop: hidden, enabled, or disabled. Default is hidden so existing row actions stay unchanged.

## Validation

- Page-size options always end with 500.
- Filtered empty states still show the same guidance copy as today.
