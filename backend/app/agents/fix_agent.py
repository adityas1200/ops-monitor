from __future__ import annotations
import json
import re
from typing import Any, Dict, Optional

from app.agents.base import BaseAgent
from app.agents.monitoring_agent import MonitoringAgent
from app.agents.rca_agent import CATEGORY_DQ, RCAAgent, _classify


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


def _artifact_from_pipeline(pipeline_id: str, idx: Dict[str, Any]) -> Dict[str, str]:
    p = idx.get(pipeline_id, {})
    platform = p.get("platform", "unknown")
    lang = "sql" if platform in ("snowflake", "dq") else ("json" if platform == "dynamodb" else "python")
    err = p.get("error") or ""
    name = pipeline_id.split("_", 1)[-1] if "_" in pipeline_id else pipeline_id
    body = err if err else f"-- Source artifact for {name} ({platform}) — fetch from live catalog --"
    return {"platform": platform, "object_name": name, "language": lang, "body": body}


def _artifact_from_rca(pipeline_id: str, rca: Dict[str, Any],
                       idx: Dict[str, Any]) -> Dict[str, str]:
    """Prefer real SQL from RCA over error-string artifacts."""
    code = rca.get("code_analysis") or {}
    task_sql = _extract_sql_body(code.get("task_sql"))
    root_id = rca.get("root_cause_node") or pipeline_id
    name = rca.get("root_cause_name") or (
        pipeline_id.split("_", 1)[-1] if "_" in pipeline_id else pipeline_id
    )

    if _is_dq(pipeline_id, rca):
        body = task_sql or (rca.get("diagnostic_results") or {}).get("diagnostic_sql") or ""
        if not body:
            base = _artifact_from_pipeline(pipeline_id, idx)
            body = base["body"]
        return {
            "platform": "snowflake",
            "object_name": str(name),
            "language": "sql",
            "body": body[:8000],
        }

    if task_sql:
        platform = (idx.get(root_id) or idx.get(pipeline_id) or {}).get("platform", "snowflake")
        lang = "sql" if platform == "snowflake" else ("json" if platform == "dynamodb" else "python")
        return {
            "platform": platform,
            "object_name": str(name),
            "language": lang,
            "body": task_sql[:8000],
        }

    return _artifact_from_pipeline(root_id if root_id in idx else pipeline_id, idx)


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
    return False


def _seed_dq_fix(artifact: Dict[str, str], rca: Dict[str, Any],
                 ev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Deterministic DQ seeds from evidence + rule SQL."""
    before = artifact.get("body") or ""
    obj = artifact.get("object_name") or "dq_check"
    err_blob = " ".join([
        str(rca.get("summary") or ""),
        str((rca.get("root_cause") or {}).get("explanation") or ""),
        str(ev.get("summary") or ""),
        str(ev.get("execution_error") or ""),
        " ".join(str(f) for f in (ev.get("facts") or [])[:6]),
        before[:1500],
    ]).lower()
    facts = [str(f) for f in (ev.get("facts") or [])[:6]]
    fact_line = "; ".join(facts[:3]) if facts else (ev.get("summary") or rca.get("summary") or "")

    div_err = any(k in err_blob for k in (
        "division by zero", "divide by zero", "div0", "division_by_zero",
    ))
    div_pat = re.search(
        r"((?:[A-Za-z_][\w\.]*|\)|\d+)\s*/\s*)([A-Za-z_][\w\.]*)",
        before,
    )
    if div_err or (div_pat and any(k in err_blob for k in ("zero", "null", "nan"))):
        after = before
        if div_pat:
            divisor = div_pat.group(2)
            if f"NULLIF({divisor}" not in before and f"nullif({divisor.lower()}" not in before.lower():
                after = before.replace(
                    div_pat.group(0),
                    f"{div_pat.group(1)}NULLIF({divisor}, 0)",
                    1,
                )
        if after == before and div_pat:
            divisor = div_pat.group(2)
            after = re.sub(
                rf"/\s*{re.escape(divisor)}\b",
                f"/ NULLIF({divisor}, 0)",
                before,
                count=1,
            )
        if after != before:
            return {
                "title": f"Guard division by zero in {obj}",
                "rationale": (
                    f"Check fails with division by zero. Wrap the divisor in NULLIF(..., 0). "
                    f"Evidence: {fact_line[:300]}"
                ),
                "risk": "low",
                "rollback": "Restore prior DQ rule SQL without NULLIF.",
                "before": before[:4000],
                "after": after[:4000],
                "language": "sql",
                "validation_hints": [
                    f"Re-run DQ check {obj} — no division by zero",
                    "Rule executes successfully on current data",
                ],
                "grounding": "evidence_seeded",
                "evidence_summary": fact_line[:500],
            }

    mismatch = any(k in err_blob for k in (
        "mismatch", "layer", "brand", "segment", "source_cnt", "target_cnt",
        "l2", "l3", "nbrx", "trx", "align",
    ))
    if mismatch and (facts or before):
        keys = facts[:4] if facts else [fact_line[:200]]
        keys_txt = "\n".join(f"--   • {k}" for k in keys)
        after = (
            f"{before.rstrip()}\n\n"
            f"-- Suggested remediation (evidence-grounded):\n"
            f"-- Align grain/filters so both sides use the same brand/segment/territory keys.\n"
            f"-- Offenders from RCA:\n"
            f"{keys_txt}\n"
            f"-- Verify with:\n"
            f"-- SELECT * FROM (<rule>) WHERE status ILIKE '%fail%' OR pass_fail = 'FAIL';\n"
        )
        return {
            "title": f"Align layer/key filters for {obj}",
            "rationale": (
                f"DQ evidence names concrete mismatched keys. Align join/filter grain on both "
                f"sides of the comparison. Evidence: {fact_line[:400]}"
            ),
            "risk": "medium",
            "rollback": "Restore prior DQ rule SQL / upstream filters.",
            "before": before[:4000] or "-- (rule SQL unavailable — see RCA diagnostic)",
            "after": after[:4000],
            "language": "sql",
            "validation_hints": [
                f"Re-run DQ check {obj} — fail count decreases for named keys",
                "SOURCE_CNT = TARGET_CNT (or equivalent) for cited brands/segments",
            ],
            "grounding": "evidence_seeded",
            "evidence_summary": fact_line[:500],
        }

    dup = any(k in err_blob for k in ("duplicate", "dup ", "grain", "row_number", "primary key"))
    if dup and before:
        if "qualify" not in before.lower() and "row_number()" not in before.lower():
            after = (
                f"{before.rstrip()}\n"
                f"-- Deduplicate to expected grain (adjust PARTITION BY from evidence):\n"
                f"-- QUALIFY ROW_NUMBER() OVER (PARTITION BY <grain_keys> ORDER BY <ts> DESC) = 1\n"
            )
            return {
                "title": f"Deduplicate to expected grain in {obj}",
                "rationale": (
                    f"Evidence suggests duplicate/grain breach. Add QUALIFY ROW_NUMBER() on the "
                    f"business grain. Evidence: {fact_line[:300]}"
                ),
                "risk": "medium",
                "rollback": "Remove QUALIFY / restore prior SQL.",
                "before": before[:4000],
                "after": after[:4000],
                "language": "sql",
                "validation_hints": [
                    f"Re-run DQ check {obj} — duplicate count = 0",
                    "Row count at grain matches expected unique keys",
                ],
                "grounding": "evidence_seeded",
                "evidence_summary": fact_line[:500],
            }

    if before.strip() and (facts or ev.get("summary")):
        after = (
            f"{before.rstrip()}\n\n"
            f"-- Evidence-backed remediation notes (review before applying):\n"
            f"-- {fact_line[:500]}\n"
            f"-- Prefer fixing upstream data or tightening the comparison predicates\n"
            f"-- so the cited offenders no longer fail this check.\n"
        )
        return {
            "title": f"Evidence-grounded remediation for {obj}",
            "rationale": (
                f"RCA surfaced concrete offenders; apply a targeted data/rule change. "
                f"Evidence: {fact_line[:400]}"
            ),
            "risk": "medium",
            "rollback": "Restore prior rule / upstream transform.",
            "before": before[:4000],
            "after": after[:4000],
            "language": "sql",
            "validation_hints": [
                f"Re-run DQ check {obj} and confirm fail_count drops",
                "Named offenders from RCA no longer appear in failing set",
            ],
            "grounding": "evidence_seeded",
            "evidence_summary": fact_line[:500],
        }

    return None


def _seed_task_fix(artifact: Dict[str, str], rca: Dict[str, Any],
                   error: str) -> Optional[Dict[str, Any]]:
    """Deterministic task seeds from real SQL + error."""
    before = artifact.get("body") or ""
    if not before.strip() or before.strip().startswith("-- Source artifact"):
        return None
    obj = artifact.get("object_name") or "task"
    blob = f"{error} {rca.get('summary') or ''} {before[:1000]}".lower()

    if any(k in blob for k in ("numeric value", "cast", "is not recognized", "invalid number", "nan")):
        cast_m = re.search(
            r"CAST\s*\(\s*([A-Za-z_][\w\.]*)\s+AS\s+(NUMBER[^)]*|DECIMAL[^)]*|FLOAT|DOUBLE)\s*\)",
            before,
            re.I,
        )
        if cast_m:
            col, typ = cast_m.group(1), cast_m.group(2)
            old = cast_m.group(0)
            new = f"CAST(COALESCE(TRY_TO_NUMBER({col}), 0) AS {typ})"
            after = before.replace(old, new, 1)
            if after != before:
                return {
                    "title": f"Safe numeric cast for {col} in {obj}",
                    "rationale": (
                        f"Hard CAST on {col} fails on non-numeric/NULL values. "
                        f"TRY_TO_NUMBER + COALESCE makes the load resilient."
                    ),
                    "risk": "low",
                    "rollback": f"Restore hard CAST on {col}.",
                    "before": before[:4000],
                    "after": after[:4000],
                    "language": artifact.get("language") or "sql",
                    "validation_hints": [
                        f"Task {obj} succeeds",
                        f"No cast errors on {col}",
                    ],
                    "grounding": "evidence_seeded",
                }

    if "division by zero" in blob or "divide by zero" in blob:
        div_pat = re.search(
            r"((?:[A-Za-z_][\w\.]*|\)|\d+)\s*/\s*)([A-Za-z_][\w\.]*)",
            before,
        )
        if div_pat:
            divisor = div_pat.group(2)
            after = before.replace(
                div_pat.group(0),
                f"{div_pat.group(1)}NULLIF({divisor}, 0)",
                1,
            )
            if after != before:
                return {
                    "title": f"NULLIF divisor in {obj}",
                    "rationale": f"Task SQL divides by {divisor} which can be zero.",
                    "risk": "low",
                    "rollback": "Remove NULLIF wrapper.",
                    "before": before[:4000],
                    "after": after[:4000],
                    "language": "sql",
                    "validation_hints": [f"Task {obj} succeeds without division by zero"],
                    "grounding": "evidence_seeded",
                }

    cat = str(rca.get("category") or "").lower()
    if any(k in blob for k in ("timeout", "warehouse", "memory", "oom")) or "infra" in cat:
        after = (
            f"{before.rstrip()}\n\n"
            f"-- Infra remediation suggestion:\n"
            f"-- ALTER TASK ... SET WAREHOUSE = <larger_or_right_sized_wh>;\n"
            f"-- or raise STATEMENT_TIMEOUT_IN_SECONDS / optimize the heavy scan/join above.\n"
        )
        return {
            "title": f"Infra/timeout remediation for {obj}",
            "rationale": (
                f"Failure signature suggests resource/timeout pressure. "
                f"Right-size warehouse or optimize the highlighted SQL. RCA: "
                f"{(rca.get('summary') or error)[:300]}"
            ),
            "risk": "medium",
            "rollback": "Restore prior warehouse / timeout settings.",
            "before": before[:4000],
            "after": after[:4000],
            "language": artifact.get("language") or "sql",
            "validation_hints": [
                f"Task {obj} completes within SLA",
                "No timeout / OOM in task history",
            ],
            "grounding": "evidence_seeded",
            "evidence_summary": (rca.get("summary") or error)[:500],
        }

    return None


def _remediate_fallback(category: str, artifact: Dict[str, str]) -> Dict[str, Any]:
    """Last-resort only — prefer honest incomplete over fake TODO SQL."""
    before = artifact["body"]
    lang = artifact["language"]
    obj = artifact["object_name"]
    cat = (category or "").lower()

    if ("infra" in cat or cat == "infra") and lang == "python":
        after = before.replace(
            "df.join(dim, 'txn_id')",
            "df.join(broadcast(dim), 'txn_id')  # broadcast small dim",
        )
        if after != before:
            after = "from pyspark.sql.functions import broadcast\n" + after
            return {
                "title": f"Fix OOM in {obj} via broadcast join",
                "rationale": "Fallback template: broadcast small dimension to avoid shuffle OOM.",
                "risk": "low",
                "rollback": "Revert script.",
                "before": before,
                "after": after,
                "language": lang,
                "validation_hints": ["job completes without OOM"],
                "grounding": "template_fallback",
            }

    if (("data" in cat or "code" in cat) and lang == "sql"
            and "CAST(amount AS NUMBER" in before):
        after = before.replace(
            "CAST(amount AS NUMBER(18,2)) AS amount",
            "CAST(COALESCE(TRY_TO_NUMBER(amount), 0) AS NUMBER(18,2)) AS amount",
        )
        if after != before:
            return {
                "title": f"Guard NaN/NULL cast in {obj}",
                "rationale": "Fallback template: TRY_TO_NUMBER + COALESCE.",
                "risk": "low",
                "rollback": "Restore prior task body.",
                "before": before,
                "after": after,
                "language": lang,
                "validation_hints": ["task succeeds"],
                "grounding": "template_fallback",
            }

    if before and not before.strip().startswith("-- Source artifact"):
        return {
            "title": f"Review required for {obj}",
            "rationale": (
                f"No deterministic seed matched category '{category}'. "
                f"Manual review of the artifact below is required — refusing to invent SQL."
            ),
            "risk": "high",
            "rollback": "N/A — no automated change proposed.",
            "before": before[:4000],
            "after": (
                f"{before.rstrip()}\n\n"
                f"-- INSUFFICIENT MAPPING: could not derive a safe automated fix.\n"
                f"-- Use RCA evidence and edit this proposal, or refine via Modify fix.\n"
            ),
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
        "after": before + "\n-- INSUFFICIENT CONTEXT: fetch task/rule SQL via RCA then re-suggest --\n",
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
                     seed: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        if not self.harness.available:
            return None
        ev = _evidence_pack(rca_result)
        task_sql = artifact.get("body") or ""
        if rca_result and not task_sql:
            task_sql = _extract_sql_body(
                (rca_result.get("code_analysis") or {}).get("task_sql")
            )[:4000]

        system = (
            f"You are a senior data engineer. Produce a concrete, evidence-grounded fix.\n\n"
            f"=== SKILL ===\n{self.skill}\n\n"
            f"CRITICAL RULES:\n"
            f"- Output raw JSON only — NO markdown code fences, NO ```json wrapper\n"
            f"- Start with {{ and end with }}\n"
            f"- Required keys: title, rationale, risk, rollback, before, after, language, "
            f"validation_hints, evidence_summary\n"
            f"- before = relevant failing SQL/code section from the artifact\n"
            f"- after = corrected version (NEVER empty or TODO-only)\n"
            f"- Ground rationale in evidence_facts / named offenders when present\n"
            f"- If a seed_fix is provided, polish it — do not discard its concrete SQL change\n"
            f"- Be SPECIFIC — exact tables, columns, SQL fragments\n"
            f"- validation_hints must be testable checks"
        )
        payload: Dict[str, Any] = {
            "artifact_platform": artifact["platform"],
            "artifact_name": artifact["object_name"],
            "category": category,
            "error_message": (error or "")[:500],
            "artifact_sql": task_sql[:4000],
            "user_edit": user_edit,
            "evidence_summary": ev.get("summary"),
            "evidence_facts": ev.get("facts"),
            "sample_rows": ev.get("sample_rows"),
            "seed_fix": (
                {k: seed.get(k) for k in (
                    "title", "rationale", "before", "after", "validation_hints", "grounding"
                )} if seed else None
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
            or (artifact.get("body") and not artifact["body"].startswith("-- Source artifact"))
        )

        seed: Optional[Dict[str, Any]] = None
        if rca and _is_dq(pipeline_id, rca):
            seed = _seed_dq_fix(artifact, rca, ev_pack)
        elif rca:
            seed = _seed_task_fix(artifact, rca, error)

        llm_fix = self._llm_suggest(artifact, category, error, rca, user_edit, seed=seed)

        grounding = "template_fallback"
        proposal = seed or _remediate_fallback(category, artifact)
        if seed:
            grounding = seed.get("grounding") or "evidence_seeded"

        if llm_fix and not _is_vague_after(
            llm_fix.get("after"), llm_fix.get("before") or artifact.get("body")
        ):
            for k, v in llm_fix.items():
                if v is not None:
                    proposal[k] = v
            grounding = "llm_polished"
        elif llm_fix and seed and not _is_vague_after(seed.get("after"), seed.get("before")):
            grounding = seed.get("grounding") or "evidence_seeded"
        elif has_evidence and _is_vague_after(proposal.get("after"), proposal.get("before")):
            if seed and not _is_vague_after(seed.get("after"), seed.get("before")):
                proposal = seed
                grounding = seed.get("grounding") or "evidence_seeded"
            else:
                proposal = {
                    "title": f"Insufficient mapping for {artifact['object_name']}",
                    "rationale": (
                        "RCA evidence exists but no safe automated SQL edit could be derived. "
                        "Refine with Modify fix or update the rule/task manually from the evidence."
                    ),
                    "risk": "high",
                    "rollback": "N/A",
                    "before": artifact.get("body") or "",
                    "after": (
                        f"{(artifact.get('body') or '').rstrip()}\n\n"
                        f"-- INSUFFICIENT MAPPING (guardrail)\n"
                        f"-- Evidence: {(ev_pack.get('summary') or '')[:400]}\n"
                    ),
                    "language": artifact.get("language") or "sql",
                    "validation_hints": ["Manually verify offenders cleared after fix"],
                    "grounding": "evidence_seeded",
                    "evidence_summary": ev_pack.get("summary"),
                }
                grounding = "evidence_seeded"

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
