# RCA knowledge: SQL code field and delete

Status: Approved 2026-10-29

## Behaviour

- Knowledge rules gain an optional `SQL_CODE` field (fixed / resolution SQL), stored after `FIX` in `rca_knowledge.md`.
- Newlines in `SQL_CODE` are escaped as `\n` so the existing single-line `KEY: value` rule format stays intact.
- Knowledge Base tab and Workbench “Add to Knowledge Base” both expose a SQL Code textarea after Fix / Resolution.
- Knowledge Base tab can delete a rule by index (with confirm). Delete removes that rule from `rca_knowledge.md` and does not leave an empty placeholder.
- Matched domain knowledge passed to the RCA agent includes `SQL_CODE` when present.

## API

- `DELETE /api/rca/knowledge/{index}` — remove rule at index; 404 if out of range.

## Validation

- Append / update round-trip preserves multiline SQL.
- Delete removes the targeted index and shifts later rules.
- Out-of-range delete raises IndexError / 404.
