# DQ summary server pagination

Status: Approved 2026-10-29

## Behaviour

- `GET /api/dq/summary` accepts `page` (default `1`) and `page_size` (default `10`, max `500`).
- After status/search filters, only that page of rows is returned in `table_checks`.
- `match_count` is the full filtered count; `page_from` / `page_to` are the 1-based inclusive range of the returned page.
- KPIs remain unfiltered date-range totals.
- Cached inventory stays full server-side; pagination/filter slices happen on the response.
- The HTTP payload does not re-send the full inventory as `all_checks` (chat gets a small failing-row preview instead).
- Dashboard DQ table pagination is server-driven: changing page or page size refetches with those params. Default page size is 10.

## Validation

- Invalid/missing page values coerce to page ≥ 1 and page_size in 1…500.
- Empty filters still return page metadata with `match_count = 0`.
- Tests cover defaults, slicing, and response shape.
