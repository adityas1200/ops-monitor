from __future__ import annotations
import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from app.agents.base import BaseAgent
from app.agents.monitoring_agent import MonitoringAgent
from app.agents.rca_agent import CATEGORY_DQ, RCAAgent, _classify

# SQL keywords / noise that can look like identifiers in FROM/JOIN scans.
_SQL_NOISE = {
    "SELECT", "FROM", "WHERE", "JOIN", "LEFT", "RIGHT", "INNER", "OUTER", "FULL",
    "CROSS", "ON", "AND", "OR", "AS", "WITH", "INSERT", "INTO", "UPDATE", "DELETE",
    "MERGE", "USING", "VALUES", "SET", "GROUP", "BY", "ORDER", "HAVING", "LIMIT",
    "UNION", "ALL", "DISTINCT", "CASE", "WHEN", "THEN", "ELSE", "END", "NULL",
    "TRUE", "FALSE", "CREATE", "REPLACE", "TABLE", "VIEW", "PROCEDURE", "TASK",
    "QUALIFY", "OVER", "PARTITION", "ROW_NUMBER", "COUNT", "SUM", "AVG", "MIN",
    "MAX", "CAST", "COALESCE", "NULLIF", "TRY_TO_NUMBER", "LATERAL", "FLATTEN",
}

# Patterns that match schema.object or db.schema.object style refs.
_OBJ_REF_RE = re.compile(
    r"(?i)\b(?:FROM|JOIN|INTO|UPDATE|MERGE\s+INTO|TABLE|USING)\s+"
    r"((?:\"?[A-Za-z_][\w$]*\"?\.){1,2}\"?[A-Za-z_][\w$]*\"?)"
)
_BARE_QUALIFIED_RE = re.compile(
    r"\b((?:[A-Za-z_][\w$]*\.){1,2}[A-Za-z_][\w$]*)\b"
)


def _is_dq(pipeline_id: str, rca: Optional[Dict[str, Any]]) -> bool:
    if str(pipeline_id).startswith("dq_"):
        return True
    if not rca:
        return False
    if rca.get("analysis_type") == "dq":
        return True
    cat = str(rca.get("category") or "")
    return cat == CATEGORY_DQ or "data quality" in cat.lower()


def _extract_sql_body(text: Optional[str]) -> str:
    """Pull SQL_CODE out of a DQ rule dump, or return the text as-is."""
    if not text:
        return ""
    s = str(text)
    for marker in ("SQL_CODE:", "sql_code:"):
        if marker in s:
            body = s.split(marker, 1)[1].strip()
            m = re.search(r"\n[A-Z_][A-Z0-9_]+:\s", body)
            if m:
                body = body[: m.start()].strip()
            return body
    return s.strip()


def _normalize_obj(name: str) -> str:
    return re.sub(r'"', "", str(name or "")).strip().upper()


def _extract_sql_object_refs(sql: Optional[str]) -> Set[str]:
    """Collect schema-qualified object names referenced in SQL."""
    if not sql:
        return set()
    found: Set[str] = set()
    for m in _OBJ_REF_RE.finditer(sql):
        ref = _normalize_obj(m.group(1))
        if ref and not any(p in _SQL_NOISE for p in ref.split(".")):
            found.add(ref)
    # Also catch fully-qualified names appearing outside FROM/JOIN (e.g. comments
    # are ignored; DDL CREATE PROC names and INSERT targets already covered).
    for m in _BARE_QUALIFIED_RE.finditer(sql):
        ref = _normalize_obj(m.group(1))
        parts = ref.split(".")
        if len(parts) < 2:
            continue
        if any(p in _SQL_NOISE for p in parts):
            continue
        # Ignore obvious float / decimal literals like 18.2
        if all(p.isdigit() for p in parts):
            continue
        found.add(ref)
    return found


def _procedure_chain(rca: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not rca:
        return []
    chain = rca.get("procedure_chain") or []
    return [c for c in chain if isinstance(c, dict)]


def _pick_upstream_procedure_sql(rca: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Pick real upstream procedure/view SQL that writes/reads the failing tables."""
    chain = _procedure_chain(rca)
    if not chain:
        return None
    tables = {
        _normalize_obj(t)
        for t in (
            (rca.get("code_analysis") or {}).get("tables_inspected")
            or rca.get("affected_tables")
            or []
        )
        if t
    }
    # Prefer a procedure whose outputs intersect failing tables.
    for c in chain:
        outputs = {_normalize_obj(o) for o in (c.get("outputs") or []) if o}
        body = (c.get("sql_body") or "").strip()
        if not body:
            continue
        if tables and outputs and (outputs & tables or any(
            any(t.endswith(f".{o.split('.')[-1]}") or o.endswith(f".{t.split('.')[-1]}")
                for o in outputs)
            for t in tables
        )):
            return {
                "object_name": str(c.get("object_name") or "upstream_procedure"),
                "body": body,
                "source_kind": "procedure",
            }
    # Else first chain entry with a real body (closest to the failed task).
    for c in chain:
        body = (c.get("sql_body") or "").strip()
        if body:
            return {
                "object_name": str(c.get("object_name") or "upstream_procedure"),
                "body": body,
                "source_kind": "procedure",
            }
    return None


def _allowed_objects(artifact: Dict[str, str], rca: Optional[Dict[str, Any]]) -> Set[str]:
    """Allowlist of real objects the fix SQL may reference."""
    allowed: Set[str] = set()
    allowed |= _extract_sql_object_refs(artifact.get("body"))
    if rca:
        code = rca.get("code_analysis") or {}
        for t in (code.get("tables_inspected") or rca.get("affected_tables") or []):
            n = _normalize_obj(t)
            if n:
                allowed.add(n)
        diag_sql = (rca.get("diagnostic_results") or {}).get("diagnostic_sql")
        allowed |= _extract_sql_object_refs(diag_sql)
        allowed |= _extract_sql_object_refs(code.get("task_sql"))
        for c in _procedure_chain(rca):
            if c.get("object_name"):
                allowed.add(_normalize_obj(c["object_name"]))
            allowed |= _extract_sql_object_refs(c.get("sql_body"))
            for lst_key in ("inputs", "outputs"):
                for o in c.get(lst_key) or []:
                    n = _normalize_obj(o)
                    if n:
                        allowed.add(n)
    # Drop placeholders / empty
    return {a for a in allowed if a and "<" not in a and ">" not in a}


def _invented_objects(sql: Optional[str], allowed: Set[str]) -> List[str]:
    """Return object refs in sql that are not covered by the allowlist."""
    if not sql or not allowed:
        # No allowlist → cannot prove invention; treat as unavailable rather than invent.
        refs = _extract_sql_object_refs(sql)
        if not allowed:
            return sorted(refs) if refs and sql and "INSERT INTO" in sql.upper() else []
        return []

    # Expand allowlist with short names and last-two-part suffixes for matching.
    expanded: Set[str] = set(allowed)
    for a in list(allowed):
        parts = a.split(".")
        expanded.add(parts[-1])
        if len(parts) >= 2:
            expanded.add(".".join(parts[-2:]))

    invented: List[str] = []
    for ref in sorted(_extract_sql_object_refs(sql)):
        parts = ref.split(".")
        candidates = {ref, parts[-1]}
        if len(parts) >= 2:
            candidates.add(".".join(parts[-2:]))
        if candidates & expanded:
            continue
        invented.append(ref)
    return invented


def _is_unavailable_artifact(body: Optional[str]) -> bool:
    if not body or not str(body).strip():
        return True
    b = str(body).strip()
    return b.startswith("-- Source artifact") or "source sql unavailable" in b.lower()


def _evidence_pack(rca: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not rca:
        return {}
    diag = rca.get("diagnostic_results") or {}
    facts = diag.get("evidence_facts") or rca.get("mandatory_evidence_facts") or []
    if not facts and rca.get("evidence"):
        facts = [e for e in rca["evidence"] if isinstance(e, str)][:8]
    summary = (
        diag.get("evidence_summary")
        or (rca.get("root_cause") or {}).get("explanation")
        or rca.get("summary")
        or ""
    )
    return {
        "facts": facts[:12] if isinstance(facts, list) else [],
        "summary": str(summary)[:800],
        "sample_rows": (diag.get("sample_rows") or [])[:5],
        "diagnostic_sql": diag.get("diagnostic_sql"),
        "execution_error": (
            (rca.get("dq_execution_result") or {}).get("error")
            if isinstance(rca.get("dq_execution_result"), dict)
            else None
        ),
    }


def _looks_like_python(body: Optional[str]) -> bool:
    if not body:
        return False
    b = body.lstrip()
    return bool(
        re.search(r"(?m)^(import |from |def |class )", b)
        or "snowpark" in b.lower()
        or "sc.sql(" in b
        or ".sql(" in b and "def main" in b
    )


def _looks_like_sql(body: Optional[str]) -> bool:
    if not body or _looks_like_python(body):
        return False
    head = body.lstrip()[:80].upper()
    return head.startswith((
        "SELECT", "WITH", "INSERT", "UPDATE", "DELETE", "MERGE", "CREATE",
        "BEGIN", "CALL", "--",
    )) or "SELECT" in body.upper()[:500]


def _extract_embedded_sqls(body: str) -> List[str]:
    """Pull SQL strings out of Snowpark/Python procedure bodies."""
    if not body:
        return []
    snippets: List[str] = []
    for m in re.finditer(
        r"""(?:sc\.)?sql\s*\(\s*(?:f)?(?P<q>\"\"\"|'''|\"|')(?P<body>.*?)(?P=q)\s*\)""",
        body,
        re.I | re.S,
    ):
        sql = (m.group("body") or "").strip()
        if sql and len(sql) > 40:
            snippets.append(sql)
    # Also catch triple-quoted blocks assigned near SELECT
    for m in re.finditer(r'(?P<q>\"\"\"|\'\'\')(?P<body>\s*(?:SELECT|WITH|INSERT|MERGE)\b.*?)(?P=q)',
                         body, re.I | re.S):
        sql = (m.group("body") or "").strip()
        if sql and len(sql) > 40 and sql not in snippets:
            snippets.append(sql)
    return snippets


def _best_sql_snippet(
    snippets: List[str],
    tables: Optional[List[str]] = None,
    prefer_join: bool = True,
) -> str:
    if not snippets:
        return ""
    bare_tables = {
        str(t).split(".")[-1].upper()
        for t in (tables or [])
        if t
    }

    def score(sql: str) -> int:
        u = sql.upper()
        s = 0
        if prefer_join and "JOIN" in u:
            s += 5
        if "CLAIM_ID" in u:
            s += 3
        if "SELECT" in u:
            s += 1
        for bare in bare_tables:
            if bare and bare in u:
                s += 4
        s += min(len(sql), 2000) // 500  # slight preference for fuller snippets
        return s

    return max(snippets, key=score)


def _focused_sql_artifact(
    raw_body: str,
    rca: Dict[str, Any],
    object_name: str,
) -> Dict[str, str]:
    """Return a focused SQL-only artifact (never dump full Snowpark Python)."""
    tables = list(
        ((rca.get("code_analysis") or {}).get("tables_inspected") or [])
        or [t.get("table") if isinstance(t, dict) else t for t in (rca.get("affected_tables") or [])]
    )
    if _looks_like_python(raw_body):
        snippet = _best_sql_snippet(_extract_embedded_sqls(raw_body), tables=tables)
        if snippet:
            return {
                "platform": "snowflake",
                "object_name": object_name,
                "language": "sql",
                "body": snippet[:4000],
                "source_kind": "procedure_sql_snippet",
            }
    if _looks_like_sql(raw_body):
        return {
            "platform": "snowflake",
            "object_name": object_name,
            "language": "sql",
            "body": raw_body[:4000],
            "source_kind": "procedure",
        }
    return {
        "platform": "snowflake",
        "object_name": object_name,
        "language": "sql",
        "body": "",
        "source_kind": "unavailable",
    }


def _artifact_from_pipeline(pipeline_id: str, idx: Dict[str, Any]) -> Dict[str, str]:
    p = idx.get(pipeline_id, {})
    platform = p.get("platform", "unknown")
    lang = "sql" if platform in ("snowflake", "dq") else ("json" if platform == "dynamodb" else "python")
    err = p.get("error") or ""
    name = pipeline_id.split("_", 1)[-1] if "_" in pipeline_id else pipeline_id
    body = err if err else (
        f"-- Source SQL unavailable for {name} ({platform}). "
        f"Fetch the live task/procedure/rule definition, then re-suggest."
    )
    return {
        "platform": platform,
        "object_name": name,
        "language": lang,
        "body": body,
        "source_kind": "unavailable" if not err else "error_text",
    }


def _artifact_from_rca(pipeline_id: str, rca: Dict[str, Any],
                       idx: Dict[str, Any]) -> Dict[str, str]:
    """Prefer focused real SQL from RCA; never invent ETL or dump Snowpark Python."""
    code = rca.get("code_analysis") or {}
    task_sql = _extract_sql_body(code.get("task_sql"))
    root_id = rca.get("root_cause_node") or pipeline_id
    name = rca.get("root_cause_name") or (
        pipeline_id.split("_", 1)[-1] if "_" in pipeline_id else pipeline_id
    )

    if _is_dq(pipeline_id, rca):
        # Fix Before must match Code Analysis: always prefer the DQ rule SQL_CODE.
        # Upstream procedure bodies are for permanent QUALIFY remediation, not Before.
        body = (
            task_sql
            or (rca.get("diagnostic_results") or {}).get("diagnostic_sql")
            or ""
        )
        if body and _looks_like_sql(body):
            return {
                "platform": "snowflake",
                "object_name": str(name),
                "language": "sql",
                "body": body[:4000],
                "source_kind": "dq_rule",
            }
        # Last resort: focused SQL from procedure chain (still SQL-only).
        upstream = _pick_upstream_procedure_sql(rca)
        if upstream and upstream.get("body"):
            focused = _focused_sql_artifact(upstream["body"], rca, upstream["object_name"])
            if focused.get("body"):
                return focused
        return {
            "platform": "snowflake",
            "object_name": str(name),
            "language": "sql",
            "body": (
                f"-- Source SQL unavailable for DQ check {name}.\n"
                f"-- DQ rule SQL_CODE was not present on the RCA payload. "
                f"Re-run RCA or paste the rule SQL via Modify fix.\n"
            ),
            "source_kind": "unavailable",
        }

    if task_sql:
        platform = (idx.get(root_id) or idx.get(pipeline_id) or {}).get("platform", "snowflake")
        lang = "sql" if platform == "snowflake" else ("json" if platform == "dynamodb" else "python")
        return {
            "platform": platform,
            "object_name": str(name),
            "language": lang,
            "body": task_sql[:4000],
            "source_kind": "task_sql",
        }

    return _artifact_from_pipeline(root_id if root_id in idx else pipeline_id, idx)


def _grain_keys_from_evidence(ev: Dict[str, Any], rca: Dict[str, Any]) -> List[str]:
    """Infer PARTITION BY columns from evidence facts / root-cause text."""
    facts = [str(f) for f in (ev.get("facts") or [])[:5]]
    blob = " | ".join(facts) or str(ev.get("summary") or "")
    skip = {
        "COUNT", "CNT", "N", "RN", "ROW_NUMBER",
        "GRAIN1", "GRAIN2", "GRAIN3", "GRAIN1_VALUE", "GRAIN2_VALUE", "GRAIN3_VALUE",
        "CURR_CNT", "PREV_CNT", "DEVIATION_PCT", "DEVIATION", "TABLE_NAME", "METRIC",
        "COMMENTS", "RESULT", "RESULT_COUNT",
    }
    keys: List[str] = []
    for m in re.finditer(r"\b([A-Za-z_][\w]*)\s*=", blob):
        col = m.group(1).upper()
        if col in skip or col in keys:
            continue
        keys.append(col)
        if len(keys) >= 4:
            break
    if not keys:
        expl = str((rca.get("root_cause") or {}).get("explanation") or rca.get("summary") or "")
        for col in ("CLAIM_ID", "PTNT_ID", "SOURCE_TYP", "PATIENT_ID", "BRAND_CD"):
            if col in expl.upper() and col not in keys:
                keys.append(col)
    return keys or ["CLAIM_ID"]


def _is_trend_or_threshold_failure(err_blob: str) -> bool:
    return bool(re.search(
        r"\b(deviation(?:_pct)?|trend|threshold|curr_cnt|prev_cnt|wow|"
        r"week[- ]over[- ]week|upper_threshold|lower_threshold)\b",
        err_blob,
        re.I,
    ))


def _is_duplicate_failure(err_blob: str) -> bool:
    """True only for real duplicate/uniqueness failures — not GRAIN1 trend metrics."""
    if _is_trend_or_threshold_failure(err_blob):
        return False
    if re.search(r"\b(duplicate|duplicates|duplication|dedup|dedupe|row_number|"
                 r"primary\s+key|uniqueness|unique\s+constraint)\b", err_blob, re.I):
        return True
    # Standalone "grain breach" / "expected grain" — not GRAIN1_VALUE columns.
    if re.search(r"\b(grain\s+breach|expected\s+grain|one[- ]row[- ]per|"
                 r"duplicate\s+claim|appears\s+\d+\s+times)\b", err_blob, re.I):
        return True
    return False


def _where_is_sane(where: str) -> bool:
    if not where or len(where) > 800:
        return False
    if where.count("(") != where.count(")"):
        return False
    # Reject mid-join fragments from nested SQL.
    bad = (" JOIN ", ") OVR ", ") BASE", " ON ", " LEFT ", " RIGHT ", " INNER ", " FROM ")
    u = f" {where.upper()} "
    if any(b in u for b in bad):
        return False
    return True


def _extract_where_clause(sql: str) -> str:
    if not sql:
        return ""
    # Prefer the last WHERE before a terminal GROUP BY / HAVING / ORDER BY / QUALIFY.
    matches = list(re.finditer(
        r"\bWHERE\b\s+(.+?)(?=\bGROUP\s+BY\b|\bQUALIFY\b|\bORDER\s+BY\b|\bHAVING\b|$)",
        sql,
        re.I | re.S,
    ))
    for m in reversed(matches):
        where = re.sub(r"\s+", " ", m.group(1)).strip().rstrip(";")
        if _where_is_sane(where):
            return where
    return ""


def _offender_keys_from_evidence(ev: Dict[str, Any], key: str = "GRAIN1_VALUE") -> List[str]:
    keys: List[str] = []
    pat = re.compile(rf"\b{re.escape(key)}\s*=\s*([^,|;]+)", re.I)
    for f in (ev.get("facts") or [])[:12]:
        m = pat.search(str(f))
        if m:
            val = m.group(1).strip().strip("'\"")
            if val and val not in keys:
                keys.append(val)
    return keys[:8]


def _rca_guidance(rca: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Pull remediation + root-cause text from RCA — Fix must follow this, not templates."""
    rca = rca or {}
    rem = rca.get("remediation") if isinstance(rca.get("remediation"), dict) else {}
    rc = rca.get("root_cause") if isinstance(rca.get("root_cause"), dict) else {}
    comparison = rc.get("comparison") if isinstance(rc.get("comparison"), dict) else {}
    return {
        "immediate": str(rem.get("immediate_fix") or "").strip(),
        "permanent": str(rem.get("permanent_fix") or "").strip(),
        "monitoring": str(rem.get("monitoring_recommendation") or "").strip(),
        "explanation": str(
            rc.get("explanation") or rca.get("summary") or ""
        ).strip(),
        "technical": str(rc.get("technical_explanation") or "").strip(),
        "expected": str(
            rc.get("expected") or comparison.get("expected") or ""
        ).strip(),
        "actual": str(
            rc.get("actual") or comparison.get("actual") or ""
        ).strip(),
        "detailed": str(rca.get("detailed_analysis") or "").strip(),
    }


def _sql_embedded_in_text(text: str) -> str:
    """Return SQL only when RCA remediation already embeds it — never invent."""
    if not text:
        return ""
    m = re.search(r"```(?:sql)?\s*([\s\S]*?)```", text, re.I)
    if m:
        body = (m.group(1) or "").strip()
        if _looks_like_sql(body):
            return body
    # Workbench / RCA often wraps SQL as {{code:SELECT ...}}
    code_bits: List[str] = []
    for m in re.finditer(r"\{\{code:([\s\S]*?)\}\}", text, re.I):
        bit = (m.group(1) or "").strip()
        if bit and _looks_like_sql(bit):
            code_bits.append(bit.rstrip(";").strip())
    if code_bits:
        # Prefer the longest runnable SQL snippet from remediation.
        best = max(code_bits, key=len)
        if len(best) > 40:
            return best if best.endswith(";") else best + ";"
    stripped = text.strip()
    if _looks_like_sql(stripped) and len(stripped) > 60 and "\n" in stripped:
        return stripped
    return ""


def _strip_narrative_from_sql(sql: Optional[str]) -> str:
    """Keep runnable SQL only — drop RCA/fix narrative comment footers."""
    if not sql:
        return ""
    narrative_prefixes = (
        "-- rca-guided",
        "-- translate the rca",
        "-- immediate:",
        "-- permanent:",
        "-- llm polish discarded",
        "-- kept rca-guided",
        "-- kept evidence-seeded",
        "-- insufficient mapping",
        "-- insufficient context",
        "-- do not invent",
        "-- evidence:",
        "-- no hardcoded",
        "-- use rca",
        "-- refine rca",
        "-- rca remediation",
    )
    lines = str(sql).splitlines()
    kept: List[str] = []
    for line in lines:
        low = line.strip().lower()
        if any(low.startswith(p) for p in narrative_prefixes):
            # Drop this and any following narrative comment lines.
            break
        kept.append(line)
    # Also drop trailing empty / lone-comment noise after SQL body.
    while kept and not kept[-1].strip():
        kept.pop()
    while kept and kept[-1].strip().startswith("--") and not _looks_like_sql(kept[-1]):
        # Keep inline SQL comments that sit mid-query; only strip trailing comment-only tails
        # if the previous non-empty line already looks like finished SQL.
        body_so_far = "\n".join(kept[:-1]).strip()
        if body_so_far and (
            body_so_far.rstrip().endswith(";")
            or _looks_like_sql(body_so_far)
        ):
            kept.pop()
            while kept and not kept[-1].strip():
                kept.pop()
            continue
        break
    out = "\n".join(kept).strip()
    return out


def _sql_after_from_rca(seed: Dict[str, Any], before: str) -> str:
    """Build After as SQL only from RCA-embedded statements (no narrative comments)."""
    for key in ("rca_immediate", "rca_permanent"):
        sql = _sql_embedded_in_text(str(seed.get(key) or ""))
        if sql:
            return _strip_narrative_from_sql(sql)
    # If remediation embeds QUALIFY … PARTITION BY cols, apply to before SELECT.
    rem = " ".join([
        str(seed.get("rca_immediate") or ""),
        str(seed.get("rca_permanent") or ""),
    ])
    qm = re.search(
        r"QUALIFY\s+ROW_NUMBER\s*\(\s*\)\s*OVER\s*\(\s*PARTITION\s+BY\s+([^)]+?)(?:\s+ORDER\s+BY\s+([^)]+))?\)\s*=\s*1",
        rem,
        re.I | re.S,
    )
    if qm and before and _looks_like_sql(before) and not _looks_like_python(before):
        parts = [p.strip() for p in qm.group(1).split(",") if p.strip()]
        order = None
        if qm.group(2):
            order = [p.strip() for p in qm.group(2).split(",") if p.strip()]
        if parts:
            return _strip_narrative_from_sql(_inject_qualify(before, parts, order))
    return ""


def _title_from_rca(guidance: Dict[str, str], obj: str) -> str:
    src = guidance.get("immediate") or guidance.get("explanation") or ""
    if src:
        # Drop {{code:...}} / {{table:...}} markup so titles stay readable.
        clean = re.sub(r"\{\{(?:code|table|procedure):([\s\S]*?)\}\}", r"\1", src)
        clean = re.sub(r"\s+", " ", clean).strip()
        first = re.split(r"[.\n]", clean, maxsplit=1)[0].strip()
        if 12 <= len(first) <= 120:
            return first
        if first:
            return first[:117] + "…"
    return f"RCA-guided remediation for {obj}"


def _seed_from_rca(
    artifact: Dict[str, str],
    rca: Dict[str, Any],
    ev: Dict[str, Any],
) -> Dict[str, Any]:
    """Seed Fix from RCA remediation/root cause — no category SQL templates."""
    guidance = _rca_guidance(rca)
    obj = artifact.get("object_name") or "target"
    before = artifact.get("body") or ""
    facts = [str(f) for f in (ev.get("facts") or [])[:6]]
    fact_line = "; ".join(facts[:3]) if facts else (
        ev.get("summary") or guidance.get("explanation") or ""
    )

    rationale_parts = [
        p for p in (
            guidance.get("explanation"),
            f"Immediate (RCA): {guidance['immediate']}" if guidance.get("immediate") else "",
            f"Permanent (RCA): {guidance['permanent']}" if guidance.get("permanent") else "",
            f"Expected: {guidance['expected']}" if guidance.get("expected") else "",
            f"Actual: {guidance['actual']}" if guidance.get("actual") else "",
            f"Evidence: {fact_line[:300]}" if fact_line else "",
        ) if p
    ]
    rationale = " ".join(rationale_parts)[:1200] or (
        f"Follow RCA findings for {obj}; no category template applied."
    )

    after_sql = (
        _sql_embedded_in_text(guidance.get("immediate") or "")
        or _sql_embedded_in_text(guidance.get("permanent") or "")
    )
    # If RCA did not embed SQL, leave after empty — LLM must produce SQL-only after.
    # Never append RCA narrative comments into after.
    after = _strip_narrative_from_sql(after_sql[:4000] if after_sql else "")
    if not after and before:
        # Try QUALIFY-from-remediation applied to before (still SQL-only).
        after = _sql_after_from_rca(
            {
                "rca_immediate": guidance.get("immediate") or "",
                "rca_permanent": guidance.get("permanent") or "",
            },
            before,
        )

    hints: List[str] = []
    if guidance.get("immediate"):
        hints.append(f"Confirm RCA immediate action: {guidance['immediate'][:200]}")
    if guidance.get("monitoring"):
        hints.append(guidance["monitoring"][:240])
    if fact_line:
        hints.append("Re-check named offenders from RCA evidence after applying the fix")
    if not hints:
        hints = ["Re-run failing check/task and confirm RCA expected condition holds"]

    return {
        "title": _title_from_rca(guidance, obj),
        "rationale": rationale,
        "risk": "medium",
        "rollback": "Revert any SQL/config change; re-run RCA if findings change.",
        "before": _strip_narrative_from_sql(before[:4000]),
        "after": after,
        "language": artifact.get("language") or "sql",
        "validation_hints": hints[:5],
        "grounding": "rca_guided",
        "evidence_summary": fact_line[:500],
        "rca_immediate": guidance.get("immediate") or "",
        "rca_permanent": guidance.get("permanent") or "",
    }


def _rca_after_fallback(seed: Dict[str, Any], before: str) -> str:
    """SQL-only fallback from RCA-embedded statements — never narrative comments."""
    return _sql_after_from_rca(seed, before)


def _dq_err_blob(rca: Optional[Dict[str, Any]], ev: Optional[Dict[str, Any]]) -> str:
    """Concatenate RCA + evidence text used to classify DQ failure kind."""
    rca = rca or {}
    ev = ev or {}
    g = _rca_guidance(rca)
    parts = [
        ev.get("summary") or "",
        " ".join(str(f) for f in (ev.get("facts") or [])[:8]),
        g.get("explanation") or "",
        g.get("detailed") or "",
        g.get("immediate") or "",
        g.get("permanent") or "",
        g.get("actual") or "",
        rca.get("summary") or "",
    ]
    return " ".join(p for p in parts if p)


def _is_unsafe_after_sql(sql: Optional[str]) -> bool:
    """Reject After SQL that is not a safe, Snowflake-valid mutation/investigation."""
    if not sql or not str(sql).strip():
        return True
    u = str(sql).upper()
    # Oracle-style ROWID / remediation DELETE snippets are not reliable Snowflake fixes.
    if re.search(r"\bROWID\b", u):
        return True
    if re.search(r"\bDELETE\s+FROM\b", u):
        return True
    if re.search(r"\bSTAGING\s*\.", u):
        return True
    if re.search(r"\bTRUNCATE\s+TABLE\b", u):
        return True
    return False


def _is_safe_dedupe_after(sql: Optional[str]) -> bool:
    if not sql:
        return False
    u = str(sql).upper()
    if _is_unsafe_after_sql(sql):
        return False
    return ("ROW_NUMBER" in u and ("CREATE OR REPLACE" in u or "QUALIFY" in u)) or (
        "QUALIFY" in u and "ROW_NUMBER" in u
    )


def _offender_values_by_column(ev: Optional[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Parse evidence facts like COL=value into column → [values]."""
    skip = {
        "COUNT", "CNT", "N", "RN", "DUP_CNT", "RESULT", "RESULT_COUNT",
        "CURR_CNT", "PREV_CNT", "DEVIATION_PCT", "DEVIATION", "DIFF_NBRX",
        "TABLE_NAME", "METRIC", "COMMENTS", "SOURCE", "TARGET",
    }
    out: Dict[str, List[str]] = {}
    for f in ((ev or {}).get("facts") or [])[:20]:
        for m in re.finditer(r"\b([A-Za-z_][\w]*)\s*=\s*([^,|;]+)", str(f)):
            col = m.group(1).upper()
            if col in skip:
                continue
            val = m.group(2).strip().strip("'\"")
            if not val or val.upper() in ("NULL", "NONE"):
                continue
            bucket = out.setdefault(col, [])
            if val not in bucket:
                bucket.append(val)
    return out


def _offender_investigation_sql(before: str, ev: Optional[Dict[str, Any]]) -> str:
    """Wrap the DQ rule as an investigation SELECT filtered to evidence offenders.

    Produces After that is always distinct from Before (WITH _dq_check wrapper).
    """
    body = _strip_narrative_from_sql(before or "")
    if not body or not _looks_like_sql(body) or _looks_like_python(body):
        return ""
    head = body.lstrip()[:12].upper()
    if not head.startswith(("SELECT", "WITH")):
        return ""

    by_col = _offender_values_by_column(ev)
    # Prefer identity-style grains over metrics.
    preferred = (
        "GRAIN1_VALUE", "GRAIN2_VALUE", "GRAIN3_VALUE",
        "SALE_ORG_NM", "SALE_ORG_ID", "TERRITORY", "CLAIM_ID", "PTNT_ID",
    )
    col = next((c for c in preferred if c in by_col and by_col[c]), None)
    if not col:
        col = next(iter(by_col), None) if by_col else None

    core = body.rstrip().rstrip(";")
    if not col or not by_col.get(col):
        # Still differentiate from Before so UI is not a twin copy.
        return (
            f"WITH _dq_check AS (\n{core}\n)\n"
            f"SELECT * FROM _dq_check\n"
            f"WHERE 1=1  /* review flagged rows from RCA evidence; no table rewrite */;"
        )

    vals = by_col[col][:20]
    in_list = ", ".join("'" + v.replace("'", "''") + "'" for v in vals)
    return (
        f"WITH _dq_check AS (\n{core}\n)\n"
        f"SELECT * FROM _dq_check\n"
        f"WHERE {col} IN ({in_list});"
    )


def _investigative_dq_after(
    before: str,
    rca: Optional[Dict[str, Any]],
    ev: Optional[Dict[str, Any]],
) -> str:
    """SELECT-only After for trend/threshold — never rewrite the fact table."""
    focused = _offender_investigation_sql(before, ev)
    if focused and not _is_vague_after(focused, before):
        return focused
    diag = (rca or {}).get("diagnostic_results") if isinstance(
        (rca or {}).get("diagnostic_results"), dict
    ) else {}
    for candidate in (
        (ev or {}).get("diagnostic_sql"),
        diag.get("diagnostic_sql") if diag else None,
    ):
        sql = _strip_narrative_from_sql(candidate or "")
        if (
            sql and _looks_like_sql(sql) and not _looks_like_python(sql)
            and not _is_vague_after(sql, before)
            and not _is_unsafe_after_sql(sql)
        ):
            return sql[:4000]
    return focused  # may be wrapper with WHERE 1=1 — still distinct from Before


def _rule_sql_from_rca(rca: Optional[Dict[str, Any]]) -> str:
    code = (rca or {}).get("code_analysis") or {}
    return _extract_sql_body(code.get("task_sql")) or ""


def _ensure_dq_sql_after(
    artifact: Dict[str, str],
    rca: Optional[Dict[str, Any]],
    ev: Optional[Dict[str, Any]],
    before: str,
    after: Optional[str] = None,
) -> Tuple[str, str]:
    """Generic DQ Before/After: templates from rule + evidence, never unsafe prose SQL.

    - Before = DQ rule SQL (Code Analysis) when available
    - Duplicate → parameterized ROW_NUMBER dedupe (ignores remediation DELETE/ROWID)
    - Trend/threshold → offender investigation SELECT (never twin of Before, never rewrite)
    - Else → safe QUALIFY inject or safe remediation SQL only
    """
    rule_sql = _strip_narrative_from_sql(_rule_sql_from_rca(rca))
    before_sql = _strip_narrative_from_sql(before or "")
    if rule_sql and _looks_like_sql(rule_sql):
        before_sql = rule_sql[:4000]

    if _is_unavailable_artifact(before_sql) and not (ev or {}).get("facts"):
        return before_sql, ""

    err_blob = _dq_err_blob(rca, ev)
    guidance = _rca_guidance(rca)
    seed_bits = {
        "rca_immediate": guidance.get("immediate") or "",
        "rca_permanent": guidance.get("permanent") or "",
    }
    keys = _grain_keys_from_evidence(ev or {}, rca or {})

    # Incoming After from remediation/LLM is only kept if safe AND not overridden
    # by a stronger template below.
    incoming = _strip_narrative_from_sql(after or "")
    incoming_ok = (
        bool(incoming)
        and not _is_vague_after(incoming, before_sql)
        and not _is_unsafe_after_sql(incoming)
    )

    # --- Trend / threshold: investigation only ---
    if _is_trend_or_threshold_failure(err_blob) or any(
        k in err_blob.lower() for k in (
            "not data corruption", "market expansion", "systematic market",
            "threshold review", "adjust the threshold", "no table rewrite",
        )
    ):
        inv = _investigative_dq_after(before_sql, rca, ev)
        if inv:
            return before_sql, inv
        if incoming_ok and "CREATE OR REPLACE" not in incoming.upper() and "ROW_NUMBER" not in incoming.upper():
            return before_sql, incoming
        return before_sql, ""

    # --- Duplicate / grain: always deterministic dedupe template ---
    if _is_duplicate_failure(err_blob):
        table = _primary_fact_table(before_sql, rca)
        if table and keys:
            where = _extract_where_clause(before_sql)
            _diag, dedupe_after = _dedupe_table_sql(table, keys, where=where or "")
            if dedupe_after and _is_safe_dedupe_after(dedupe_after):
                # Keep Before = rule SQL (Code Analysis), After = table dedupe.
                return before_sql, _strip_narrative_from_sql(dedupe_after)
        if keys and before_sql and _looks_like_sql(before_sql):
            head = before_sql.lstrip()[:12].upper()
            # Only QUALIFY-inject when Before is a load SELECT, not an aggregate check.
            if head.startswith(("SELECT", "WITH")) and "HAVING" not in before_sql.upper():
                injected = _inject_qualify(before_sql, keys)
                if _is_safe_dedupe_after(injected) or (
                    "QUALIFY" in injected.upper() and not _is_vague_after(injected, before_sql)
                ):
                    return before_sql, _strip_narrative_from_sql(injected)
        # Never fall through to remediation DELETE.
        return before_sql, ""

    # --- Other DQ: accept safe incoming, else QUALIFY / safe rem ---
    if incoming_ok and (
        _is_safe_dedupe_after(incoming)
        or incoming.lstrip().upper().startswith(("SELECT", "WITH"))
    ):
        return before_sql, incoming

    rem_sql = _sql_after_from_rca(seed_bits, before_sql)
    if (
        rem_sql
        and not _is_unsafe_after_sql(rem_sql)
        and not _is_vague_after(rem_sql, before_sql)
    ):
        return before_sql, rem_sql

    if keys and before_sql and _looks_like_sql(before_sql) and not _looks_like_python(before_sql):
        head = before_sql.lstrip()[:12].upper()
        if head.startswith(("SELECT", "WITH")) and "HAVING" not in before_sql.upper():
            injected = _inject_qualify(before_sql, keys)
            if not _is_vague_after(injected, before_sql):
                return before_sql, _strip_narrative_from_sql(injected)

    inv = _investigative_dq_after(before_sql, rca, ev)
    if inv and not _is_vague_after(inv, before_sql):
        return before_sql, inv
    return before_sql, ""



def _contradicts_rca(
    llm_fix: Dict[str, Any],
    rca: Optional[Dict[str, Any]],
    seed: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Reject LLM after that fights the RCA narrative (e.g. dedupe when RCA says market signal)."""
    g = _rca_guidance(rca)
    blob = " ".join([
        g.get("immediate") or "",
        g.get("permanent") or "",
        g.get("explanation") or "",
        g.get("detailed") or "",
        g.get("actual") or "",
        str((seed or {}).get("rationale") or ""),
    ]).lower()
    after_u = str(llm_fix.get("after") or "").upper()
    no_rewrite = any(k in blob for k in (
        "not data corruption",
        "market expansion",
        "systematic market",
        "not a data quality",
        "not corruption",
        "threshold review",
        "adjust the threshold",
        "threshold change",
        "business threshold",
        "no table rewrite",
        "investigation only",
    ))
    if no_rewrite and "CREATE OR REPLACE" in after_u and "ROW_NUMBER" in after_u:
        return "contradicts RCA (table rewrite/dedupe not indicated)"
    return None


def _primary_fact_table(sql: str, rca: Optional[Dict[str, Any]] = None) -> str:
    refs = sorted(_extract_sql_object_refs(sql), key=len, reverse=True)
    if refs:
        return refs[0]
    for t in ((rca or {}).get("code_analysis") or {}).get("tables_inspected") or []:
        if t:
            return str(t)
    for t in (rca or {}).get("affected_tables") or []:
        if isinstance(t, str) and t:
            return t
        if isinstance(t, dict) and t.get("table"):
            return str(t["table"])
    return ""


def _inject_qualify(sql: str, partition_cols: List[str], order_cols: Optional[List[str]] = None) -> str:
    """Append QUALIFY ROW_NUMBER()=1 to a SELECT / INSERT…SELECT body."""
    if not sql or not partition_cols or _looks_like_python(sql):
        return sql
    if not _looks_like_sql(sql):
        return sql
    if re.search(r"\bQUALIFY\b", sql, re.I):
        return sql
    part = ", ".join(partition_cols)
    order = ", ".join(order_cols or partition_cols[:1])
    clause = (
        f"\nQUALIFY ROW_NUMBER() OVER (\n"
        f"  PARTITION BY {part}\n"
        f"  ORDER BY {order}\n"
        f") = 1"
    )
    body = sql.rstrip()
    if body.endswith(";"):
        return body[:-1] + clause + ";"
    return body + clause + ";"


def _dedupe_table_sql(
    table: str,
    partition_cols: List[str],
    where: str = "",
    order_cols: Optional[List[str]] = None,
) -> Tuple[str, str]:
    """Concrete before/after for deduping a known fact table (no invented sources)."""
    part = ", ".join(partition_cols)
    order = ", ".join(order_cols or partition_cols[:1])
    where_sql = f"\nWHERE {where}" if where else ""
    before = (
        f"SELECT {part}, COUNT(*) AS dup_cnt\n"
        f"FROM {table}{where_sql}\n"
        f"GROUP BY {part}\n"
        f"HAVING COUNT(*) > 1;"
    )
    if where:
        # Preserve rows outside the failing filter; dedupe only the offending slice.
        after = (
            f"CREATE OR REPLACE TABLE {table} AS\n"
            f"SELECT * FROM {table}\n"
            f"WHERE NOT ({where})\n"
            f"UNION ALL\n"
            f"SELECT * EXCLUDE (_dedupe_rn)\n"
            f"FROM (\n"
            f"  SELECT\n"
            f"    t.*,\n"
            f"    ROW_NUMBER() OVER (\n"
            f"      PARTITION BY {part}\n"
            f"      ORDER BY {order}\n"
            f"    ) AS _dedupe_rn\n"
            f"  FROM {table} t\n"
            f"  WHERE {where}\n"
            f")\n"
            f"WHERE _dedupe_rn = 1;"
        )
    else:
        after = (
            f"CREATE OR REPLACE TABLE {table} AS\n"
            f"SELECT * EXCLUDE (_dedupe_rn)\n"
            f"FROM (\n"
            f"  SELECT\n"
            f"    t.*,\n"
            f"    ROW_NUMBER() OVER (\n"
            f"      PARTITION BY {part}\n"
            f"      ORDER BY {order}\n"
            f"    ) AS _dedupe_rn\n"
            f"  FROM {table} t\n"
            f")\n"
            f"WHERE _dedupe_rn = 1;"
        )
    return before, after


def _is_vague_after(after: Optional[str], before: Optional[str] = None) -> bool:
    if not after or not str(after).strip():
        return True
    a = str(after).lower()
    if "todo: apply category-specific fix" in a:
        return True
    if "insufficient mapping" in a and before and str(after).strip() == str(before).strip():
        return True
    if a.strip() in ("pass", "n/a", "tbd", "fix me"):
        return True
    if len(str(after).strip()) < 8:
        return True
    # After must change the SQL — identical before/after is not a fix.
    if before:
        norm = lambda s: re.sub(r"\s+", " ", str(s).strip().rstrip(";").lower())
        if norm(after) == norm(before):
            return True
        # Comment-only remediation bolted onto the same SQL is not a fix.
        after_sql = re.sub(r"--.*?$", "", str(after), flags=re.M).strip()
        if norm(after_sql) == norm(before) and "--" in str(after):
            return True
    return False


def _reject_llm_polish(
    llm_fix: Dict[str, Any],
    artifact: Dict[str, str],
    allowed: Set[str],
    seed: Optional[Dict[str, Any]] = None,
    rca: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Return a reason to discard LLM before/after, or None if acceptable."""
    before = str(llm_fix.get("before") or "")
    after = str(llm_fix.get("after") or "")
    seed_before = str((seed or {}).get("before") or artifact.get("body") or "")

    invented_after = _invented_objects(after, allowed)
    invented_before = _invented_objects(before, allowed)
    invented = sorted(set(invented_after) | set(invented_before))
    if invented:
        return f"invented objects not in lineage/artifact: {', '.join(invented[:8])}"

    if _looks_like_python(before) or _looks_like_python(after):
        return "LLM returned Snowpark/Python — only SQL before/after queries are allowed"

    if _is_unsafe_after_sql(after):
        return "LLM/remediation After uses unsafe SQL (DELETE/ROWID/STAGING) — rejected"

    # Hard-block classic hallucinated staging sources.
    for blob in (before, after):
        if re.search(r"\bSTAGING\s*\.\s*\w+", blob, re.I):
            seed_has = bool(re.search(r"\bSTAGING\s*\.\s*\w+", seed_before, re.I))
            if not seed_has:
                return "invented STAGING.* object not in lineage/artifact"

    # If we only have DQ-rule SQL (no procedure), refuse brand-new INSERT/MERGE ETL
    # bodies that rewrite the seed into a fictional pipeline.
    source_kind = artifact.get("source_kind") or ""
    if source_kind in ("dq_rule", "unavailable") and after:
        after_u = after.upper()
        seed_u = seed_before.upper()
        invents_etl = (
            ("INSERT INTO" in after_u or "MERGE INTO" in after_u)
            and "INSERT INTO" not in seed_u
            and "MERGE INTO" not in seed_u
        )
        if invents_etl:
            return "LLM invented INSERT/MERGE ETL not present in source artifact"

    if _is_unavailable_artifact(artifact.get("body")) and (
        "INSERT INTO" in after.upper() or "MERGE INTO" in after.upper()
    ):
        return "source SQL unavailable — refusing invented ETL"

    contra = _contradicts_rca(llm_fix, rca, seed)
    if contra:
        return contra

    return None


def _seed_dq_fix(artifact: Dict[str, str], rca: Dict[str, Any],
                 ev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """RCA-guided DQ seed — always try to produce concrete SQL After when possible."""
    before = artifact.get("body") or ""
    obj = artifact.get("object_name") or "dq_check"
    facts = [str(f) for f in (ev.get("facts") or [])[:6]]
    fact_line = "; ".join(facts[:3]) if facts else (ev.get("summary") or rca.get("summary") or "")

    if _is_unavailable_artifact(before):
        guidance = _rca_guidance(rca)
        ensured_before, ensured_after = _ensure_dq_sql_after(artifact, rca, ev, before, "")
        return {
            "title": f"Source SQL unavailable for {obj}",
            "rationale": (
                "RCA evidence exists but no live procedure/rule SQL was resolved from lineage. "
                "Refusing to invent staging/ETL objects. "
                + (f"RCA immediate: {guidance['immediate'][:300]} " if guidance.get("immediate") else "")
                + f"Evidence: {fact_line[:300]}"
            ),
            "risk": "high",
            "rollback": "N/A — no automated change proposed." if not ensured_after else (
                "Restore prior table definition / re-run DQ check if findings change."
            ),
            "before": ensured_before[:4000] if ensured_before else _strip_narrative_from_sql(before[:4000]),
            "after": ensured_after,
            "language": "sql",
            "validation_hints": [
                f"Fetch procedure DDL for tables cited in RCA, then re-suggest fix for {obj}",
            ],
            "grounding": "evidence_seeded" if ensured_after else "rca_guided",
            "evidence_summary": fact_line[:500],
            "rca_immediate": guidance.get("immediate") or "",
            "rca_permanent": guidance.get("permanent") or "",
        }

    seed = _seed_from_rca(artifact, rca, ev)
    prior_after = seed.get("after") or ""
    ensured_before, ensured_after = _ensure_dq_sql_after(
        artifact, rca, ev, seed.get("before") or before, prior_after,
    )
    seed["before"] = ensured_before
    seed["after"] = ensured_after
    if ensured_after and not _is_vague_after(ensured_after, ensured_before):
        # Concrete SQL from evidence/remediation — prefer evidence_seeded label.
        if prior_after != ensured_after or _is_duplicate_failure(_dq_err_blob(rca, ev)):
            seed["grounding"] = "evidence_seeded"
        if "CREATE OR REPLACE" in ensured_after.upper():
            seed["rollback"] = (
                "Restore prior definition of the affected table from backup/time-travel; "
                "re-run the DQ check."
            )
    return seed



def _seed_task_fix(artifact: Dict[str, str], rca: Dict[str, Any],
                   error: str) -> Optional[Dict[str, Any]]:
    """RCA-guided task seed — no hardcoded CAST/NULLIF/warehouse templates."""
    before = artifact.get("body") or ""
    if _is_unavailable_artifact(before):
        return None
    ev = _evidence_pack(rca)
    if error and not ev.get("summary"):
        ev = {**ev, "summary": error[:500]}
    rca_use = dict(rca or {})
    g = _rca_guidance(rca_use)
    if not g.get("immediate") and not g.get("explanation") and error:
        rca_use = {
            **rca_use,
            "summary": rca_use.get("summary") or error[:400],
            "remediation": rca_use.get("remediation") or {
                "immediate_fix": (
                    f"Address task failure using RCA/error context: {error[:300]}"
                ),
            },
        }
    return _seed_from_rca(artifact, rca_use, ev)


def _remediate_fallback(category: str, artifact: Dict[str, str]) -> Dict[str, Any]:
    """Last-resort only — no category SQL templates; require RCA for a real fix."""
    before = _strip_narrative_from_sql(artifact["body"])
    lang = artifact["language"]
    obj = artifact["object_name"]

    if before and not _is_unavailable_artifact(before):
        return {
            "title": f"Review required for {obj}",
            "rationale": (
                f"No RCA remediation was available to ground a fix for '{category}'. "
                f"Re-run RCA, then Identify & Suggest Fix — refusing to invent SQL."
            ),
            "risk": "high",
            "rollback": "N/A — no automated change proposed.",
            "before": before[:4000],
            "after": "",
            "language": lang,
            "validation_hints": ["failure no longer reproduces after manual fix"],
            "grounding": "template_fallback",
        }

    return {
        "title": f"Proposed remediation for {obj}",
        "rationale": f"Category '{category}' — artifact SQL unavailable; cannot ground a fix.",
        "risk": "high",
        "rollback": "N/A",
        "before": before,
        "after": "",
        "language": lang,
        "validation_hints": ["failure no longer reproduces"],
        "grounding": "template_fallback",
    }


class FixAgent(BaseAgent):
    name = "fix"
    skill_file = "fix.md"

    def _resolve_rca(self, pipeline_id: str, incident_id: Optional[str],
                     rca_context: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Prefer Workbench rca_context; otherwise run RCA. Never drop context for incident_id."""
        if isinstance(rca_context, dict) and (
            rca_context.get("summary")
            or rca_context.get("category")
            or rca_context.get("diagnostic_results")
            or rca_context.get("code_analysis")
            or rca_context.get("evidence")
            or rca_context.get("root_cause")
        ):
            ctx = dict(rca_context)
            ctx.setdefault("pipeline_id", pipeline_id)
            if incident_id:
                ctx.setdefault("incident_id", incident_id)
            return ctx
        try:
            return RCAAgent().analyze(pipeline_id)
        except Exception:  # noqa: BLE001
            return None

    def _llm_suggest(self, artifact: Dict[str, str], category: str,
                     error: str, rca_result: Optional[Dict[str, Any]],
                     user_edit: Optional[str] = None,
                     seed: Optional[Dict[str, Any]] = None,
                     allowed_objects: Optional[Set[str]] = None,
                     ) -> Optional[Dict[str, Any]]:
        if not self.harness.available:
            return None
        ev = _evidence_pack(rca_result)
        task_sql = artifact.get("body") or ""
        if rca_result and not task_sql:
            task_sql = _extract_sql_body(
                (rca_result.get("code_analysis") or {}).get("task_sql")
            )[:4000]

        allowed = sorted(allowed_objects or _allowed_objects(artifact, rca_result))
        chain = [
            {
                "object": c.get("object_name"),
                "type": c.get("object_type"),
                "sql": (c.get("sql_body") or "")[:1200],
                "inputs": (c.get("inputs") or [])[:5],
                "outputs": (c.get("outputs") or [])[:5],
            }
            for c in _procedure_chain(rca_result)[:3]
        ]

        system = (
            f"You are a senior data engineer. Produce a concrete fix that follows the RCA.\n\n"
            f"=== SKILL ===\n{self.skill}\n\n"
            f"CRITICAL RULES:\n"
            f"- Output raw JSON only — NO markdown code fences, NO ```json wrapper\n"
            f"- Start with {{ and end with }}\n"
            f"- Required keys: title, rationale, risk, rollback, before, after, language, "
            f"validation_hints, evidence_summary\n"
            f"- FOLLOW rca_remediation / root_cause / expected / actual — do NOT substitute a "
            f"generic hardcoded template (dedupe CREATE OR REPLACE, NULLIF, warehouse resize, "
            f"trend Investigate SELECT) unless the RCA itself calls for that action\n"
            f"- If RCA says market expansion / threshold / not data corruption → propose "
            f"investigation or process/threshold actions consistent with RCA; NEVER invent a "
            f"table rewrite\n"
            f"- If RCA says duplicates/grain breach → propose SQL on allowed_objects that "
            f"addresses that finding\n"
            f"- before = focused failing SQL query ONLY (SELECT/INSERT/WITH…) — never full "
            f"Snowpark/Python procedure dumps\n"
            f"- after = corrected or investigative SQL ONLY — pure SQL, no explanatory "
            f"comments, no RCA narrative, no -- Immediate/-- Permanent footers\n"
            f"- Put human explanation in rationale / validation_hints — NEVER inside after\n"
            f"- NEVER invent schemas/tables/procedures not listed in allowed_objects "
            f"(e.g. do NOT invent STAGING.RX_CLAIMS_RAW)\n"
            f"- If artifact_source_kind is unavailable or dq_rule and no procedure_chain SQL "
            f"is provided: do NOT fabricate INSERT/MERGE ETL; prefer investigative SELECT "
            f"grounded in evidence when RCA does not provide rewrite SQL\n"
            f"- Ground rationale in RCA + evidence_facts / named offenders\n"
            f"- If a seed_fix is provided, keep its RCA intent and object names\n"
            f"- validation_hints must be testable checks"
        )
        guidance = _rca_guidance(rca_result)
        payload: Dict[str, Any] = {
            "artifact_platform": artifact["platform"],
            "artifact_name": artifact["object_name"],
            "artifact_source_kind": artifact.get("source_kind") or "unknown",
            "category": category,
            "error_message": (error or "")[:500],
            "artifact_sql": task_sql[:4000],
            "allowed_objects": allowed[:40],
            "procedure_chain": chain or None,
            "user_edit": user_edit,
            "evidence_summary": ev.get("summary"),
            "evidence_facts": ev.get("facts"),
            "sample_rows": ev.get("sample_rows"),
            "rca_remediation": {
                "immediate_fix": guidance.get("immediate") or None,
                "permanent_fix": guidance.get("permanent") or None,
                "monitoring_recommendation": guidance.get("monitoring") or None,
            },
            "seed_fix": (
                {k: seed.get(k) for k in (
                    "title", "rationale", "before", "after", "validation_hints",
                    "grounding", "rca_immediate", "rca_permanent",
                ) if seed.get(k) is not None} if seed else None
            ),
        }
        if rca_result:
            payload["root_cause_summary"] = str(rca_result.get("summary") or "")[:400]
            payload["detailed_analysis"] = str(rca_result.get("detailed_analysis") or "")[:600]
            rc = rca_result.get("root_cause")
            if isinstance(rc, dict):
                payload["root_cause"] = {
                    k: rc.get(k) for k in (
                        "explanation", "technical_explanation", "expected", "actual"
                    ) if rc.get(k)
                }
            if guidance.get("expected"):
                payload.setdefault("root_cause", {})
                if isinstance(payload.get("root_cause"), dict):
                    payload["root_cause"].setdefault("expected", guidance["expected"])
                    payload["root_cause"].setdefault("actual", guidance["actual"])

        txt = self.harness.reason(system, json.dumps(payload, default=str), max_tokens=2500)
        if not txt:
            return None
        result = self._extract_json(txt)
        if result and (result.get("after") or result.get("rationale")):
            return result
        return None

    def suggest(self, pipeline_id: str, incident_id: Optional[str] = None,
                user_edit: Optional[str] = None,
                rca_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        idx = {p["id"]: p for p in MonitoringAgent().collect()}
        target = idx.get(pipeline_id, {})
        rca = self._resolve_rca(pipeline_id, incident_id, rca_context)

        root_id = (rca or {}).get("root_cause_node") or pipeline_id
        category = (rca or {}).get("category") or _classify(target.get("error"))
        artifact = (
            _artifact_from_rca(pipeline_id, rca, idx) if rca
            else _artifact_from_pipeline(root_id if root_id in idx else pipeline_id, idx)
        )

        signature = f"{artifact['platform']} {category}"
        prior = self.recall(signature)

        error = target.get("error") or ""
        if rca:
            for item in (rca.get("evidence") or []):
                if isinstance(item, str) and "Error" in item:
                    error = item
                    break
            if not error:
                error = str(
                    (rca.get("root_cause") or {}).get("explanation")
                    or rca.get("summary")
                    or ""
                )

        ev_pack = _evidence_pack(rca)
        has_evidence = bool(
            ev_pack.get("facts") or ev_pack.get("summary")
            or (artifact.get("body") and not _is_unavailable_artifact(artifact.get("body")))
        )

        seed: Optional[Dict[str, Any]] = None
        if rca and _is_dq(pipeline_id, rca):
            seed = _seed_dq_fix(artifact, rca, ev_pack)
        elif rca:
            seed = _seed_task_fix(artifact, rca, error)

        allowed = _allowed_objects(artifact, rca)
        llm_fix = self._llm_suggest(
            artifact, category, error, rca, user_edit, seed=seed, allowed_objects=allowed,
        )

        grounding = "template_fallback"
        proposal = seed or _remediate_fallback(category, artifact)
        if seed:
            grounding = seed.get("grounding") or "rca_guided"

        reject_reason = None
        if llm_fix:
            reject_reason = _reject_llm_polish(
                llm_fix, artifact, allowed, seed=seed, rca=rca,
            )

        if (
            llm_fix
            and not reject_reason
            and not _is_vague_after(
                llm_fix.get("after"), llm_fix.get("before") or artifact.get("body")
            )
        ):
            for k, v in llm_fix.items():
                if v is not None:
                    proposal[k] = v
            grounding = "llm_polished"
        elif seed:
            # Prefer RCA-guided seed; never fall back to narrative comment After.
            proposal = dict(seed)
            grounding = seed.get("grounding") or "rca_guided"
            if _is_vague_after(proposal.get("after"), proposal.get("before")):
                if rca and _is_dq(pipeline_id, rca):
                    ensured_before, ensured_after = _ensure_dq_sql_after(
                        artifact, rca, ev_pack,
                        str(proposal.get("before") or artifact.get("body") or ""),
                        proposal.get("after"),
                    )
                    proposal["before"] = ensured_before
                    proposal["after"] = ensured_after
                    if ensured_after:
                        grounding = "evidence_seeded"
                else:
                    sql_after = _rca_after_fallback(
                        seed, str(proposal.get("before") or artifact.get("body") or ""),
                    )
                    proposal["after"] = sql_after
        elif has_evidence and _is_vague_after(proposal.get("after"), proposal.get("before")):
            if rca and _is_dq(pipeline_id, rca):
                ensured_before, ensured_after = _ensure_dq_sql_after(
                    artifact, rca, ev_pack,
                    artifact.get("body") or "",
                    proposal.get("after"),
                )
                proposal = {
                    "title": f"Evidence-seeded fix for {artifact['object_name']}",
                    "rationale": (
                        "Derived SQL After from RCA evidence / grain keys when remediation "
                        "did not embed a full statement."
                    ),
                    "risk": "medium",
                    "rollback": (
                        "Restore prior table definition; re-run DQ check."
                        if ensured_after and "CREATE OR REPLACE" in ensured_after.upper()
                        else "N/A"
                    ),
                    "before": ensured_before or _strip_narrative_from_sql(artifact.get("body") or ""),
                    "after": ensured_after,
                    "language": artifact.get("language") or "sql",
                    "validation_hints": ["Manually verify offenders cleared after fix"],
                    "grounding": "evidence_seeded",
                    "evidence_summary": ev_pack.get("summary"),
                }
                grounding = "evidence_seeded"
            else:
                sql_after = _rca_after_fallback(
                    {
                        "rca_immediate": (_rca_guidance(rca).get("immediate") or ""),
                        "rca_permanent": (_rca_guidance(rca).get("permanent") or ""),
                    },
                    artifact.get("body") or "",
                )
                proposal = {
                    "title": f"Insufficient mapping for {artifact['object_name']}",
                    "rationale": (
                        "RCA evidence exists but no safe automated SQL edit could be derived "
                        "from RCA remediation. Refine with Modify fix or update the rule/task "
                        "manually from the RCA."
                    ),
                    "risk": "high",
                    "rollback": "N/A",
                    "before": _strip_narrative_from_sql(artifact.get("body") or ""),
                    "after": sql_after,
                    "language": artifact.get("language") or "sql",
                    "validation_hints": ["Manually verify offenders cleared after fix"],
                    "grounding": "rca_guided",
                    "evidence_summary": ev_pack.get("summary"),
                }
                grounding = "rca_guided"

        # Final contract: before/after are SQL only — never RCA comment dumps.
        proposal["before"] = _strip_narrative_from_sql(proposal.get("before"))
        proposal["after"] = _strip_narrative_from_sql(proposal.get("after"))
        if _is_vague_after(proposal.get("after"), proposal.get("before")):
            if rca and _is_dq(pipeline_id, rca):
                ensured_before, ensured_after = _ensure_dq_sql_after(
                    artifact, rca, ev_pack,
                    str(proposal.get("before") or artifact.get("body") or ""),
                    proposal.get("after"),
                )
                if ensured_before:
                    proposal["before"] = ensured_before
                if ensured_after:
                    proposal["after"] = _strip_narrative_from_sql(ensured_after)
                    if grounding == "llm_polished":
                        grounding = (seed or {}).get("grounding") or "evidence_seeded"
                    elif grounding in ("rca_guided", "template_fallback"):
                        grounding = "evidence_seeded"
            else:
                sql_after = _rca_after_fallback(
                    seed or {
                        "rca_immediate": (_rca_guidance(rca).get("immediate") or ""),
                        "rca_permanent": (_rca_guidance(rca).get("permanent") or ""),
                    },
                    str(proposal.get("before") or artifact.get("body") or ""),
                )
                if sql_after:
                    proposal["after"] = _strip_narrative_from_sql(sql_after)
                    if grounding == "llm_polished" and seed:
                        grounding = seed.get("grounding") or "rca_guided"

        fix = {
            "fix_id": f"FIX-{pipeline_id}",
            "incident_id": incident_id or ((rca or {}).get("incident_id")),
            "target": {
                "platform": artifact["platform"],
                "artifact": artifact["object_name"],
                "object_name": root_id,
            },
            "category": category,
            "reused_from_memory": bool(prior),
            "grounding": grounding,
            "evidence_summary": (
                proposal.get("evidence_summary")
                or ev_pack.get("summary")
                or (rca or {}).get("summary")
            ),
            "evidence_facts": ev_pack.get("facts") or [],
            "title": proposal.get("title"),
            "rationale": proposal.get("rationale"),
            "risk": proposal.get("risk", "medium"),
            "rollback": proposal.get("rollback"),
            "before": proposal.get("before"),
            "after": proposal.get("after"),
            "language": proposal.get("language") or artifact.get("language"),
            "validation_hints": proposal.get("validation_hints") or [],
        }

        if user_edit and grounding == "template_fallback" and not llm_fix:
            fix = self._apply_user_edit(fix, user_edit)

        self.learn({
            "signature": signature,
            "fix_id": fix["fix_id"],
            "title": fix.get("title"),
            "user_edit": user_edit,
            "grounding": grounding,
        })
        return fix

    def _apply_user_edit(self, fix: Dict[str, Any], edit: str) -> Dict[str, Any]:
        note = f"\n-- user edit applied: {edit} --\n"
        e = edit.lower()
        after = fix["after"]
        if "medium" in e and "large" in fix["after"].lower():
            after = after.replace("LARGE", "MEDIUM").replace("Large", "Medium")
        if "coalesce to 0" in e:
            after = after.replace(
                "COALESCE(TRY_TO_NUMBER(amount), -1)",
                "COALESCE(TRY_TO_NUMBER(amount), 0)",
            )
        fix["after"] = after + note
        fix["title"] = f"{fix.get('title', 'Fix')} (user-modified)"
        fix["user_edit"] = edit
        return fix
