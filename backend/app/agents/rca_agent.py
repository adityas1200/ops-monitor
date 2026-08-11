from __future__ import annotations
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from app.agents.base import BaseAgent
from app.agents.monitoring_agent import MonitoringAgent
from app.connectors.dq_connector import DQConnector
from app.connectors.lineage_service import (LineageService, index_runs_by_task_key,
                                            parse_tables_from_text)

# ── Skill failure categories (matches rca.md exactly) ─────────────────────────
CATEGORY_CODE        = "Code Failure"
CATEGORY_DQ          = "Data Quality Failure"
CATEGORY_DEPENDENCY  = "Dependency Failure"
CATEGORY_INFRA       = "Infrastructure Failure"
CATEGORY_AVAIL       = "Data Availability Failure"
CATEGORY_UNKNOWN     = "Unknown Failure"


def _normalize_rca_dates(date_from: Optional[str] = None, date_to: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """Default RCA collect window to last 14 days ending today (UTC) when dates omitted."""
    if date_from and date_to:
        return date_from, date_to
    today = datetime.now(timezone.utc).date()
    df = date_from or (today - timedelta(days=14)).isoformat() + "T00:00:00+00:00"
    dt = date_to or today.isoformat() + "T23:59:59+00:00"
    return df, dt


def _collect_pipelines_for_rca(pipeline_id: str, date_from: Optional[str] = None,
                               date_to: Optional[str] = None) -> List[Dict[str, Any]]:
    df, dt = _normalize_rca_dates(date_from, date_to)
    pipelines = MonitoringAgent().collect(df, dt)
    if any(p.get("id") == pipeline_id for p in pipelines):
        return pipelines
    # Fallback: unscoped collect if target missing from window
    return MonitoringAgent().collect()


def _classify(error: Optional[str]) -> str:
    """Classify failure into one of the 6 categories defined in rca.md."""
    e = (error or "").lower()
    # Infrastructure — resource / compute / timeout
    if any(k in e for k in (
        "outofmemory", "heap", "timeout", "throughput", "throttle",
        "executor lost", "warehouse", "capacity", "spillage",
    )):
        return CATEGORY_INFRA
    # Data Quality — validation / grain / null / schema mismatch
    if any(k in e for k in (
        "nan", "null", "cast", "numeric value", "schema mismatch",
        "duplicate", "grain", "validation", "threshold", "reconcil",
        "record count", "dq check",
    )):
        return CATEGORY_DQ
    # Code — SQL compilation / syntax / permission / procedure / Snowpark
    if any(k in e for k in (
        "sql compilation", "syntax", "py4j", "line ",
        "invalid identifier", "object not found", "procedure exception",
        "snowpark", "access denied", "permission", "not authorized",
        "forbidden", "unexpected token", "undefined",
    )):
        return CATEGORY_CODE
    # Dependency — upstream task / predecessor
    if any(k in e for k in (
        "predecessor", "upstream", "missing source", "not succeeded",
        "taskfailed", "dependency", "parent task",
    )):
        return CATEGORY_DEPENDENCY
    # Data Availability — source not loaded / stage empty
    if any(k in e for k in (
        "no data", "empty", "not loaded", "not arrived", "unavailable",
        "stage", "share", "no rows", "no file",
    )):
        return CATEGORY_AVAIL
    return CATEGORY_UNKNOWN


def _confidence_level(score: float) -> str:
    """Map decimal confidence to the skill's High / Medium / Low labels."""
    if score >= 0.85:
        return "High"
    if score >= 0.65:
        return "Medium"
    return "Low"


def _lineage_text(nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]]) -> str:
    """
    Convert graph {nodes, edges} to the skill's text arrow format:
        NODE_A
        ↓
        [FAILED] NODE_B
    """
    if not nodes:
        return ""
    # Topological sort
    by_id = {n["id"]: n for n in nodes}
    in_deg: Dict[str, int] = {n["id"]: 0 for n in nodes}
    children: Dict[str, List[str]] = {n["id"]: [] for n in nodes}
    for e in edges:
        if e["from"] in by_id and e["to"] in by_id:
            children[e["from"]].append(e["to"])
            in_deg[e["to"]] += 1

    queue = [nid for nid, d in in_deg.items() if d == 0]
    ordered: List[str] = []
    visited: Set[str] = set()
    while queue:
        cur = queue.pop(0)
        if cur in visited:
            continue
        visited.add(cur)
        ordered.append(cur)
        for child in children.get(cur, []):
            in_deg[child] -= 1
            if in_deg[child] == 0:
                queue.append(child)
    # Any nodes not reached (cycles)
    for nid in by_id:
        if nid not in visited:
            ordered.append(nid)

    lines: List[str] = []
    for i, nid in enumerate(ordered):
        node = by_id[nid]
        label = node.get("label") or node.get("name") or nid
        # strip schema prefix for readability (keep last 2 parts)
        parts = label.replace("SF Task: ", "").split(".")
        short = ".".join(parts[-2:]) if len(parts) > 2 else label
        prefix = "[FAILED] " if node.get("state") in ("root_cause", "failed") else ""
        lines.append(f"{prefix}{short}")
        if i < len(ordered) - 1:
            lines.append("↓")
    return "\n".join(lines)


class RCAAgent(BaseAgent):
    name = "rca"
    skill_file = "rca.md"

    def __init__(self):
        super().__init__()
        self._knowledge = self._load_knowledge_file()

    def _load_knowledge_file(self) -> str:
        """Load the user-contributed rca_knowledge.md alongside the main skill."""
        from app.core.config import SKILLS_DIR
        p = SKILLS_DIR / "rca_knowledge.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def _recall_by_tables(self, tables: List[str]) -> List[Dict[str, Any]]:
        """Find past RCA memories that involve the same tables."""
        if not tables:
            return []
        table_set = set(tables)
        scored = []
        for record in self.memory.all():
            past_tables = set(record.get("tables_involved") or [])
            overlap = table_set & past_tables
            if overlap:
                scored.append((len(overlap), record))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored[:5]]

    def _match_knowledge_rules(self, error: Optional[str], task_name: Optional[str],
                               tables: List[str]) -> List[Dict[str, str]]:
        """Match user-contributed knowledge rules against current failure context."""
        if not self._knowledge:
            return []
        rules: List[Dict[str, str]] = []
        current_block: Dict[str, str] = {}
        for line in self._knowledge.split("\n"):
            stripped = line.strip()
            if stripped == "---":
                if current_block.get("PATTERN"):
                    rules.append(current_block)
                current_block = {}
                continue
            if ":" in stripped and stripped.split(":")[0].strip().upper() in (
                "PATTERN", "CATEGORY", "ROOT_CAUSE", "FIX", "ADDED_BY", "ADDED_ON", "NOTES"
            ):
                key, _, val = stripped.partition(":")
                current_block[key.strip().upper()] = val.strip()
        if current_block.get("PATTERN"):
            rules.append(current_block)

        matched = []
        context_lower = " ".join([
            (error or ""), (task_name or ""), " ".join(tables)
        ]).lower()
        for rule in rules:
            pattern = (rule.get("PATTERN") or "").lower()
            if not pattern:
                continue
            words = pattern.split()
            match_count = sum(1 for w in words if w in context_lower)
            if match_count >= max(2, len(words) // 2):
                rule["_match_score"] = match_count
                matched.append(rule)
        matched.sort(key=lambda r: r.get("_match_score", 0), reverse=True)
        return matched[:5]

    def update_resolution(self, pipeline_id: str, resolution: str) -> None:
        """Patch the most recent RCA memory for this pipeline with the resolution applied."""
        all_mem = self.memory.all()
        for record in reversed(all_mem):
            if record.get("pipeline_id") == pipeline_id:
                record["resolution_applied"] = resolution
                from app.core.config import MEMORY_DIR
                import json
                (MEMORY_DIR / self.memory.file).write_text(
                    json.dumps(all_mem, indent=2, default=str), encoding="utf-8")
                break

    # ── LLM-first methods ────────────────────────────────────────────────────

    def _generate_narrative(self, result: Dict[str, Any]) -> Optional[str]:
        """Generate a natural-language expert briefing for the chat reply."""
        if not self.harness.available:
            return None
        system = (
            "You are a senior data operations analyst briefing a colleague on a failure investigation. "
            "Be direct, specific, and expert. Reference actual task names, table names, and error messages. "
            "Use markdown: **bold** for key terms, `code` for SQL identifiers, bullet points for lists. "
            "Keep it to 4-6 sentences. End with a clear recommended next action. "
            "Do NOT use generic phrases like 'I found the issue' — jump straight to the findings."
        )
        messages = [{"role": "user", "content": json.dumps({
            "task_name": result.get("root_cause_name"),
            "category": result.get("category"),
            "summary": result.get("summary"),
            "confidence": result.get("confidence_level"),
            "error": (result.get("evidence") or [""])[0][:300],
            "impact_summary": result.get("impact_summary"),
            "remediation_immediate": (result.get("remediation") or {}).get("immediate_fix"),
            "downstream_count": len(result.get("impacted_nodes") or []),
            "tables": [t.get("table") for t in (result.get("affected_tables") or [])[:5]],
        }, default=str)}]
        return self.harness.speak(system, messages, max_tokens=800)

    def _llm_remediation(self, category: str, target: Dict[str, Any],
                         root: Dict[str, Any], extra_context: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Generate specific remediation using LLM with actual failure context."""
        if not self.harness.available:
            return None
        system = (
            "You are a Snowflake/data-ops expert. Given a failure, produce SPECIFIC remediation. "
            "Reference exact object names, SQL patterns, and concrete actions. "
            "Do NOT give generic advice — every sentence must be grounded in the failure details. "
            "Output raw JSON only — NO markdown code fences. Start with { and end with }. "
            "Use these EXACT keys: immediate_fix, permanent_fix, monitoring_recommendation. "
            "Keep each value to 1-2 sentences."
        )
        user_payload = json.dumps({
            "category": category,
            "task_name": target.get("name"),
            "root_cause_task": root.get("name"),
            "error": (root.get("error") or target.get("error") or "")[:500],
            "tables": target.get("tables", [])[:5],
            "platform": target.get("platform"),
            "warehouse": target.get("warehouse"),
            "extra_context": extra_context,
        }, default=str)
        txt = self.harness.reason(system, user_payload, max_tokens=1200)
        if not txt:
            return None
        return self._extract_json(txt)
    # ── Failure-evidence pipeline (generic for all DQ failures) ────────────────
    # Goal: for EVERY failed check, materialize concrete offending rows first,
    # then force the root cause from that evidence. Do not rely on per-QC fixes.

    _VERDICT_COLS = {"RESULT", "QC_RESULT", "PASS_FAIL", "CHECK_RESULT", "DQ_RESULT",
                     "STATUS", "PASS", "FAIL", "IS_PASS"}
    _COUNT_COLS = {"RESULT_COUNT", "RESULT_CNT", "FAIL_COUNT", "FAILED_COUNT",
                   "MISMATCH_COUNT", "VIOLATION_COUNT", "EXCEPTION_COUNT", "CNT", "COUNT",
                   "TOTAL", "TOTAL_COUNT", "DEVIATION", "DIFF", "DIFFERENCE",
                   "SOURCE_CNT", "TARGET_CNT", "L2", "L3", "L3_SALES_ALIGNED"}
    _META_COLS = {"TABLE_NAME", "METRIC", "SOURCE", "TARGET", "COMMENTS", "COMMENT",
                  "DASHBOARD_NAME", "SUBJECT_AREA", "QC_ID", "CHECK_TYPE", "FREQUENCY"}
    _PAIR_COLS = (
        ("SOURCE_CNT", "TARGET_CNT"),
        ("L2", "L3"),
        ("L2", "L3_SALES_ALIGNED"),
        ("L3_TGT_CNT", "L2_TGT_CNT"),
        ("CURR_CALLS", "PREV_CALLS"),
        ("TOTAL_HCP_CALLS", "PREV_CNT"),
    )
    _GRAIN_RE = re.compile(
        r"(SEG_|PRODUCT|BRAND|CLAIM|CUST|TERR|NAME|CODE|KEY|VAL|GRP|ORG|REGION|"
        r"_ID$|^ID$|HCP|PTNT|NPI)",
        re.IGNORECASE,
    )
    _VAGUE_PHRASES = (
        "data mismatch", "semantic data quality", "populations do not align",
        "without the diagnostic", "cannot be determined", "could result from",
        "likely stems", "possible causes", "one product brand has a discrepancy",
    )

    def _norm_cols(self, exec_result: Optional[Dict[str, Any]]) -> List[str]:
        return [str(c).strip().upper() for c in ((exec_result or {}).get("columns") or [])]

    def _verdict_only(self, exec_result: Optional[Dict[str, Any]]) -> bool:
        """True when execution proves failure but hides WHY (Pass/Fail, bare count, 1 row)."""
        if not exec_result or not exec_result.get("executed"):
            return False
        rows = exec_result.get("rows") or []
        cols = self._norm_cols(exec_result)
        if not cols:
            return len(rows) <= 1
        if any(c in self._VERDICT_COLS for c in cols):
            return True
        summary_cols = self._VERDICT_COLS | self._COUNT_COLS
        if all(c in summary_cols for c in cols):
            return True
        return len(rows) <= 1

    def _has_grain_column(self, cols: List[str]) -> bool:
        skip = self._VERDICT_COLS | self._COUNT_COLS | self._META_COLS
        return any(c not in skip and self._GRAIN_RE.search(c) for c in cols)

    def _needs_diagnostic(self, exec_result: Optional[Dict[str, Any]]) -> bool:
        """True when live results do not already name the offender with grain keys."""
        if self._verdict_only(exec_result):
            return True
        if not exec_result or not exec_result.get("executed"):
            return False
        rows = exec_result.get("rows") or []
        cols = self._norm_cols(exec_result)
        if not rows or not cols:
            return True
        if not self._has_grain_column(cols):
            return True
        # Has grain but no identifiable failing row → still need a filtered drill-down.
        failing = self._extract_failing_rows(exec_result, limit=1)
        return not bool(failing)

    @staticmethod
    def _row_upper(row: Dict[str, Any]) -> Dict[str, Any]:
        return {str(k).strip().upper(): v for k, v in row.items()}

    def _row_looks_failing(self, row: Dict[str, Any]) -> bool:
        """Heuristic: mark a detail row as a failure for sample prioritization."""
        if not isinstance(row, dict):
            return False
        upper = self._row_upper(row)
        for vk in self._VERDICT_COLS:
            if vk in upper and upper[vk] is not None:
                if str(upper[vk]).strip().upper() in ("FAIL", "FAILED", "ERROR", "FALSE", "N", "NO"):
                    return True
        for a, b in self._PAIR_COLS:
            if a in upper and b in upper and upper[a] is not None and upper[b] is not None:
                try:
                    if abs(float(upper[a]) - float(upper[b])) > 1e-9:
                        return True
                except (TypeError, ValueError):
                    pass
        # Generic numeric left/right pair: any *CNT / L2/L3 style leftovers.
        numeric = {}
        for k, v in upper.items():
            if k in self._META_COLS | self._VERDICT_COLS:
                continue
            try:
                numeric[k] = float(v)
            except (TypeError, ValueError):
                continue
        if "DEVIATION" in numeric and abs(numeric["DEVIATION"]) > 1e-9:
            return True
        if "DIFF" in numeric and abs(numeric["DIFF"]) > 1e-9:
            return True
        # Any two measure-like columns that disagree.
        measure_keys = [k for k in numeric if any(tok in k for tok in (
            "CNT", "COUNT", "NBRX", "TOTAL", "CALL", "L2", "L3", "SOURCE", "TARGET"))]
        if len(measure_keys) >= 2:
            vals = [numeric[k] for k in measure_keys[:4]]
            if max(vals) - min(vals) > 1e-9:
                # Only treat as failing when an explicit pair pattern exists among them
                for a, b in self._PAIR_COLS:
                    if a in numeric and b in numeric and abs(numeric[a] - numeric[b]) > 1e-9:
                        return True
        return False

    def _extract_failing_rows(self, exec_result: Optional[Dict[str, Any]],
                             limit: int = 15) -> List[Dict[str, Any]]:
        """Return offending rows only. If the rule returns only violators, keep all."""
        rows = [r for r in ((exec_result or {}).get("rows") or []) if isinstance(r, dict)]
        if not rows:
            return []
        cols = self._norm_cols(exec_result)
        failing = [r for r in rows if self._row_looks_failing(r)]
        if failing:
            return failing[:limit]
        # Detail queries that only return violators (duplicates, exceptions) have no
        # RESULT/pair columns — treat every returned row as an offender.
        has_verdict = any(c in self._VERDICT_COLS for c in cols)
        has_pair = any(a in cols and b in cols for a, b in self._PAIR_COLS)
        if not has_verdict and not has_pair:
            return rows[:limit]
        return []

    def _prefer_failing_rows(self, exec_result: Optional[Dict[str, Any]],
                            limit: int = 10) -> List[Dict[str, Any]]:
        rows = [r for r in ((exec_result or {}).get("rows") or []) if isinstance(r, dict)]
        failing = self._extract_failing_rows(exec_result, limit=limit)
        if not failing:
            return rows[:limit]
        other = [r for r in rows if r not in failing]
        return (failing + other)[:limit]

    def _grain_cols_for(self, cols: List[str]) -> List[str]:
        skip = self._VERDICT_COLS | self._COUNT_COLS | self._META_COLS
        return [c for c in cols if c not in skip and self._GRAIN_RE.search(c)]

    def _summarize_evidence_rows(self, rows: List[Dict[str, Any]],
                                 columns: List[str]) -> Dict[str, Any]:
        """Build mandatory facts the LLM must cite — generic across check shapes."""
        cols = [str(c).strip().upper() for c in columns]
        grain_cols = self._grain_cols_for(cols)
        facts: List[str] = []
        key_values: List[str] = []

        for row in rows[:8]:
            upper = self._row_upper(row)
            grain_bits = []
            for g in grain_cols:
                if g in upper and upper[g] is not None:
                    grain_bits.append(f"{g}={upper[g]}")
                    key_values.append(str(upper[g]))
            measure_bits = []
            for a, b in self._PAIR_COLS:
                if a in upper and b in upper and upper[a] is not None and upper[b] is not None:
                    try:
                        av, bv = float(upper[a]), float(upper[b])
                        diff = av - bv
                        measure_bits.append(f"{a}={av:g} vs {b}={bv:g} (diff={diff:g})")
                    except (TypeError, ValueError):
                        measure_bits.append(f"{a}={upper[a]} vs {b}={upper[b]}")
            if "DEVIATION" in upper and upper["DEVIATION"] is not None:
                measure_bits.append(f"DEVIATION={upper['DEVIATION']}")
            # Duplicate-style count columns
            for ck in ("COUNT", "CNT", "OCCURRENCE_COUNT", "RESULT_COUNT"):
                if ck in upper and upper[ck] is not None:
                    measure_bits.append(f"{ck}={upper[ck]}")
                    break
            if not measure_bits:
                # Fall back to any non-meta values
                for k, v in upper.items():
                    if k in self._META_COLS | self._VERDICT_COLS or k in grain_cols:
                        continue
                    if v is not None:
                        measure_bits.append(f"{k}={v}")
                    if len(measure_bits) >= 4:
                        break
            piece = ", ".join(grain_bits + measure_bits) if (grain_bits or measure_bits) else str(upper)
            facts.append(piece)

        summary = (
            f"{len(rows)} offending row(s) identified. "
            + ("Top findings: " + " | ".join(facts) if facts else "See sample_rows.")
        )
        return {
            "summary": summary,
            "facts": facts,
            "key_values": key_values,
            "grain_columns": grain_cols,
            "offending_row_count": len(rows),
        }

    def _evidence_pack(self, rows: List[Dict[str, Any]], columns: List[Any],
                       source: str, diagnostic_sql: Optional[str] = None) -> Dict[str, Any]:
        cols = [str(c) for c in columns]
        failing = rows
        # Prefer explicitly failing subset when present.
        tmp = {"executed": True, "rows": rows, "columns": cols}
        extracted = self._extract_failing_rows(tmp, limit=15)
        if extracted:
            failing = extracted
        summary = self._summarize_evidence_rows(failing, cols)
        return {
            "source": source,
            "diagnostic_sql": diagnostic_sql,
            "row_count": len(failing),
            "columns": cols,
            "sample_rows": failing[:15],
            "evidence_summary": summary["summary"],
            "evidence_facts": summary["facts"],
            "key_values": summary["key_values"],
            "grain_columns": summary["grain_columns"],
        }

    def _generate_diagnostic_sql(self, check: Dict[str, Any],
                                 rule_sql: Optional[str]) -> Optional[str]:
        """LLM rewrite of a check into a read-only query that names offenders."""
        if not self.harness.available or not rule_sql:
            return None
        system = (
            "You are a Snowflake data-ops investigator. Rewrite the given DQ check SQL into ONE "
            "read-only diagnostic SELECT that returns ONLY the failing rows WITH identifying keys "
            "(product/segment/id/claim/etc.) and both sides' values or occurrence counts.\n"
            "Adapt shape: comparison→grain+both sides+diff, mismatches only; "
            "duplicate→keys+COUNT HAVING COUNT(*)>1; missing/flow→missing keys+side; "
            "threshold/trend→breaching rows+metric/deviation.\n"
            "If a CTE already has the grain but the outer SELECT drops it, SELECT * FROM that CTE "
            "filtered to non-zero deviation / unequal sides, ordered by abs(diff) DESC.\n"
            "STRICT: single SELECT/WITH; same tables/filters/joins; no Pass/Fail collapse; "
            "no writes/DDL; no semicolons; LIMIT 100. "
            "Raw JSON only: {\"sql\": \"...\", \"explanation\": \"...\"}."
        )
        payload = json.dumps({
            "qc_id": check.get("name"),
            "subject_area": check.get("table_name"),
            "check_type": check.get("column_name"),
            "check_description": check.get("error"),
            "check_sql": (rule_sql or "")[:3000],
        }, default=str)
        txt = self.harness.reason(system, payload, max_tokens=900)
        if not txt:
            return None
        parsed = self._extract_json(txt)
        if parsed and parsed.get("sql"):
            return str(parsed["sql"]).strip()
        return None

    def _fallback_diagnostic_sql(self, rule_sql: Optional[str]) -> Optional[str]:
        """Deterministic unwrap of common wrappers that hide offenders."""
        if not rule_sql:
            return None
        s = rule_sql.strip().rstrip(";").strip()
        s_code = re.sub(r"(?m)^\s*--.*?$", "", s).strip()

        # 1) Single-CTE wrapper → SELECT * FROM cte [WHERE deviation<>0]
        m = re.match(r"(?is)^\s*with\s+(\w+)\s+as\s*\(", s_code)
        if m:
            name = m.group(1)
            start = m.end()
            depth, i = 1, start
            while i < len(s_code) and depth:
                ch = s_code[i]
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                i += 1
            rest = s_code[i:].strip()
            if re.match(rf"(?is)^select\b.+\bfrom\s+{re.escape(name)}\b\s*$", rest):
                if not re.match(rf"(?is)^select\s+\*\s+from\s+{re.escape(name)}\b", rest):
                    cte_body = s_code[start:i - 1]
                    where = ""
                    order = ""
                    if re.search(r"(?i)\bdeviation\b", cte_body):
                        where = "\nWHERE ABS(COALESCE(DEVIATION, 0)) > 0"
                        order = "\nORDER BY ABS(COALESCE(DEVIATION, 0)) DESC NULLS LAST"
                    return (f"{s_code[:i].strip()}\nSELECT * FROM {name}"
                            f"{where}{order}\nLIMIT 100")

        # 2) Pass/Fail CASE WHEN wrapper → SELECT * FROM <from-clause>
        m = re.search(
            r"(?is)^(.*?)\bselect\s+case\s+when\b.+?\bend(?:\s+as\s+\w+)?\s+from\b(.+)$",
            s_code,
        )
        if m:
            prefix = m.group(1).strip()
            rest = re.sub(r"(?is)\border\s+by\s+.+$", "", m.group(2).strip()).strip()
            body = f"{prefix}\nSELECT * FROM {rest}".strip() if prefix else f"SELECT * FROM {rest}"
            if not re.search(r"(?i)\blimit\s+\d+", body):
                body += "\nLIMIT 100"
            return body
        return None

    def _run_diagnostic_candidates(self, dq_conn: DQConnector, check: Dict[str, Any],
                                   dq_rule: Dict[str, Any], *,
                                   allow_llm: bool = True) -> Tuple[Optional[Dict[str, Any]],
                                                                    Optional[str]]:
        """Try deterministic fallback first, then LLM rewrite. Prefer packs with grain."""
        rule_sql = dq_rule.get("SQL_CODE")
        candidates: List[str] = []
        fb = self._fallback_diagnostic_sql(rule_sql)
        if fb:
            candidates.append(fb)
        if allow_llm:
            llm_sql = self._generate_diagnostic_sql(check, rule_sql)
            if llm_sql and llm_sql not in candidates:
                candidates.append(llm_sql)

        last_err = None
        best_without_grain: Optional[Dict[str, Any]] = None
        for diag_sql in candidates:
            diag_exec = dq_conn.run_diagnostic_sql(
                diag_sql, dq_rule, context_sql=rule_sql)
            if diag_exec and diag_exec.get("executed") and diag_exec.get("rows"):
                pack = self._evidence_pack(
                    diag_exec.get("rows") or [],
                    diag_exec.get("columns") or [],
                    source="diagnostic_query",
                    diagnostic_sql=diag_exec.get("diagnostic_sql") or diag_sql,
                )
                if self._has_grain_column([str(c).upper() for c in pack["columns"]]):
                    return pack, None
                if best_without_grain is None:
                    best_without_grain = pack
            elif diag_exec and diag_exec.get("error"):
                last_err = diag_exec.get("error")
        return best_without_grain, last_err

    def _collect_dq_failure_evidence(self, dq_conn: DQConnector, check: Dict[str, Any],
                                     dq_rule: Optional[Dict[str, Any]],
                                     dq_execution_result: Optional[Dict[str, Any]],
                                     live_status: Optional[str], *,
                                     allow_diagnostic_llm: bool = True) -> Tuple[Optional[Dict[str, Any]],
                                                                                 Optional[str]]:
        """Generic evidence collector for ANY failed DQ check.

        1) Reuse live execution rows when they already name offenders (grain + fail rows).
        2) Otherwise run diagnostic drill-down (deterministic unwrap, then LLM rewrite).
        Returns (evidence_pack, warning).
        """
        if live_status != "FAILED":
            return None, None

        # Path A: live results already identify offenders.
        if (dq_execution_result and dq_execution_result.get("executed")
                and not self._needs_diagnostic(dq_execution_result)):
            failing = self._extract_failing_rows(dq_execution_result, limit=15)
            if failing:
                return self._evidence_pack(
                    failing,
                    dq_execution_result.get("columns") or [],
                    source="live_execution_failing_rows",
                ), None

        # Path B: drill down from the rule SQL.
        if dq_rule and dq_rule.get("SQL_CODE"):
            pack, err = self._run_diagnostic_candidates(
                dq_conn, check, dq_rule, allow_llm=allow_diagnostic_llm)
            if pack:
                return pack, None
            return None, err

        return None, "No rule SQL available for diagnostic drill-down"

    def _llm_cites_evidence(self, text: str, evidence: Optional[Dict[str, Any]]) -> bool:
        """True when LLM narrative mentions at least one concrete key/value from evidence."""
        if not text or not evidence:
            return False
        t = text.lower()
        for kv in evidence.get("key_values") or []:
            if kv and str(kv).lower() in t:
                return True
        for fact in evidence.get("evidence_facts") or []:
            for part in str(fact).split(","):
                if "=" in part:
                    val = part.split("=", 1)[1].strip().split()[0]
                    if len(val) >= 2 and val.lower() in t:
                        return True
        return False

    def _deterministic_root_cause(self, check: Dict[str, Any],
                                  evidence: Dict[str, Any],
                                  tables: List[str]) -> Dict[str, Any]:
        """Build an evidence-grounded root cause without the LLM — used as seed/fallback."""
        facts = evidence.get("evidence_facts") or []
        summary = evidence.get("evidence_summary") or ""
        top = facts[0] if facts else summary
        n = evidence.get("row_count") or len(evidence.get("sample_rows") or [])
        label = check.get("name") or "this check"
        explanation = (
            f"DQ check '{label}' fails because live evidence shows {n} offending row(s). "
            f"Top finding: {top}. "
            f"Full evidence: {summary}"
        )
        entities = [{"name": t, "type": "table"} for t in tables[:5]]
        for g in evidence.get("grain_columns") or []:
            entities.append({"name": g, "type": "column"})
        return {
            "explanation": explanation,
            "business_explanation": (
                f"A data quality check failed with {n} concrete exception(s). "
                f"Key finding: {top}."
            ),
            "technical_explanation": summary or explanation,
            "entities": entities,
            "code_snippets": [],
            "comparison": {
                "expected": "Compared groups/keys should match (or meet the check threshold).",
                "actual": top or summary,
            },
        }

    # ── Private helpers ────────────────────────────────────────────────────────
    # ── Private helpers ────────────────────────────────────────────────────────

    def _enrich_for_rca(self, lineage: LineageService, record: Dict[str, Any]) -> Dict[str, Any]:
        return lineage.enrich_pipeline(record, resolve_tables=True)

    def _walk_upstream_root(self, target: Dict[str, Any],
                            run_by_key: Dict[str, Dict[str, Any]],
                            graph: Dict[str, Dict[str, Any]]) -> str:
        """Step 3 (Scenario 3): trace to the earliest failing upstream ancestor."""
        tkey = target.get("task_key") or target["id"]
        visited: Set[str] = set()
        root = tkey
        stack = [tkey]
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            bad_ups = [
                u for u in graph.get(cur, {}).get("upstream", [])
                if run_by_key.get(u, {}).get("status") in ("FAILED", "DELAYED")
            ]
            if bad_ups:
                root = bad_ups[0]
                stack.extend(bad_ups)
        return root

    def _downstream_impact(self, root_key: str,
                           graph: Dict[str, Dict[str, Any]]) -> List[str]:
        """Walk forward from root to collect all impacted task keys."""
        impacted: Set[str] = set()
        stack = list(graph.get(root_key, {}).get("downstream", []))
        while stack:
            cur = stack.pop()
            if cur in impacted:
                continue
            impacted.add(cur)
            stack.extend(graph.get(cur, {}).get("downstream", []))
        return list(impacted)

    def _relevant_task_keys(self, target_key: str, root_key: str,
                            impacted: List[str],
                            graph: Dict[str, Dict[str, Any]]) -> Set[str]:
        relevant = {target_key, root_key, *impacted}
        stack = [target_key]
        while stack:
            cur = stack.pop()
            for u in graph.get(cur, {}).get("upstream", []):
                if u not in relevant:
                    relevant.add(u)
                    stack.append(u)
        return relevant

    def _build_lineage(self, target_key: str, root_key: str, impacted: List[str],
                       graph: Dict[str, Dict[str, Any]],
                       run_by_key: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        relevant = self._relevant_task_keys(target_key, root_key, impacted, graph)
        nodes, edges = [], []
        for tkey in relevant:
            meta = graph.get(tkey, {})
            run = run_by_key.get(tkey, {})
            if not meta and not run:
                continue
            if tkey == root_key:
                state = "root_cause"
            elif run.get("status") in ("FAILED", "DELAYED"):
                state = "failed"
            elif tkey in impacted:
                state = "impacted"
            else:
                state = "healthy"
            label = run.get("name") or meta.get("name") or tkey
            platform = run.get("platform") or meta.get("platform") or "snowflake"
            nodes.append({"id": tkey, "label": label, "platform": platform, "state": state})
            for u in meta.get("upstream", []):
                if u in relevant:
                    edges.append({"from": u, "to": tkey})
        return {"nodes": nodes, "edges": edges}

    def _build_affected_tables(self, target: Dict[str, Any], root: Dict[str, Any],
                               dq_related: List[Dict[str, Any]],
                               table_roles: Dict[str, str]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        seen: Set[str] = set()

        def add(table: str, source: str, role: str, related: str, status: str) -> None:
            if not table or table in seen:
                return
            seen.add(table)
            rows.append({"table": table, "source": source, "role": role,
                         "related_entity": related, "status": status})

        for t in root.get("tables") or []:
            add(t, "task", "root_cause", root.get("name", ""), root.get("status", "FAILED"))
        for t in target.get("tables") or []:
            role = "failed_task" if target["id"] != root.get("id") else "root_cause"
            add(t, "task", role, target.get("name", ""), target.get("status", "FAILED"))
        for check in dq_related:
            for t in check.get("tables") or []:
                add(t, "dq", "dq_failed", check.get("name", ""), check.get("status", "FAILED"))
        for tbl, role in table_roles.items():
            if tbl not in seen:
                add(tbl, "lineage", role, "", role.replace("_", " ").upper())
        return rows

    def _table_roles_from_context(self, target: Dict[str, Any], root: Dict[str, Any],
                                  dq_related: List[Dict[str, Any]]) -> Dict[str, str]:
        roles: Dict[str, str] = {}
        for t in root.get("tables") or []:
            roles[t] = "root_cause"
        for t in target.get("tables") or []:
            if t not in roles:
                roles[t] = "failed_task"
        for check in dq_related:
            for t in check.get("tables") or []:
                if t not in roles:
                    roles[t] = "dq_failed"
                shared = set(check.get("shared_tables") or []) & set(target.get("tables") or [])
                if t in shared:
                    roles[t] = "shared_task_dq"
        return roles

    def _build_impact_assessment(
        self,
        root_key: str,
        impacted_keys: List[str],
        affected_tables: List[Dict[str, Any]],
        downstream_consumers: Dict[str, Any],
        run_by_key: Dict[str, Dict[str, Any]],
        graph: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Impact Assessment section as defined in rca.md."""
        impacted_tables = list({t["table"] for t in affected_tables})[:20]

        # Impacted pipelines = root + all downstream tasks (short names)
        impacted_pipelines: List[str] = []
        root_run = run_by_key.get(root_key, {})
        root_meta = graph.get(root_key, {})
        root_name = root_run.get("name") or root_meta.get("name") or root_key
        impacted_pipelines.append(root_name)
        for k in impacted_keys[:15]:
            r = run_by_key.get(k, {})
            m = graph.get(k, {})
            n = r.get("name") or m.get("name") or k
            if n not in impacted_pipelines:
                impacted_pipelines.append(n)

        # Impacted reports = views / procedures named like reports / dashboards
        impacted_reports: List[str] = []
        for item in (downstream_consumers.get("details") or []):
            name = item.get("name", "").lower()
            if any(kw in name for kw in ("report", "dashboard", "mart", "bi", "analytics", "kpi")):
                impacted_reports.append(item.get("name"))

        # Business severity — Critical if reports affected or many pipelines
        n_impact = len(impacted_keys) + len(impacted_tables)
        if impacted_reports:
            severity = "Critical"
        elif n_impact >= 10:
            severity = "High"
        elif n_impact >= 3:
            severity = "Medium"
        else:
            severity = "Low"

        return {
            "impacted_tables": impacted_tables,
            "impacted_pipelines": impacted_pipelines,
            "impacted_reports": impacted_reports,
            "business_severity": severity,
        }

    def _build_remediation(
        self,
        category: str,
        target: Dict[str, Any],
        root: Dict[str, Any],
        extra_context: Optional[str] = None,
        *,
        use_llm: bool = True,
    ) -> Dict[str, Any]:
        """Try LLM-generated remediation first, fall back to templates."""
        if use_llm:
            llm_result = self._llm_remediation(category, target, root, extra_context)
            if llm_result and llm_result.get("immediate_fix"):
                return llm_result
        return self._template_remediation(category, target, root)

    def _template_remediation(
        self,
        category: str,
        target: Dict[str, Any],
        root: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Deterministic fallback remediation templates by category."""
        error = (root.get("error") or target.get("error") or "").strip()
        name = root.get("name") or target.get("name") or "the failed task"

        if category == CATEGORY_CODE:
            return {
                "immediate_fix": (
                    f"Review the SQL / procedure definition in '{name}'. "
                    f"Error: {error[:200] if error else 'see logs'}. "
                    "Fix the compilation error, missing object, or permission issue."
                ),
                "permanent_fix": (
                    "Add a pre-flight check in the task to validate dependent objects exist "
                    "before executing the main SQL. Implement CI/CD linting for procedure changes."
                ),
                "monitoring_recommendation": (
                    "Alert on SQL compilation errors and 'object not found' patterns "
                    "within 5 minutes of task start."
                ),
            }
        if category == CATEGORY_DQ:
            return {
                "immediate_fix": (
                    f"Inspect DQ validation logic for '{name}'. "
                    "Check source table row counts, filter conditions, and join logic for unexpected changes."
                ),
                "permanent_fix": (
                    "Add row-count reconciliation checks between source and target tables. "
                    "Define explicit null-handling rules and document business thresholds."
                ),
                "monitoring_recommendation": (
                    "Track record-count ratios daily. Alert if delta exceeds ±5% vs 7-day rolling average."
                ),
            }
        if category == CATEGORY_DEPENDENCY:
            return {
                "immediate_fix": (
                    f"Identify and resolve the upstream failure blocking '{name}'. "
                    "Check the parent task / predecessor pipeline and re-run once resolved."
                ),
                "permanent_fix": (
                    "Implement retry logic with exponential back-off for predecessor dependencies. "
                    "Add explicit WAIT conditions and dependency health checks."
                ),
                "monitoring_recommendation": (
                    "Monitor predecessor task completion time. Alert if SLA window is breached "
                    "before the dependent task is scheduled to start."
                ),
            }
        if category == CATEGORY_INFRA:
            return {
                "immediate_fix": (
                    f"Re-run '{name}' during off-peak hours or on a larger warehouse. "
                    "Check for warehouse suspension or capacity throttling in Snowflake Activity logs."
                ),
                "permanent_fix": (
                    "Tune query to reduce spill / memory pressure. "
                    "Consider multi-cluster warehouse auto-scaling or table micro-partitioning."
                ),
                "monitoring_recommendation": (
                    "Alert on warehouse queue depth > 5 minutes and bytes_spilled_to_remote_storage > 0."
                ),
            }
        if category == CATEGORY_AVAIL:
            return {
                "immediate_fix": (
                    f"Verify the upstream data source for '{name}' has completed loading. "
                    "Check file arrival in the landing stage / share refresh status."
                ),
                "permanent_fix": (
                    "Add a data-arrival sentinel check (e.g. SYSTEM$PIPE_STATUS or S3 manifest check) "
                    "as a gate before the task is scheduled."
                ),
                "monitoring_recommendation": (
                    "Alert if the source table has not received new rows within the expected SLA window."
                ),
            }
        # Unknown
        return {
            "immediate_fix": (
                f"Review task logs and Snowflake Query History for '{name}'. "
                f"Error: {error[:200] if error else 'no error captured'}."
            ),
            "permanent_fix": "Investigate root cause, add targeted error handling, and document fix.",
            "monitoring_recommendation": "Enable enhanced logging for this task and alert on any non-SUCCESS state.",
        }

    def _build_propagation_chain(
        self,
        root_key: str,
        target_key: str,
        root: Dict[str, Any],
        target: Dict[str, Any],
        impacted_keys: List[str],
        graph: Dict[str, Dict[str, Any]],
        run_by_key: Dict[str, Dict[str, Any]],
        dq_related: List[Dict[str, Any]],
        downstream_consumers: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Build a linear failure propagation chain for UI rendering."""
        chain: List[Dict[str, Any]] = []
        root_name = root.get("name") or root_key
        chain.append({"name": root_name, "type": root.get("platform", "task"), "status": "root_cause"})
        if root_key != target_key:
            chain.append({"name": target.get("name", target_key), "type": "task", "status": "failed"})
        for k in impacted_keys[:2]:
            r = run_by_key.get(k, {})
            chain.append({"name": r.get("name", k), "type": "task", "status": "impacted"})
        for dq in dq_related[:1]:
            chain.append({"name": dq.get("name", ""), "type": "dq", "status": "failed"})
        for item in (downstream_consumers.get("details") or [])[:3]:
            if any(kw in item.get("name", "").lower() for kw in ("report", "dashboard", "mart", "bi", "kpi")):
                chain.append({"name": item["name"], "type": "report", "status": "impacted"})
                break
        return chain

    # ── Main analysis entry-points ─────────────────────────────────────────────

    def analyze(self, pipeline_id: str, extra_context: Optional[str] = None,
                date_from: Optional[str] = None, date_to: Optional[str] = None,
                *, include_narrative: bool = False) -> Dict[str, Any]:
        if pipeline_id.startswith("dq_"):
            return self.analyze_dq(
                pipeline_id, extra_context,
                date_from=date_from, date_to=date_to,
                include_narrative=include_narrative)

        journey: List[Dict[str, str]] = []
        structured_evidence: List[Dict[str, Any]] = []

        # ── Step 1: Identify failed object ────────────────────────────────────
        lineage = LineageService()
        pipelines = [lineage.enrich_pipeline(p)
                     for p in _collect_pipelines_for_rca(pipeline_id, date_from, date_to)]
        idx = {p["id"]: p for p in pipelines}
        target = idx.get(pipeline_id)
        if not target:
            return {"error": f"pipeline {pipeline_id} not found"}

        target = self._enrich_for_rca(lineage, target)
        idx[pipeline_id] = target
        run_by_key = index_runs_by_task_key(pipelines)
        graph = lineage.load_task_graph()

        target_key = target.get("task_key") or pipeline_id
        journey.append({"step": "Failure detected", "status": "done",
                        "detail": f"{target.get('name')} — status {target.get('status')}"})

        # ── Step 2: Classify failure ──────────────────────────────────────────
        root_key = self._walk_upstream_root(target, run_by_key, graph)
        root = run_by_key.get(root_key, target)
        if root.get("id") != target.get("id"):
            root = self._enrich_for_rca(lineage, root)
            run_by_key[root_key] = root

        impacted_keys = self._downstream_impact(root_key, graph)
        category = _classify(root.get("error") or target.get("error"))
        journey.append({"step": "Failure classified", "status": "done",
                        "detail": f"{category}"})
        journey.append({"step": "Upstream dependency traced", "status": "done",
                        "detail": f"Root cause node: {root.get('name', root_key)}"})

        # ── Lineage graphs ────────────────────────────────────────────────────
        lineage_graph = self._build_lineage(target_key, root_key, impacted_keys, graph, run_by_key)
        df, dt = _normalize_rca_dates(date_from, date_to)
        dq_related = lineage.correlate_dq(target, lineage.failed_dq_checks(df, dt))
        table_roles = self._table_roles_from_context(target, root, dq_related)
        affected_tables = self._build_affected_tables(target, root, dq_related, table_roles)
        lineage_table = lineage.build_lineage_table(
            target, root_key, impacted_keys, graph, run_by_key, dq_related, table_roles)

        relevant_keys = self._relevant_task_keys(target_key, root_key, impacted_keys, graph)
        table_lineage = lineage.build_table_lineage_graph(
            relevant_keys, root_key, impacted_keys, graph, run_by_key)

        all_resolved_tables = [t["table"] for t in affected_tables][:3]
        downstream_map = lineage.discover_all_downstream(
            all_resolved_tables,
            context_database=target.get("database", ""),
            context_schema=target.get("schema", ""),
        )
        table_lineage = lineage.enrich_table_lineage_with_downstream(table_lineage, downstream_map)

        journey.append({"step": "Lineage resolved", "status": "done",
                        "detail": f"{len(lineage_graph.get('nodes', []))} nodes, {len(impacted_keys)} downstream impacted"})

        # --- Two-section lineage graphs ---------------------------------------
        io_keys = {target_key, root_key}
        upstream_lineage = lineage.build_upstream_lineage_graph(
            target_key, root_key, graph, run_by_key, resolve_io_keys=io_keys)
        downstream_lineage = lineage.build_downstream_lineage_graph(
            target_key, root_key, impacted_keys, graph, run_by_key,
            downstream_map=downstream_map, resolve_io_keys=io_keys)

        downstream_consumers = {
            "views": sum(1 for items in downstream_map.values() for i in items if i["type"] == "view"),
            "procedures": sum(1 for items in downstream_map.values() for i in items if i["type"] == "procedure"),
            "details": [item for items in downstream_map.values() for item in items][:20],
        }
        for parent_tbl, items in downstream_map.items():
            for item in items:
                lineage_table.append({
                    "kind": item["type"], "id": item["fqn"], "name": item["name"],
                    "status": "IMPACTED", "state": "impacted", "role": "downstream",
                    "upstream": parent_tbl, "tables": item["fqn"], "table_count": 1, "error": "—",
                })

        # ── Metadata-first evidence collection ───────────────────────────────
        signature = f"{root.get('platform','snowflake')} {category} {(root.get('error') or '')[:50]}"
        prior = self.recall(signature)
        confidence = 0.72
        if prior:
            confidence = min(0.95, 0.72 + 0.1 * len(prior))
        if affected_tables:
            confidence = min(0.97, confidence + 0.05)
        if root.get("error"):
            confidence = min(0.97, confidence + 0.05)

        evidence: List[str] = []
        if target.get("name"):
            evidence.append(f"Task Name: {target['name']}")
            structured_evidence.append({"type": "task_metadata", "source": "Task History",
                                        "summary": f"Task: {target['name']}", "strength": "high", "confidence_contribution": 0.0})
        if target.get("status"):
            evidence.append(f"Task State: {target['status']}")
        if root.get("error"):
            evidence.append(f"Error Message: {root['error']}")
            structured_evidence.append({"type": "error_message", "source": "Snowflake Error Log",
                                        "summary": root["error"][:200], "strength": "high", "confidence_contribution": 0.05})
        if root.get("query_id"):
            evidence.append(f"Root Query ID: {root['query_id']}")
            structured_evidence.append({"type": "query_id", "source": "Query History",
                                        "summary": f"Query ID: {root['query_id']}", "strength": "high", "confidence_contribution": 0.05})
        if target.get("started_at"):
            evidence.append(f"Start Time: {target['started_at']}")
        if target.get("ended_at"):
            evidence.append(f"End Time: {target['ended_at']}")
        if target.get("tables"):
            evidence.append(f"Tables in failed task: {', '.join(target['tables'][:8])}")
            structured_evidence.append({"type": "table_resolution", "source": "SQL Parsing / ACCOUNT_USAGE",
                                        "summary": f"{len(target['tables'])} table(s) resolved", "strength": "high", "confidence_contribution": 0.05})
        if dq_related:
            evidence.append(
                f"{len(dq_related)} related DQ failure(s): "
                + ", ".join(c.get("name", "") for c in dq_related[:5]))
            structured_evidence.append({"type": "dq_correlation", "source": "DQ Validation Summary",
                                        "summary": f"{len(dq_related)} correlated DQ failure(s)", "strength": "medium", "confidence_contribution": 0.03})
        if root.get("log_ref"):
            from app.connectors.aws_connector import AWSConnector
            from app.connectors.snowflake_connector import SnowflakeConnector
            getter = SnowflakeConnector() if root.get("platform") == "snowflake" else AWSConnector()
            log_lines = getter.get_logs(root["log_ref"])[:6]
            evidence += log_lines
            if log_lines:
                structured_evidence.append({"type": "log_entry", "source": "Execution Logs",
                                            "summary": f"{len(log_lines)} log line(s) retrieved", "strength": "medium", "confidence_contribution": 0.03})

        journey.append({"step": "Evidence collected", "status": "done",
                        "detail": f"{len(evidence)} evidence item(s)"})

        # ── Impact assessment + remediation (skill sections) ─────────────────
        impact_assessment = self._build_impact_assessment(
            root_key, impacted_keys, affected_tables, downstream_consumers, run_by_key, graph)
        remediation = self._build_remediation(category, target, root, extra_context, use_llm=False)
        confidence_lvl = _confidence_level(confidence)

        # ── Summary ───────────────────────────────────────────────────────────
        table_summary = (
            f"{len(affected_tables)} table(s) involved"
            if affected_tables else "no physical tables resolved"
        )
        summary = (
            f"'{target['name']}' {target['status'].lower()} — {category}. "
            f"Root cause: '{root['name']}'. "
            f"{len(impacted_keys)} downstream task(s) impacted. {table_summary}."
        )
        detailed_analysis = (
            f"Task '{target['name']}' (platform: {target.get('platform','snowflake')}) "
            f"failed with status {target['status']}. "
            f"The earliest failing upstream node is '{root['name']}' "
            f"(error: {(root.get('error') or 'no error captured')[:300]}). "
            f"Failure is classified as {category}."
        )

        refinement = None
        if extra_context:
            refinement = self._refine(target, root, category, extra_context, idx, run_by_key)
            if refinement:
                summary += f" Refinement: {refinement['note']}"
                confidence = min(0.97, confidence + 0.05)
                confidence_lvl = _confidence_level(confidence)
                if refinement.get("new_root") and refinement["new_root"] in run_by_key:
                    root_key = refinement["new_root"]
                    root = run_by_key[root_key]
                    impacted_keys = self._downstream_impact(root_key, graph)
                    lineage_graph = self._build_lineage(target_key, root_key, impacted_keys, graph, run_by_key)

        # ── SQL deep-dive: fetch task/procedure definition ────────────────────
        root_meta = graph.get(root_key, {})
        root_db = root_meta.get("database") or root.get("database") or ""
        root_schema = root_meta.get("schema") or root.get("schema") or ""
        root_task_name = root_meta.get("task_name") or ""
        task_sql = lineage.fetch_object_definition(root_db, root_schema, root_task_name, "task")

        procedure_io = lineage.resolve_task_procedure_io(
            root_db, root_schema, root_task_name, root.get("tables"))

        # ── Fetch upstream procedure chain (2-3 levels deep) ────────────────
        procedure_chain: List[Dict[str, Any]] = []
        if root_db and root_schema and root_task_name:
            procedure_chain = lineage.fetch_upstream_procedure_chain(
                root_db, root_schema, root_task_name, depth=3)

        # ── If DQ-related, also execute DQ SQL ──────────────────────────────
        dq_execution_result = None
        if dq_related:
            dq_conn = DQConnector()
            first_dq = dq_related[0]
            dq_execution_result = dq_conn.execute_dq_rule(
                first_dq.get("name") or "", subject_area=first_dq.get("table_name"))

        # ── Memory: smart recall by tables ──────────────────────────────────
        table_priors = self._recall_by_tables(target.get("tables", []))
        all_priors = (prior or []) + table_priors

        # ── Knowledge rules matching ─────────────────────────────────────────
        knowledge_rules = self._match_knowledge_rules(
            root.get("error") or target.get("error"),
            target.get("name"),
            target.get("tables", []),
        )

        # ── Claude harness (uses the full rca.md skill) ───────────────────────
        chain_for_llm = [
            {"object": c["object_name"], "type": c["object_type"],
             "sql": c["sql_body"][:1500], "inputs": c["inputs"][:5], "outputs": c["outputs"][:5]}
            for c in procedure_chain[:3]
        ] if procedure_chain else None

        dq_exec_for_llm = None
        if dq_execution_result and dq_execution_result.get("executed"):
            dq_exec_for_llm = {
                "row_count": dq_execution_result["row_count"],
                "columns": dq_execution_result["columns"],
                "sample_rows": dq_execution_result["rows"][:10],
            }

        llm = self.think({
            "task_name": target.get("name"),
            "task_state": target.get("status"),
            "error_message": root.get("error") or target.get("error"),
            "start_time": target.get("started_at"),
            "end_time": target.get("ended_at"),
            "warehouse": target.get("warehouse"),
            "parent_task": graph.get(target_key, {}).get("upstream", [])[:3],
            "dependency_tasks": [run_by_key.get(k, {}).get("name", k) for k in impacted_keys[:5]],
            "tables": target.get("tables", [])[:10],
            "root_cause_task": root.get("name"),
            "category": category,
            "affected_tables": [t["table"] for t in affected_tables[:10]],
            "impacted_downstream_count": len(impacted_keys),
            "task_definition_sql": (task_sql or "")[:2000],
            "procedure_io": procedure_io,
            "upstream_procedure_chain": chain_for_llm,
            "dq_execution_results": dq_exec_for_llm,
            "sql_analysis_request": (
                "CRITICAL: Analyze the SQL/procedure chain and identify the SPECIFIC root cause. "
                "Reference exact table names, column names, and SQL conditions. "
                "If procedure chain is provided, trace the data flow and identify where the logic breaks. "
                "Provide root_cause as a structured object with explanation, entities, and code_snippets."
            ),
            "prior_rca_cases": [
                {"error_pattern": p.get("error_pattern"), "category": p.get("category"),
                 "resolution": p.get("resolution_applied"), "analysis": p.get("code_analysis_summary")}
                for p in all_priors[:3]
            ],
            "domain_knowledge": knowledge_rules[:3],
            "extra_context": extra_context,
        }, max_tokens=4000)

        # ── Build code_analysis ──────────────────────────────────────────────
        code_analysis: Dict[str, Any] = {
            "task_sql": task_sql[:2000] if task_sql else None,
            "procedure_io": procedure_io,
            "tables_inspected": [t["table"] for t in affected_tables[:10]],
            "llm_explanation": None,
        }

        if llm:
            if llm.get("summary"):
                summary = llm["summary"]
            if llm.get("detailed_analysis"):
                detailed_analysis = llm["detailed_analysis"]
            if llm.get("failure_type"):
                category = llm["failure_type"]
            if llm.get("confidence"):
                confidence = float(llm["confidence"])
                confidence_lvl = llm.get("confidence_level") or _confidence_level(confidence)
            if llm.get("impact_assessment"):
                impact_assessment.update(llm["impact_assessment"])
            if llm.get("remediation"):
                remediation.update(llm["remediation"])
            if llm.get("evidence"):
                evidence = list(llm["evidence"])
            if llm.get("code_analysis"):
                code_analysis["llm_explanation"] = llm["code_analysis"]
            elif llm.get("detailed_analysis"):
                code_analysis["llm_explanation"] = llm["detailed_analysis"]

        # ── Structured root_cause from LLM ───────────────────────────────────
        root_cause_obj = None
        if llm and llm.get("root_cause"):
            root_cause_obj = llm["root_cause"]
        elif llm:
            entities = [{"name": t, "type": "table"} for t in (target.get("tables") or [])[:5]]
            root_cause_obj = {
                "explanation": llm.get("detailed_analysis") or summary,
                "entities": entities,
                "code_snippets": [],
                "comparison": None,
            }

        # ── Impact summary one-liner ─────────────────────────────────────────
        impact_summary = None
        if llm and llm.get("impact_summary"):
            impact_summary = llm["impact_summary"]
        else:
            n_tables = len(impact_assessment.get("impacted_tables", []))
            n_reports = len(impact_assessment.get("impacted_reports", []))
            n_pipelines = len(impact_assessment.get("impacted_pipelines", []))
            parts = []
            if n_tables:
                parts.append(f"{n_tables} table(s)")
            if n_pipelines:
                parts.append(f"{n_pipelines} pipeline(s)")
            if n_reports:
                report_names = ", ".join(impact_assessment["impacted_reports"][:2])
                parts.append(f"{n_reports} report(s) including {report_names}")
            if len(impacted_keys):
                parts.append(f"{len(impacted_keys)} downstream task(s)")
            impact_summary = ", ".join(parts) if parts else "No downstream impact identified"

        # ── Text lineage diagrams (skill format) ─────────────────────────────
        upstream_text = (
            llm.get("upstream_lineage_text") if llm else None
        ) or _lineage_text(upstream_lineage.get("nodes", []), upstream_lineage.get("edges", []))
        downstream_text = (
            llm.get("downstream_lineage_text") if llm else None
        ) or _lineage_text(downstream_lineage.get("nodes", []), downstream_lineage.get("edges", []))

        journey.append({"step": "RCA identified", "status": "done",
                        "detail": f"{category} — {confidence_lvl} confidence ({round(confidence * 100)}%)"})

        # ── New structured output fields ─────────────────────────────────────
        confidence_drivers = [
            {"factor": "Error identified", "met": bool(root.get("error"))},
            {"factor": "Dependency traced", "met": root_key != target_key},
            {"factor": "Lineage resolved", "met": bool(lineage_graph.get("nodes"))},
            {"factor": "First failing step found", "met": bool(root_key)},
            {"factor": "Supporting evidence collected", "met": len(evidence) >= 3},
            {"factor": "Impact path validated", "met": len(impacted_keys) > 0},
            {"factor": "Prior case matched", "met": bool(all_priors)},
        ]

        failure_propagation = self._build_propagation_chain(
            root_key, target_key, root, target, impacted_keys,
            graph, run_by_key, dq_related, downstream_consumers)

        incident_summary_obj = {
            "failure_type": category,
            "failure_name": target.get("name", pipeline_id),
            "environment": "Production",
            "detection_time": target.get("started_at"),
            "current_status": target.get("status"),
            "confidence_score": round(confidence * 100),
            "root_cause_category": category,
        }

        if root_cause_obj:
            root_cause_obj["business_explanation"] = (
                (llm.get("root_cause") or {}).get("business_explanation") if llm else None
            ) or summary
            root_cause_obj["technical_explanation"] = (
                (llm.get("root_cause") or {}).get("technical_explanation") if llm else None
            ) or detailed_analysis

        incident = self.incident_log.log({
            "pipeline_id": pipeline_id, "name": target["name"], "platform": target["platform"],
            "status": target["status"], "signature": signature, "source": "rca",
            "root_cause_node": root_key, "category": category, "rca_summary": summary,
        })

        result = {
            "incident_id": incident["incident_id"],
            "pipeline_id": pipeline_id,
            "analysis_type": "task",
            "root_cause_node": root_key,
            "root_cause_name": root.get("name", root_key),
            # Skill output fields
            "failure_type": category,
            "summary": summary,
            "detailed_analysis": detailed_analysis,
            "category": category,
            "confidence": round(confidence, 2),
            "confidence_level": confidence_lvl,
            "evidence": evidence,
            "recommended_next": "fix",
            "impact_assessment": impact_assessment,
            "impact_summary": impact_summary,
            "root_cause": root_cause_obj,
            "remediation": remediation,
            # Investigation dashboard fields
            "incident_summary": incident_summary_obj,
            "investigation_journey": journey,
            "failure_propagation": failure_propagation,
            "confidence_drivers": confidence_drivers,
            "structured_evidence": structured_evidence,
            # Lineage
            "upstream_lineage": upstream_lineage,
            "upstream_lineage_text": upstream_text,
            "downstream_lineage": downstream_lineage,
            "downstream_lineage_text": downstream_text,
            "lineage": lineage_graph,
            "lineage_table": lineage_table,
            "affected_tables": affected_tables,
            "related_dq_failures": [
                {"id": c.get("id"), "name": c.get("name"), "status": c.get("status"),
                 "tables": c.get("tables", []), "match_score": c.get("match_score")}
                for c in dq_related
            ],
            "impacted_nodes": impacted_keys,
            "table_lineage": table_lineage,
            "downstream_consumers": downstream_consumers,
            "refinement": refinement,
            "code_analysis": code_analysis,
            "procedure_chain": procedure_chain[:3] if procedure_chain else None,
            "dq_execution_result": dq_execution_result if dq_execution_result and dq_execution_result.get("executed") else None,
            "knowledge_applied": knowledge_rules[:3] if knowledge_rules else None,
            "seen_before": bool(all_priors),
        }
        result["chat_narrative"] = (
            self._generate_narrative(result) if include_narrative else None)
        self.learn({
            "signature": signature,
            "pipeline_id": pipeline_id,
            "category": category,
            "root_cause_node": root_key,
            "root_cause_name": root.get("name"),
            "error_pattern": (root.get("error") or "")[:200],
            "resolution_applied": None,
            "code_analysis_summary": (code_analysis.get("llm_explanation") or "")[:300],
            "tables_involved": [t["table"] for t in affected_tables[:10]],
            "severity": impact_assessment.get("business_severity"),
            "confidence": confidence,
        })
        return result

    def analyze_dq(self, check_id: str, extra_context: Optional[str] = None,
                   date_from: Optional[str] = None, date_to: Optional[str] = None,
                   *, include_narrative: bool = False) -> Dict[str, Any]:
        journey: List[Dict[str, str]] = []
        structured_evidence: List[Dict[str, Any]] = []

        lineage = LineageService()
        df, dt = _normalize_rca_dates(date_from, date_to)
        all_checks = DQConnector().read_results(df, dt)
        check = next((c for c in all_checks if c["id"] == check_id), None)
        if not check:
            return {"error": f"DQ check {check_id} not found"}

        check = {**check, "tables": lineage.resolve_dq_tables(check)}
        journey.append({"step": "Failure detected", "status": "done",
                        "detail": f"DQ check '{check.get('name')}' — status {check.get('status')}"})

        pipelines = [lineage.enrich_pipeline(p) for p in MonitoringAgent().collect()]
        idx = {p["id"]: p for p in pipelines}
        run_by_key = index_runs_by_task_key(pipelines)
        graph = lineage.load_task_graph()
        failed_tasks: List[Tuple[int, Dict[str, Any]]] = []
        check_tables = set(check.get("tables") or [])

        for p in idx.values():
            if p.get("status") not in ("FAILED", "DELAYED"):
                continue
            overlap = check_tables & set(p.get("tables") or [])
            score = len(overlap) * 10
            if score > 0:
                failed_tasks.append((score, p))
        failed_tasks.sort(key=lambda x: x[0], reverse=True)
        related_tasks = [p for _, p in failed_tasks[:10]]

        root_task = related_tasks[0] if related_tasks else None
        category = _classify(check.get("error"))
        if root_task:
            category = _classify(root_task.get("error") or check.get("error"))
        journey.append({"step": "Failure classified", "status": "done", "detail": category})
        if related_tasks:
            journey.append({"step": "Related task failures correlated", "status": "done",
                            "detail": f"{len(related_tasks)} task(s) share tables with this DQ check"})

        table_roles: Dict[str, str] = {}
        for t in check_tables:
            table_roles[t] = "dq_failed"
        for p in related_tasks:
            for t in p.get("tables") or []:
                if t in check_tables:
                    table_roles[t] = "shared_task_dq"
                elif t not in table_roles:
                    table_roles[t] = "failed_task"

        dq_related = [check] if check.get("status") in ("FAILED", "WARNING", "DELAYED") else []
        affected_tables = self._build_affected_tables(check, root_task or check, dq_related, table_roles)

        lineage_table: List[Dict[str, Any]] = [{
            "kind": "dq", "id": check_id, "name": f"DQ: {check.get('name')}",
            "status": check.get("status", "FAILED"), "state": "failed", "role": "data_quality",
            "upstream": check.get("table_name") or "—",
            "tables": ", ".join(check_tables) if check_tables else "—",
            "table_count": len(check_tables),
            "error": (check.get("error") or "")[:120] or "—",
        }]
        for p in related_tasks:
            tkey = p.get("task_key") or p["id"]
            lineage_table.append({
                "kind": "task", "id": tkey, "name": p.get("name", tkey),
                "status": p.get("status", "FAILED"), "state": "failed", "role": "related_task",
                "upstream": ", ".join(graph.get(tkey, {}).get("upstream", [])[:2]) or "—",
                "tables": ", ".join((p.get("tables") or [])[:5]) or "—",
                "table_count": len(p.get("tables") or []),
                "error": (p.get("error") or "")[:120] or "—",
            })
        for tbl, role in sorted(table_roles.items()):
            lineage_table.append({
                "kind": "table", "id": tbl, "name": tbl,
                "status": role.replace("_", " ").upper(), "state": "failed",
                "role": role, "upstream": "—", "tables": tbl, "table_count": 1, "error": "—",
            })

        impacted_keys: List[str] = []
        root_key_task = check_id
        if root_task:
            root_key_task = root_task.get("task_key") or root_task["id"]
            impacted_keys = self._downstream_impact(root_key_task, graph)

        if root_task:
            lineage_graph = self._build_lineage(
                root_key_task, root_key_task, impacted_keys, graph, run_by_key)
            lineage_graph["nodes"].append({"id": check_id, "label": f"DQ: {check.get('name')}",
                                           "platform": "dq", "state": "failed"})
            lineage_graph["edges"].append({"from": root_key_task, "to": check_id})
        else:
            nodes, edges = [], []
            nodes.append({"id": check_id, "label": f"DQ: {check.get('name')}",
                          "platform": "dq", "state": "failed"})
            for p in related_tasks:
                tkey = p.get("task_key") or p["id"]
                nodes.append({"id": tkey, "label": p.get("name", tkey),
                              "platform": "snowflake", "state": "failed"})
                edges.append({"from": tkey, "to": check_id})
            lineage_graph = {"nodes": nodes, "edges": edges}

        journey.append({"step": "Lineage resolved", "status": "done",
                        "detail": f"{len(lineage_graph.get('nodes', []))} nodes discovered"})

        relevant_keys = {root_key_task, *impacted_keys} if root_task else set()
        for p in related_tasks:
            relevant_keys.add(p.get("task_key") or p["id"])
        table_lineage = lineage.build_table_lineage_graph(
            relevant_keys, root_key_task,
            impacted_keys, graph, run_by_key) if relevant_keys else {"nodes": [], "edges": []}

        all_resolved_tables = [t["table"] for t in affected_tables][:3]
        from app.core.config import get_dq_monitoring_config
        dq_cfg = get_dq_monitoring_config()
        dq_fqn_parts = dq_cfg["table_fqn"].split(".")
        ctx_db = (root_task or {}).get("database", "") or (dq_fqn_parts[0] if len(dq_fqn_parts) >= 3 else "")
        ctx_schema = (root_task or {}).get("schema", "") or (dq_fqn_parts[1] if len(dq_fqn_parts) >= 3 else "")
        downstream_map = lineage.discover_all_downstream(
            all_resolved_tables, context_database=ctx_db, context_schema=ctx_schema)
        table_lineage = lineage.enrich_table_lineage_with_downstream(table_lineage, downstream_map)

        dq_io_keys = {root_key_task} if root_task else None
        dq_upstream_lineage = lineage.build_upstream_lineage_graph(
            root_key_task, root_key_task, graph, run_by_key,
            resolve_io_keys=dq_io_keys) if root_task else {"nodes": [], "edges": []}
        dq_downstream_lineage = lineage.build_downstream_lineage_graph(
            root_key_task, root_key_task, impacted_keys, graph, run_by_key,
            downstream_map=downstream_map,
            resolve_io_keys=dq_io_keys) if root_task else {"nodes": [], "edges": []}

        downstream_consumers = {
            "views": sum(1 for items in downstream_map.values() for i in items if i["type"] == "view"),
            "procedures": sum(1 for items in downstream_map.values() for i in items if i["type"] == "procedure"),
            "details": [item for items in downstream_map.values() for item in items][:20],
        }
        for parent_tbl, items in downstream_map.items():
            for item in items:
                lineage_table.append({
                    "kind": item["type"], "id": item["fqn"], "name": item["name"],
                    "status": "IMPACTED", "state": "impacted", "role": "downstream",
                    "upstream": parent_tbl, "tables": item["fqn"], "table_count": 1, "error": "—",
                })

        evidence = [f"DQ check: {check.get('name')} — {check.get('status', 'FAILED')}"]
        structured_evidence.append({"type": "dq_check", "source": "DQ Validation Summary",
                                    "summary": f"DQ check '{check.get('name')}' failed", "strength": "high", "confidence_contribution": 0.05})
        if check.get("error"):
            evidence.append(f"Error Message: {check['error']}")
            structured_evidence.append({"type": "error_message", "source": "DQ Check Output",
                                        "summary": check["error"][:200], "strength": "high", "confidence_contribution": 0.05})
        if check_tables:
            evidence.append(f"Tables from DQ: {', '.join(sorted(check_tables)[:8])}")
            structured_evidence.append({"type": "table_resolution", "source": "DQ Rule / SQL Parsing",
                                        "summary": f"{len(check_tables)} table(s) identified", "strength": "high", "confidence_contribution": 0.05})
        if related_tasks:
            evidence.append(
                f"{len(related_tasks)} related failed task(s) share table(s): "
                + ", ".join(p.get("name", "") for p in related_tasks[:4]))
            structured_evidence.append({"type": "task_correlation", "source": "Task Monitoring",
                                        "summary": f"{len(related_tasks)} correlated failed task(s)", "strength": "medium", "confidence_contribution": 0.03})
        journey.append({"step": "Evidence collected", "status": "done",
                        "detail": f"{len(evidence)} evidence item(s)"})

        if root_task:
            summary = (
                f"DQ check '{check.get('name')}' failed ({category}). "
                f"Likely linked to failed task '{root_task.get('name')}'. "
                f"{len(impacted_keys)} downstream task(s) impacted. "
                f"{len(check_tables)} table(s) identified."
            )
            root_key = root_task.get("task_key") or root_task["id"]
        else:
            summary = (
                f"DQ check '{check.get('name')}' failed ({category}). "
                f"{len(check_tables)} table(s) identified; no matching failed task found."
            )
            root_key = check_id

        detailed_analysis = (
            f"DQ check '{check.get('name')}' failed with status {check.get('status')}. "
            f"Failure classified as {category}. "
            + (f"Closest related task: '{root_task.get('name')}' ({root_task.get('status')}). " if root_task else "No correlated task failure found. ")
            + f"Tables involved: {', '.join(sorted(check_tables)[:5]) or 'unknown'}."
        )

        if extra_context:
            summary += f" Note: {extra_context}"

        confidence = 0.78 if check_tables else 0.55

        # ── DQ SQL deep-dive: fetch rule definition + EXECUTE it ────────────
        dq_conn = DQConnector()
        dq_rule = dq_conn.fetch_dq_rule_sql(check.get("name") or "", subject_area=check.get("table_name"))
        dq_rule_sql = None
        dq_execution_result = None
        if dq_rule:
            dq_rule_sql = "\n".join(f"{k}: {v}" for k, v in dq_rule.items() if v)[:3000]
            dq_execution_result = dq_conn.execute_dq_rule(
                check.get("name") or "", subject_area=check.get("table_name"), limit=500)
            row_count = dq_execution_result.get("row_count", 0) if dq_execution_result else 0
            journey.append({"step": "DQ rule SQL executed", "status": "done",
                            "detail": f"Rule fetched and executed — {row_count} row(s) returned"})
        else:
            journey.append({"step": "DQ rule SQL executed", "status": "done",
                            "detail": "No SQL rule definition found"})

        # ── Reconcile against LIVE execution (deterministic, no LLM needed) ───
        # The recorded status is historical; re-running the rule tells us the truth now.
        live_status, live_failing_count = DQConnector.interpret_execution(dq_execution_result)
        dq_currently_passing = live_status == "SUCCESS"
        recorded_status = check.get("status")
        # #region agent log
        try:
            import time as _time
            _er = (dq_execution_result or {})
            open(r"C:\Users\EKGAH\Documents\project\ops-monitor\debug-605d47.log", "a", encoding="utf-8").write(
                json.dumps({"sessionId": "605d47", "hypothesisId": "A,B,E", "runId": "post-fix",
                            "location": "rca_agent.py:analyze_dq",
                            "message": "DQ RCA live reconcile",
                            "data": {"check_id": check_id, "name": check.get("name"),
                                     "recorded_status": recorded_status,
                                     "category_pre_llm": category,
                                     "live_status": live_status,
                                     "live_failing_count": live_failing_count,
                                     "dq_currently_passing": dq_currently_passing,
                                     "executed": bool(_er.get("executed")),
                                     "exec_error": (_er.get("error") or "")[:300],
                                     "row_count": _er.get("row_count"),
                                     "sample_pairs": [
                                         {str(k).upper(): v for k, v in (r or {}).items()
                                          if str(k).upper() in ("SOURCE_CNT", "TARGET_CNT", "TABLE_NAME")}
                                         for r in (_er.get("rows") or [])[:4]
                                     ]},
                            "timestamp": int(_time.time() * 1000)}) + "\n")
        except Exception:
            pass
        # #endregion

        exec_err = str((dq_execution_result or {}).get("error") or "")
        live_client_timeout = (
            not (dq_execution_result or {}).get("executed")
            and (("000604" in exec_err) or ("timeout" in exec_err.lower()))
        )

        # If we still have no physical tables, parse them straight out of the rule
        # SQL so the root cause can name exact objects.
        if not check_tables and dq_rule and dq_rule.get("SQL_CODE"):
            sql_tables = parse_tables_from_text(dq_rule.get("SQL_CODE"))
            if sql_tables:
                check_tables = set(sql_tables)

        # A failed DQ check is, by definition, a data-quality failure.
        if category == CATEGORY_UNKNOWN:
            category = CATEGORY_DQ

        # Give bare-numeric QC ids a readable label using their subject area.
        subject_area = check.get("table_name")
        check_label = check.get("name") or check_id
        if subject_area and str(check_label).isdigit():
            check_label = f"{check_label} on {subject_area}"

        tables_phrase = ", ".join(sorted(check_tables)[:5]) or (subject_area or "unknown")

        if live_status:
            journey.append({"step": "Live validation", "status": "done",
                            "detail": f"Rule re-executed now → {live_status}"
                                      + (f", {live_failing_count} violation(s)"
                                         if live_failing_count is not None else "")})
            evidence.insert(1, f"Live re-execution: {live_status}"
                            + (f" ({live_failing_count} violating row(s))"
                               if live_failing_count is not None else ""))

        if dq_currently_passing:
            # Historical failure has been resolved — the check passes right now.
            category = CATEGORY_DQ
            confidence = min(confidence, 0.60)
            summary = (
                f"DQ check '{check_label}' is currently PASSING. The recorded {recorded_status} "
                f"from {check.get('run_at') or 'its last run'} is stale and has been resolved."
            )
            detailed_analysis = (
                f"Live re-execution of the DQ rule for '{check_label}' returns PASS "
                f"(0 violating rows). The historically recorded {recorded_status} has since been "
                f"resolved in the underlying data. Tables validated: {tables_phrase}."
            )
        elif live_status == "FAILED":
            # The check genuinely fails right now — give an exact, evidence-backed RC.
            category = CATEGORY_DQ
            if live_failing_count:
                confidence = max(confidence, 0.85)
            viol = (f"{live_failing_count} violating row(s)"
                    if live_failing_count is not None else "violations")
            summary = (
                f"DQ check '{check_label}' is FAILING now — live re-execution returned {viol} "
                f"({category})."
                + (f" Likely linked to failed task '{root_task.get('name')}'." if root_task else "")
            )
            detailed_analysis = (
                f"Live re-execution of the DQ rule for '{check_label}' returns FAIL with {viol}. "
                f"Failure classified as {category}. "
                + (f"Closest related task: '{root_task.get('name')}' ({root_task.get('status')}). "
                   if root_task else "No correlated task failure found. ")
                + f"Tables involved: {tables_phrase}."
            )
        elif live_client_timeout:
            # App client cancelled the query — not proof of a data mismatch.
            category = CATEGORY_INFRA
            confidence = min(confidence, 0.55)
            journey.append({"step": "Live validation", "status": "warn",
                            "detail": "Rule re-execution timed out (client 000604) — no live verdict"})
            evidence.insert(1, f"Live re-execution timed out (client): {exec_err[:180]}")
            summary = (
                f"Live re-execution of DQ check '{check_label}' timed out (Snowflake client "
                f"000604). Cannot confirm pass/fail from live rows; recorded status was "
                f"{recorded_status}. The same SQL may succeed in the Snowflake UI with a "
                f"longer client timeout — a timeout is not evidence of mismatched counts."
            )
            detailed_analysis = (
                f"Client cancelled the DQ rule for '{check_label}' before results returned "
                f"({exec_err[:200]}). Tables involved: {tables_phrase}. Do not treat this as "
                f"a confirmed data-quality breach until the rule completes and SOURCE_CNT / "
                f"TARGET_CNT (or RESULT) can be interpreted."
            )

        if extra_context and live_status:
            summary += f" Note: {extra_context}"

        # ── Fetch upstream procedure chain (2-3 levels) ──────────────────────
        procedure_chain: List[Dict[str, Any]] = []
        if root_task:
            rt_meta = graph.get(root_key_task, {})
            rt_db = rt_meta.get("database") or root_task.get("database") or ""
            rt_schema = rt_meta.get("schema") or root_task.get("schema") or ""
            rt_task_name = rt_meta.get("task_name") or ""
            if rt_db and rt_schema and rt_task_name:
                procedure_chain = lineage.fetch_upstream_procedure_chain(
                    rt_db, rt_schema, rt_task_name, depth=3)

        # ── Memory: recall priors for DQ checks ──────────────────────────────
        dq_signature = f"dq {category} {(check.get('error') or '')[:50]}"
        prior = self.recall(dq_signature)
        table_priors = self._recall_by_tables(list(check_tables))
        all_priors = (prior or []) + table_priors
        # Don't let prior cases inflate confidence for a check that currently passes —
        # a stale/resolved failure must stay low-confidence per the skill.
        if all_priors and not dq_currently_passing:
            confidence = min(0.95, confidence + 0.1 * min(len(all_priors), 3))

        # ── Knowledge rules ─────────────────────────────────────────────────
        knowledge_rules = self._match_knowledge_rules(
            check.get("error"), check.get("name"), list(check_tables))

        # ── LLM analysis for DQ ──────────────────────────────────────────────
        dq_exec_context = None
        if dq_execution_result and dq_execution_result.get("executed"):
            dq_exec_context = {
                "row_count": dq_execution_result["row_count"],
                "columns": dq_execution_result["columns"],
                "sample_rows": self._prefer_failing_rows(dq_execution_result, limit=10),
            }
        elif dq_execution_result and dq_execution_result.get("error"):
            dq_exec_context = {"execution_error": dq_execution_result["error"]}

        # ── Generic failure-evidence pipeline (all failed checks) ─────────────
        # Always materialize concrete offending rows BEFORE asking the LLM.
        # Path A: reuse live rows when they already name offenders.
        # Path B: deterministic SQL unwrap + LLM rewrite diagnostic.
        # Then force the root cause to cite that evidence (deterministic fallback).
        diagnostic_results = None
        failure_evidence = None
        if live_status == "FAILED":
            failure_evidence, diag_warn = self._collect_dq_failure_evidence(
                dq_conn, check, dq_rule, dq_execution_result, live_status,
                allow_diagnostic_llm=False)
            if failure_evidence:
                diagnostic_results = {
                    "diagnostic_sql": failure_evidence.get("diagnostic_sql"),
                    "row_count": failure_evidence.get("row_count"),
                    "columns": failure_evidence.get("columns"),
                    "sample_rows": failure_evidence.get("sample_rows"),
                    "evidence_summary": failure_evidence.get("evidence_summary"),
                    "evidence_facts": failure_evidence.get("evidence_facts"),
                    "source": failure_evidence.get("source"),
                }
                journey.append({
                    "step": "Failure evidence collected",
                    "status": "done",
                    "detail": (
                        f"{failure_evidence.get('row_count')} offending row(s) via "
                        f"{failure_evidence.get('source')}"
                    ),
                })
                evidence.insert(0, failure_evidence.get("evidence_summary") or "Offending rows collected")
            elif diag_warn:
                journey.append({"step": "Failure evidence collected", "status": "warn",
                                "detail": f"Could not surface offenders: {diag_warn}"})

        det_root = None
        if failure_evidence:
            det_root = self._deterministic_root_cause(
                check, failure_evidence, list(check_tables))

        chain_for_llm = [
            {"object": c["object_name"], "type": c["object_type"],
             "sql": c["sql_body"][:1500], "inputs": c["inputs"][:5], "outputs": c["outputs"][:5]}
            for c in procedure_chain[:3]
        ] if procedure_chain else None

        llm = self.think({
            "analysis_type": "dq_check",
            "qc_id": check.get("name"),
            "qc_status": check.get("status"),
            "error_message": check.get("error"),
            "subject_area": check.get("table_name"),
            "check_type": check.get("column_name"),
            "tables": list(check_tables)[:10],
            "dq_rule_definition": dq_rule_sql[:2000] if dq_rule_sql else None,
            "dq_execution_results": dq_exec_context,
            "diagnostic_results": diagnostic_results,
            "mandatory_evidence_summary": (
                failure_evidence.get("evidence_summary") if failure_evidence else None),
            "mandatory_evidence_facts": (
                failure_evidence.get("evidence_facts") if failure_evidence else None),
            "seed_root_cause": det_root,
            "dq_currently_passing": dq_currently_passing,
            "live_status": live_status,
            "live_failing_count": live_failing_count,
            "recorded_status": recorded_status,
            "upstream_procedure_chain": chain_for_llm,
            "related_failed_tasks": [
                {"name": p.get("name"), "error": (p.get("error") or "")[:100]}
                for p in related_tasks[:3]
            ],
            "category": category,
            "prior_rca_cases": [
                {"error_pattern": p.get("error_pattern"), "category": p.get("category"),
                 "resolution": p.get("resolution_applied")}
                for p in all_priors[:3]
            ],
            "domain_knowledge": knowledge_rules[:3],
            "sql_analysis_request": (
                "Trust the LIVE execution above all else (Evidence Hierarchy Rank 1). "
                "If live_status is SUCCESS / dq_currently_passing is true, the check PASSES now: "
                "report the recorded failure as STALE/RESOLVED, keep confidence <= 0.60, and do "
                "NOT invent a SQL syntax or code error. "
                "If live_status is FAILED: you MUST ground the root cause in "
                "mandatory_evidence_facts / diagnostic_results.sample_rows. "
                "Cite the SPECIFIC keys (brand/segment/id/claim) and BOTH sides' numbers from those "
                "facts. Expand seed_root_cause into a polished narrative — do NOT replace concrete "
                "values with vague phrases. Do NOT blame SQL wrappers (HAVING, CASE Pass/Fail) when "
                "evidence already names the mismatched key — the data mismatch IS the root cause. "
                "Forbidden vague phrases: 'data mismatch', 'semantic issue', 'populations do not "
                "align', 'one brand has a discrepancy', 'without diagnostic', 'could result from'. "
                "Provide root_cause as a structured object."
            ),
            "extra_context": extra_context,
        }, max_tokens=4000)

        code_analysis: Dict[str, Any] = {
            "task_sql": dq_rule_sql,
            "procedure_io": None,
            "tables_inspected": list(check_tables)[:10],
            "llm_explanation": None,
        }

        if llm:
            if llm.get("summary"):
                summary = llm["summary"]
            if llm.get("detailed_analysis"):
                detailed_analysis = llm["detailed_analysis"]
            if llm.get("failure_type"):
                category = llm["failure_type"]
            if llm.get("confidence"):
                confidence = float(llm["confidence"])
            if llm.get("remediation"):
                remediation_override = llm["remediation"]
            else:
                remediation_override = None
            if llm.get("evidence"):
                evidence = list(llm["evidence"])
            if llm.get("code_analysis"):
                code_analysis["llm_explanation"] = llm["code_analysis"]
            elif llm.get("detailed_analysis"):
                code_analysis["llm_explanation"] = llm["detailed_analysis"]
        else:
            remediation_override = None

        # ── Structured root_cause from LLM, grounded in collected evidence ───
        root_cause_obj = None
        if llm and llm.get("root_cause"):
            root_cause_obj = llm["root_cause"]
        elif llm:
            entities = [{"name": t, "type": "table"} for t in list(check_tables)[:5]]
            root_cause_obj = {
                "explanation": llm.get("detailed_analysis") or summary,
                "entities": entities,
                "code_snippets": [],
                "comparison": None,
            }

        # Enforce evidence grounding: if we have concrete offenders and the LLM
        # narrative does not cite them, replace with deterministic RC.
        if failure_evidence and det_root:
            ev_summary = failure_evidence.get("evidence_summary") or "Offending rows collected"
            if ev_summary not in evidence:
                evidence.insert(0, ev_summary)
            rc_text = " ".join([
                str((root_cause_obj or {}).get("explanation") or ""),
                str((root_cause_obj or {}).get("technical_explanation") or ""),
                str(summary or ""),
                str(detailed_analysis or ""),
            ])
            if not self._llm_cites_evidence(rc_text, failure_evidence):
                root_cause_obj = det_root
                summary = det_root["explanation"]
                detailed_analysis = det_root.get("technical_explanation") or det_root["explanation"]
            elif root_cause_obj is None:
                root_cause_obj = det_root
            confidence = max(confidence, 0.85)

        confidence_lvl = _confidence_level(confidence)

        impact_assessment = self._build_impact_assessment(
            root_key, impacted_keys, affected_tables, downstream_consumers, run_by_key, graph)
        remediation = self._build_remediation(
            category, check, root_task or check, use_llm=False)
        if remediation_override:
            remediation.update(remediation_override)

        # ── Impact summary one-liner ─────────────────────────────────────────
        impact_summary = None
        if llm and llm.get("impact_summary"):
            impact_summary = llm["impact_summary"]
        else:
            n_tables = len(impact_assessment.get("impacted_tables", []))
            n_reports = len(impact_assessment.get("impacted_reports", []))
            n_pipelines = len(impact_assessment.get("impacted_pipelines", []))
            parts = []
            if n_tables:
                parts.append(f"{n_tables} table(s)")
            if n_pipelines:
                parts.append(f"{n_pipelines} pipeline(s)")
            if n_reports:
                report_names = ", ".join(impact_assessment["impacted_reports"][:2])
                parts.append(f"{n_reports} report(s) including {report_names}")
            if len(impacted_keys):
                parts.append(f"{len(impacted_keys)} downstream task(s)")
            impact_summary = ", ".join(parts) if parts else "No downstream impact identified"

        upstream_text = _lineage_text(dq_upstream_lineage.get("nodes", []), dq_upstream_lineage.get("edges", []))
        downstream_text = _lineage_text(dq_downstream_lineage.get("nodes", []), dq_downstream_lineage.get("edges", []))

        journey.append({"step": "RCA identified", "status": "done",
                        "detail": f"{category} — {confidence_lvl} confidence ({round(confidence * 100)}%)"})

        # ── New structured output fields ─────────────────────────────────────
        confidence_drivers = [
            {"factor": "Error identified", "met": bool(check.get("error"))},
            {"factor": "Related task correlated", "met": bool(root_task)},
            {"factor": "Lineage resolved", "met": bool(lineage_graph.get("nodes"))},
            {"factor": "DQ rule SQL analyzed", "met": bool(dq_rule_sql)},
            {"factor": "Supporting evidence collected",
             "met": bool(failure_evidence) or len(evidence) >= 3},
            {"factor": "Impact path validated", "met": len(impacted_keys) > 0},
            {"factor": "Prior case matched", "met": bool(all_priors)},
        ]

        dq_propagation: List[Dict[str, Any]] = []
        if root_task:
            dq_propagation.append({"name": root_task.get("name", ""), "type": "task", "status": "root_cause"})
        dq_propagation.append({"name": f"DQ: {check.get('name', '')}", "type": "dq", "status": "failed"})
        for k in impacted_keys[:2]:
            r = run_by_key.get(k, {})
            dq_propagation.append({"name": r.get("name", k), "type": "task", "status": "impacted"})
        for item in (downstream_consumers.get("details") or [])[:3]:
            if any(kw in item.get("name", "").lower() for kw in ("report", "dashboard", "mart", "bi")):
                dq_propagation.append({"name": item["name"], "type": "report", "status": "impacted"})
                break

        incident_summary_obj = {
            "failure_type": category,
            "failure_name": f"DQ: {check_label}",
            "environment": "Production",
            "detection_time": check.get("run_at"),
            "recorded_status": recorded_status,
            "current_status": live_status or recorded_status,
            "stale": bool(dq_currently_passing),
            "confidence_score": round(confidence * 100),
            "root_cause_category": category,
        }

        if root_cause_obj:
            root_cause_obj["business_explanation"] = (
                (llm.get("root_cause") or {}).get("business_explanation") if llm else None
            ) or summary
            root_cause_obj["technical_explanation"] = (
                (llm.get("root_cause") or {}).get("technical_explanation") if llm else None
            ) or detailed_analysis

        incident = self.incident_log.log({
            "pipeline_id": check_id, "name": check.get("name"), "platform": "dq",
            "status": check.get("status"), "signature": f"dq {category}", "source": "rca",
            "root_cause_node": root_key, "category": category, "rca_summary": summary,
        })

        result = {
            "incident_id": incident["incident_id"],
            "pipeline_id": check_id,
            "analysis_type": "dq",
            "root_cause_node": root_key,
            "root_cause_name": (root_task or check).get("name", check_id),
            "failure_type": category,
            "summary": summary,
            "detailed_analysis": detailed_analysis,
            "category": category,
            "confidence": confidence,
            "confidence_level": confidence_lvl,
            "evidence": evidence,
            "recommended_next": (
                "monitor" if dq_currently_passing
                else "fix" if (root_task or live_status == "FAILED")
                else "investigate"
            ),
            "recorded_status": recorded_status,
            "current_status": live_status or recorded_status,
            "stale": bool(dq_currently_passing),
            "live_failing_count": live_failing_count,
            "impact_assessment": impact_assessment,
            "impact_summary": impact_summary,
            "root_cause": root_cause_obj,
            "remediation": remediation,
            # Investigation dashboard fields
            "incident_summary": incident_summary_obj,
            "investigation_journey": journey,
            "failure_propagation": dq_propagation,
            "confidence_drivers": confidence_drivers,
            "structured_evidence": structured_evidence,
            # Lineage
            "upstream_lineage": dq_upstream_lineage,
            "upstream_lineage_text": upstream_text,
            "downstream_lineage": dq_downstream_lineage,
            "downstream_lineage_text": downstream_text,
            "lineage": lineage_graph,
            "lineage_table": lineage_table,
            "affected_tables": affected_tables,
            "related_dq_failures": [{"id": check_id, "name": check.get("name"),
                                     "tables": list(check_tables)}],
            "related_task_failures": [
                {"id": p["id"], "name": p.get("name"), "tables": p.get("tables", [])}
                for p in related_tasks
            ],
            "impacted_nodes": impacted_keys,
            "table_lineage": table_lineage,
            "downstream_consumers": downstream_consumers,
            "refinement": None,
            "code_analysis": code_analysis,
            "procedure_chain": procedure_chain[:3] if procedure_chain else None,
            "dq_execution_result": dq_execution_result if dq_execution_result and dq_execution_result.get("executed") else None,
            "diagnostic_results": diagnostic_results,
            "knowledge_applied": knowledge_rules[:3] if knowledge_rules else None,
            "seen_before": bool(all_priors),
        }
        result["chat_narrative"] = (
            self._generate_narrative(result) if include_narrative else None)
        self.learn({
            "signature": dq_signature,
            "pipeline_id": check_id,
            "category": category,
            "root_cause_node": root_key,
            "root_cause_name": (root_task or check).get("name", check_id),
            "error_pattern": (check.get("error") or "")[:200],
            "resolution_applied": None,
            "code_analysis_summary": (code_analysis.get("llm_explanation") or "")[:300],
            "tables_involved": list(check_tables)[:10],
            "severity": impact_assessment.get("business_severity"),
            "confidence": confidence,
        })
        return result

    def _refine(self, target, root, category, hint: str, idx, run_by_key) -> Optional[Dict[str, Any]]:
        h = hint.lower()
        for nid, n in {**idx, **{k: v for k, v in run_by_key.items()}}.items():
            short = (n.get("task_key") or nid).split(".")[-1].lower()
            name_part = n.get("name", "").lower().split(":")[-1].strip()
            if (short and short in h) or (name_part and name_part in h):
                key = n.get("task_key") or nid
                if n.get("status") in ("FAILED", "DELAYED") and key != root.get("task_key", root.get("id")):
                    return {"note": f"user hint pointed to '{n['name']}' — re-evaluated as candidate root",
                            "new_root": key}
        return {"note": f"incorporated user context: '{hint}' (no root change; confidence raised)",
                "new_root": None}
