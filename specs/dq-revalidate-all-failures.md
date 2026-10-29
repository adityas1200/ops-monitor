# DQ revalidate all failures + confirm UX

Status: Approved 2026-10-29

## Behaviour

- Live revalidation (`revalidate=true` on `GET /api/dq/summary`) re-runs rule SQL for **every** latest-per-check row in the selected date range whose recorded status is `FAILED`, `WARNING`, or `DELAYED` — not a fixed cap of 8.
- Dashboard **Revalidate** does not call the API immediately. It opens a confirmation dialog:
  - Heading: `Revalidation Confirmation`
  - Body: warn that all failures in the selected date range will be revalidated, show the applied `From`–`To` dates, and note that the request may take time.
  - Actions: **Cancel** (left) and **Proceed** (right).
- **Cancel** closes the dialog with no API call.
- **Proceed** closes the dialog and calls `/api/dq/summary` with `revalidate=true` (same date range / filters / page as the current DQ view).
- While that request is in flight:
  - Show a loading state **only on the Revalidate button**.
  - Do **not** set the DQ view’s full-page / overlay loading state (table and KPIs stay interactive).
- On success: show a top-right snackbar (light green) with message `Revalidation successful`, then apply the returned summary to the DQ view.
- On failure: show a top-right snackbar (light red) with the error message. Do not clear existing DQ data solely because revalidate failed.
- Snackbars auto-dismiss after a short interval (and can be dismissed manually). Normal Search / Refresh / KPI / pagination loads keep `revalidate=false` and existing overlay loading behaviour.

## Validation

- Backend: `_revalidate_failures` has no hard cap of 8; all failing statuses in the loaded latest-per-check set are attempted (still bounded by the existing DQ fetch hard cap for how many checks are loaded).
- Frontend: Proceed path sets `revalidate=true` without `dqLoading` overlay; Cancel never calls the API.
- Tests cover uncapped failure iteration (or document removal of the 8-cap constant) and that summary with `revalidate=true` still returns the existing response shape.
