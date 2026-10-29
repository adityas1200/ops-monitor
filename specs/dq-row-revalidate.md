# DQ row actions + single-check revalidate

Status: Approved 2026-10-29

## Behaviour

- DQ Status table has an **Actions** column with icon buttons (tooltips), left-to-right:
  1. **View Details** — eye icon (all rows)
  2. **Run RCA** — investigation icon; enabled only when status is `FAILED` or `WARNING` (disabled otherwise)
  3. **Revalidate** — refresh icon; enabled only when status is `FAILED` or `WARNING` (disabled otherwise)
- Single-check revalidate: `POST /api/dq/revalidate` with body `{ qc_id, subject_area }` (and optional `id` for client row match).
- Backend runs live rule SQL for that QC + subject area (same path as bulk `revalidate_check`), returns `{ qc_id, subject_area, status, live_status, recorded_status, stale, error, pass_count, fail_count, live_checked, ... }`.
- Dashboard updates that table row from the response (no full-page overlay); button shows a busy state for that row only. Success/error snackbar as for bulk revalidate where useful.

## Validation

- Missing `qc_id` → 400.
- Rule not found / execute failure reported in payload (not silent).
- Tests cover request shape and connector single-check path.
