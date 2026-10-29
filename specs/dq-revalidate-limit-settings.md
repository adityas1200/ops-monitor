# DQ revalidate limit settings

Status: Approved 2026-10-29

## Behaviour

- Settings → Data Quality adds a **Revalidate** block with two radio options:
  - **Default** (selected by default): dashboard Revalidate runs live SQL for **every** failing check (`FAILED` / `WARNING` / `DELAYED`) in the selected date range inventory (dynamic — no fixed count).
  - **Custom**: shows a number field **Enter revalidate limit**. Dashboard Revalidate runs live SQL for at most that many failing checks (first N in inventory order).
- Stored under `monitoring.dq`:
  - `revalidate_mode`: `"default"` | `"custom"` (default `"default"`)
  - `revalidate_limit`: integer; used only when mode is `"custom"`
- Custom limit rules (Settings UI + save validation):
  - Digits only while typing (no letters/symbols)
  - Must be an integer in **1…200** inclusive
  - Invalid values show a red error under the field; **Save all settings** is blocked while Custom is selected and the limit is invalid
- After a successful save, the next Dashboard Revalidate uses the saved mode/limit (`revalidate=true` path reads settings).
- Confirmation dialog copy may note Custom mode and the configured limit when applicable.

## Validation

- Backend `DQMonitoringConfig` / `get_dq_monitoring_config` / `validate_monitoring_settings` accept and persist the new fields; invalid custom limits are rejected on save.
- `_revalidate_failures` applies the configured limit when mode is custom; all failing rows in the loaded set when default.
- Tests cover default vs custom limit application and Settings validation bounds (1–200).
