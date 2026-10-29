# Editable DQ result limit

Status: Superseded 2026-10-29 by `specs/dq-full-range-kpis.md`

## Behaviour (historical)

- Settings previously exposed Maximum DQ rows (1–10,000) to bound the dashboard query `LIMIT`.
- That control is removed. Dashboard loads the full latest-per-check set for the date range (hard-capped at 10,000).
