"""
RCA investigation — <one-line description of the issue>.

Phases
------
1. Schema triage   — confirm object types, columns, freshness
2. Coverage audit  — null / distinct counts for suspect columns
3. Join diagnosis  — classify join outcomes (full / key-miss / value-null)
4. Waterfall       — COALESCE chain coverage projection
5. Samples         — concrete rows to validate conclusions

Requires: connection.py in the same directory (or on sys.path)
"""
import sys
sys.path.insert(0, r"C:\Users\LXGPV\Ish")

from connection import get_snowflake_connection

# ── config ─────────────────────────────────────────────────────────────────────
DATABASE = "CPH_DB_PROD"
SCHEMA   = "ANALYTICS_V2"

# Primary objects under investigation
OBJECT_A = f"{DATABASE}.{SCHEMA}.<TABLE_A>"
OBJECT_B = f"{DATABASE}.{SCHEMA}.<TABLE_B>"

# The column whose NULLs / wrong values triggered the investigation
SUSPECT_COL = "<SUSPECT_COL>"

# Date range / filter (edit as needed)
FILTER_CLAUSE = "WHERE 1=1"   # e.g. "WHERE WK_ID = '20260508'"
# ───────────────────────────────────────────────────────────────────────────────


def q(cur, sql, label, limit=60):
    """Run sql, print results with a banner."""
    sep = "=" * 78
    print(f"\n{sep}\n{label}\n{sep}")
    try:
        cur.execute(sql)
        if not cur.description:
            print("(no result set)")
            return [], []
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        print("  " + " | ".join(cols))
        for r in rows[:limit]:
            print("  " + " | ".join(str(x) for x in r))
        if len(rows) > limit:
            print(f"  ... {len(rows) - limit} more rows")
        return cols, rows
    except Exception as e:
        print(f"  ERROR: {e}")
        return [], []


def main():
    conn = get_snowflake_connection()
    cur  = conn.cursor()
    try:

        # ── Phase 1: Schema triage ─────────────────────────────────────────────
        q(cur, f"""
            SELECT TABLE_TYPE, ROW_COUNT, CREATED, LAST_ALTERED
            FROM {DATABASE}.INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = '{SCHEMA}'
              AND TABLE_NAME IN ('<TABLE_A>', '<TABLE_B>')
        """, "Phase 1a — Object metadata")

        q(cur, f"""
            SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, ORDINAL_POSITION
            FROM {DATABASE}.INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = '{SCHEMA}'
              AND TABLE_NAME   = '<TABLE_A>'
            ORDER BY ORDINAL_POSITION
        """, "Phase 1b — Column inventory: TABLE_A")

        # Uncomment if investigating a view or procedure
        # q(cur, f"SELECT GET_DDL('VIEW', '{OBJECT_A}')", "Phase 1c — DDL")

        # ── Phase 2: Coverage audit ────────────────────────────────────────────
        q(cur, f"""
            SELECT
                COUNT(*)                                            AS total_rows,
                COUNT({SUSPECT_COL})                               AS populated,
                COUNT(*) - COUNT({SUSPECT_COL})                    AS null_count,
                ROUND(100.0 * COUNT({SUSPECT_COL})
                      / NULLIF(COUNT(*), 0), 2)                    AS pct_populated,
                COUNT(DISTINCT {SUSPECT_COL})                      AS cardinality
            FROM {OBJECT_A}
            {FILTER_CLAUSE}
        """, f"Phase 2a — Null audit: {SUSPECT_COL}")

        # Break down by a category to spot patterns
        q(cur, f"""
            SELECT
                <CATEGORY_COL>,
                COUNT(*)                                            AS total,
                COUNT({SUSPECT_COL})                               AS populated,
                ROUND(100.0 * COUNT({SUSPECT_COL})
                      / NULLIF(COUNT(*), 0), 2)                    AS pct
            FROM {OBJECT_A}
            {FILTER_CLAUSE}
            GROUP BY 1 ORDER BY 2 DESC
            LIMIT 30
        """, "Phase 2b — Null breakdown by category")

        # ── Phase 3: Join outcome classification ───────────────────────────────
        q(cur, f"""
            SELECT
                CASE
                    WHEN r.<JOIN_KEY> IS NULL  THEN 'NO_MATCH_IN_RIGHT'
                    WHEN r.<VALUE_COL> IS NULL THEN 'MATCH_BUT_VALUE_NULL'
                    ELSE                            'FULL_MATCH'
                END AS outcome,
                COUNT(*) AS row_count,
                ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct
            FROM {OBJECT_A} l
            LEFT JOIN (
                SELECT DISTINCT <JOIN_KEY>, <VALUE_COL>
                FROM {OBJECT_B}
            ) r ON l.<LEFT_KEY> = r.<JOIN_KEY>
            {FILTER_CLAUSE}
            GROUP BY 1 ORDER BY 2 DESC
        """, "Phase 3a — Join outcome classifier")

        # ── Phase 4: Waterfall recovery ────────────────────────────────────────
        q(cur, f"""
            SELECT
                COUNT(*)                                              AS total_rows,
                COUNT({SUSPECT_COL})                                 AS current_nn,
                COUNT(COALESCE({SUSPECT_COL}, alt1.<VALUE_COL>))     AS after_alt1,
                COUNT(COALESCE({SUSPECT_COL}, alt1.<VALUE_COL>,
                               alt2.<VALUE_COL>))                    AS after_alt2,
                ROUND(100.0 *
                    COUNT(COALESCE({SUSPECT_COL}, alt1.<VALUE_COL>, alt2.<VALUE_COL>))
                    / NULLIF(COUNT(*), 0), 2)                        AS waterfall_pct
            FROM {OBJECT_A} l
            LEFT JOIN (
                SELECT <ALT1_KEY>, MIN(<VALUE_COL>) AS <VALUE_COL>
                FROM <ALT1_TABLE>
                WHERE <VALUE_COL> IS NOT NULL GROUP BY 1
            ) alt1 ON l.<LEFT_KEY> = alt1.<ALT1_KEY>
            LEFT JOIN (
                SELECT <ALT2_KEY>, MIN(<VALUE_COL>) AS <VALUE_COL>
                FROM <ALT2_TABLE>
                WHERE <VALUE_COL> IS NOT NULL GROUP BY 1
            ) alt2 ON l.<LEFT_KEY2> = alt2.<ALT2_KEY>
            {FILTER_CLAUSE}
        """, "Phase 4a — Waterfall coverage projection")

        # ── Phase 5: Sample rows ───────────────────────────────────────────────
        q(cur, f"""
            SELECT *
            FROM {OBJECT_A}
            WHERE {SUSPECT_COL} IS NULL
            {FILTER_CLAUSE.replace('WHERE', 'AND') if 'WHERE' in FILTER_CLAUSE else FILTER_CLAUSE}
            LIMIT 15
        """, "Phase 5a — Sample NULL rows for manual review")

    finally:
        cur.close()
        conn.close()
        print("\n" + "=" * 78)
        print("DONE — remember to append findings to learnings.md")
        print("=" * 78)


if __name__ == "__main__":
    main()
