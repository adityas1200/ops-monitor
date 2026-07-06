from __future__ import annotations
from typing import Any, Dict, List, Optional, Set, Tuple

from app.agents.base import BaseAgent
from app.agents.monitoring_agent import MonitoringAgent
from app.connectors.dq_connector import DQConnector
from app.connectors.lineage_service import LineageService, index_runs_by_task_key

# ── Skill failure categories (matches rca.md exactly) ─────────────────────────
CATEGORY_CODE        = "Code Failure"
CATEGORY_DQ          = "Data Quality Failure"
CATEGORY_DEPENDENCY  = "Dependency Failure"
CATEGORY_INFRA       = "Infrastructure Failure"
CATEGORY_AVAIL       = "Data Availability Failure"
CATEGORY_UNKNOWN     = "Unknown Failure"


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
    ) -> Dict[str, Any]:
        """Derive Remediation section from category and context."""
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

    def _build_rca_report(
        self,
        task_name: str,
        execution_time: str,
        failure_type: str,
        summary: str,
        detailed_analysis: str,
        evidence: List[str],
        upstream_text: str,
        downstream_text: str,
        impact: Dict[str, Any],
        remediation: Dict[str, Any],
        confidence_level: str,
    ) -> str:
        """Generate the structured RCA report text matching the skill's Output Format."""
        ev_lines = "\n".join(f"  • {e}" for e in evidence) if evidence else "  • No additional evidence."
        imp_tables   = ", ".join(impact.get("impacted_tables", [])[:10]) or "None identified"
        imp_pipelines = ", ".join(impact.get("impacted_pipelines", [])[:10]) or "None identified"
        imp_reports  = ", ".join(impact.get("impacted_reports", [])) or "None identified"
        severity     = impact.get("business_severity", "Unknown")

        up_text   = upstream_text   or "(no upstream lineage resolved)"
        down_text = downstream_text or "(no downstream lineage resolved)"

        return (
            "================================================\n"
            "RCA REPORT\n"
            "================================================\n\n"
            f"Task Name:      {task_name}\n"
            f"Execution Time: {execution_time}\n"
            f"Failure Type:   {failure_type}\n\n"
            "----------------------------------------\n"
            "ROOT CAUSE\n"
            "----------------------------------------\n\n"
            f"Summary:\n  {summary}\n\n"
            f"Detailed Analysis:\n  {detailed_analysis}\n\n"
            f"Evidence:\n{ev_lines}\n\n"
            "----------------------------------------\n"
            "UPSTREAM LINEAGE\n"
            "----------------------------------------\n\n"
            f"{up_text}\n\n"
            "----------------------------------------\n"
            "DOWNSTREAM LINEAGE\n"
            "----------------------------------------\n\n"
            f"{down_text}\n\n"
            "----------------------------------------\n"
            "IMPACT ASSESSMENT\n"
            "----------------------------------------\n\n"
            f"Impacted Tables:    {imp_tables}\n"
            f"Impacted Pipelines: {imp_pipelines}\n"
            f"Impacted Reports:   {imp_reports}\n"
            f"Business Severity:  {severity}\n\n"
            "----------------------------------------\n"
            "REMEDIATION\n"
            "----------------------------------------\n\n"
            f"Immediate Fix:\n  {remediation.get('immediate_fix', '')}\n\n"
            f"Permanent Fix:\n  {remediation.get('permanent_fix', '')}\n\n"
            f"Monitoring Recommendation:\n  {remediation.get('monitoring_recommendation', '')}\n\n"
            "----------------------------------------\n"
            "CONFIDENCE SCORE\n"
            "----------------------------------------\n\n"
            f"{confidence_level}\n\n"
            "================================================"
        )

    # ── Main analysis entry-points ─────────────────────────────────────────────

    def analyze(self, pipeline_id: str, extra_context: Optional[str] = None) -> Dict[str, Any]:
        if pipeline_id.startswith("dq_"):
            return self.analyze_dq(pipeline_id, extra_context)

        # ── Step 1: Identify failed object ────────────────────────────────────
        lineage = LineageService()
        pipelines = [lineage.enrich_pipeline(p) for p in MonitoringAgent().collect()]
        idx = {p["id"]: p for p in pipelines}
        target = idx.get(pipeline_id)
        if not target:
            return {"error": f"pipeline {pipeline_id} not found"}

        target = self._enrich_for_rca(lineage, target)
        idx[pipeline_id] = target
        run_by_key = index_runs_by_task_key(pipelines)
        graph = lineage.load_task_graph()

        target_key = target.get("task_key") or pipeline_id
        # ── Step 2: Classify failure ──────────────────────────────────────────
        root_key = self._walk_upstream_root(target, run_by_key, graph)
        root = run_by_key.get(root_key, target)
        if root.get("id") != target.get("id"):
            root = self._enrich_for_rca(lineage, root)
            run_by_key[root_key] = root

        impacted_keys = self._downstream_impact(root_key, graph)
        category = _classify(root.get("error") or target.get("error"))

        # ── Lineage graphs ────────────────────────────────────────────────────
        lineage_graph = self._build_lineage(target_key, root_key, impacted_keys, graph, run_by_key)
        dq_related = lineage.correlate_dq(target, lineage.failed_dq_checks())
        table_roles = self._table_roles_from_context(target, root, dq_related)
        affected_tables = self._build_affected_tables(target, root, dq_related, table_roles)
        lineage_table = lineage.build_lineage_table(
            target, root_key, impacted_keys, graph, run_by_key, dq_related, table_roles)

        relevant_keys = self._relevant_task_keys(target_key, root_key, impacted_keys, graph)
        table_lineage = lineage.build_table_lineage_graph(
            relevant_keys, root_key, impacted_keys, graph, run_by_key)

        all_resolved_tables = [t["table"] for t in affected_tables]
        downstream_map = lineage.discover_all_downstream(
            all_resolved_tables,
            context_database=target.get("database", ""),
            context_schema=target.get("schema", ""),
        )
        table_lineage = lineage.enrich_table_lineage_with_downstream(table_lineage, downstream_map)

        # --- Two-section lineage graphs ---------------------------------------
        upstream_lineage = lineage.build_upstream_lineage_graph(
            target_key, root_key, graph, run_by_key)
        downstream_lineage = lineage.build_downstream_lineage_graph(
            target_key, root_key, impacted_keys, graph, run_by_key,
            downstream_map=downstream_map)

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
        if target.get("status"):
            evidence.append(f"Task State: {target['status']}")
        if root.get("error"):
            evidence.append(f"Error Message: {root['error']}")
        if root.get("query_id"):
            evidence.append(f"Root Query ID: {root['query_id']}")
        if target.get("started_at"):
            evidence.append(f"Start Time: {target['started_at']}")
        if target.get("ended_at"):
            evidence.append(f"End Time: {target['ended_at']}")
        if target.get("tables"):
            evidence.append(f"Tables in failed task: {', '.join(target['tables'][:8])}")
        if dq_related:
            evidence.append(
                f"{len(dq_related)} related DQ failure(s): "
                + ", ".join(c.get("name", "") for c in dq_related[:5]))
        if root.get("log_ref"):
            from app.connectors.aws_connector import AWSConnector
            from app.connectors.snowflake_connector import SnowflakeConnector
            getter = SnowflakeConnector() if root.get("platform") == "snowflake" else AWSConnector()
            evidence += getter.get_logs(root["log_ref"])[:6]

        # ── Impact assessment + remediation (skill sections) ─────────────────
        impact_assessment = self._build_impact_assessment(
            root_key, impacted_keys, affected_tables, downstream_consumers, run_by_key, graph)
        remediation = self._build_remediation(category, target, root, extra_context)
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

        # ── Claude harness (uses the full rca.md skill) ───────────────────────
        llm = self.think({
            # Metadata First inputs as per skill Principle 1
            "task_name": target.get("name"),
            "task_state": target.get("status"),
            "error_message": root.get("error") or target.get("error"),
            "start_time": target.get("started_at"),
            "end_time": target.get("ended_at"),
            "warehouse": target.get("warehouse"),
            "parent_task": graph.get(target_key, {}).get("upstream", [])[:3],
            "dependency_tasks": [run_by_key.get(k, {}).get("name", k) for k in impacted_keys[:5]],
            "tables": target.get("tables", [])[:10],
            # RCA context
            "root_cause_task": root.get("name"),
            "category": category,
            "affected_tables": [t["table"] for t in affected_tables[:10]],
            "impacted_downstream_count": len(impacted_keys),
            "prior_cases": prior[:3],
            "extra_context": extra_context,
        }, max_tokens=2000)

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

        # ── Text lineage diagrams (skill format) ─────────────────────────────
        upstream_text = (
            llm.get("upstream_lineage_text") if llm else None
        ) or _lineage_text(upstream_lineage.get("nodes", []), upstream_lineage.get("edges", []))
        downstream_text = (
            llm.get("downstream_lineage_text") if llm else None
        ) or _lineage_text(downstream_lineage.get("nodes", []), downstream_lineage.get("edges", []))

        # ── Structured RCA report text ────────────────────────────────────────
        execution_time = f"{target.get('started_at', 'N/A')} – {target.get('ended_at', 'N/A')}"
        rca_report = self._build_rca_report(
            task_name=target.get("name", pipeline_id),
            execution_time=execution_time,
            failure_type=category,
            summary=summary,
            detailed_analysis=detailed_analysis,
            evidence=evidence,
            upstream_text=upstream_text,
            downstream_text=downstream_text,
            impact=impact_assessment,
            remediation=remediation,
            confidence_level=confidence_lvl,
        )

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
            "remediation": remediation,
            "rca_report": rca_report,
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
            "seen_before": bool(prior),
        }
        self.learn({"signature": signature, "pipeline_id": pipeline_id,
                    "category": category, "root_cause_node": root_key})
        return result

    def analyze_dq(self, check_id: str, extra_context: Optional[str] = None) -> Dict[str, Any]:
        lineage = LineageService()
        all_checks = DQConnector().read_results()
        check = next((c for c in all_checks if c["id"] == check_id), None)
        if not check:
            return {"error": f"DQ check {check_id} not found"}

        check = {**check, "tables": lineage.resolve_dq_tables(check)}
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

        table_roles: Dict[str, str] = {}
        for t in check_tables:
            table_roles[t] = "dq_failed"
        for p in related_tasks:
            for t in p.get("tables") or []:
                if t in check_tables:
                    table_roles[t] = "shared_task_dq"
                elif t not in table_roles:
                    table_roles[t] = "failed_task"

        dq_related = [check] if check.get("status") in ("FAILED", "DELAYED") else []
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

        relevant_keys = {root_key_task, *impacted_keys} if root_task else set()
        for p in related_tasks:
            relevant_keys.add(p.get("task_key") or p["id"])
        table_lineage = lineage.build_table_lineage_graph(
            relevant_keys, root_key_task,
            impacted_keys, graph, run_by_key) if relevant_keys else {"nodes": [], "edges": []}

        all_resolved_tables = [t["table"] for t in affected_tables]
        from app.core.config import get_dq_monitoring_config
        dq_cfg = get_dq_monitoring_config()
        dq_fqn_parts = dq_cfg["table_fqn"].split(".")
        ctx_db = (root_task or {}).get("database", "") or (dq_fqn_parts[0] if len(dq_fqn_parts) >= 3 else "")
        ctx_schema = (root_task or {}).get("schema", "") or (dq_fqn_parts[1] if len(dq_fqn_parts) >= 3 else "")
        downstream_map = lineage.discover_all_downstream(
            all_resolved_tables, context_database=ctx_db, context_schema=ctx_schema)
        table_lineage = lineage.enrich_table_lineage_with_downstream(table_lineage, downstream_map)

        dq_upstream_lineage = lineage.build_upstream_lineage_graph(
            root_key_task, root_key_task, graph, run_by_key) if root_task else {"nodes": [], "edges": []}
        dq_downstream_lineage = lineage.build_downstream_lineage_graph(
            root_key_task, root_key_task, impacted_keys, graph, run_by_key,
            downstream_map=downstream_map) if root_task else {"nodes": [], "edges": []}

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
        if check.get("error"):
            evidence.append(f"Error Message: {check['error']}")
        if check_tables:
            evidence.append(f"Tables from DQ: {', '.join(sorted(check_tables)[:8])}")
        if related_tasks:
            evidence.append(
                f"{len(related_tasks)} related failed task(s) share table(s): "
                + ", ".join(p.get("name", "") for p in related_tasks[:4]))

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
        confidence_lvl = _confidence_level(confidence)

        impact_assessment = self._build_impact_assessment(
            root_key, impacted_keys, affected_tables, downstream_consumers, run_by_key, graph)
        remediation = self._build_remediation(category, check, root_task or check)

        upstream_text = _lineage_text(dq_upstream_lineage.get("nodes", []), dq_upstream_lineage.get("edges", []))
        downstream_text = _lineage_text(dq_downstream_lineage.get("nodes", []), dq_downstream_lineage.get("edges", []))
        execution_time = check.get("run_at", "N/A")
        rca_report = self._build_rca_report(
            task_name=f"DQ: {check.get('name', check_id)}",
            execution_time=execution_time,
            failure_type=category,
            summary=summary,
            detailed_analysis=detailed_analysis,
            evidence=evidence,
            upstream_text=upstream_text,
            downstream_text=downstream_text,
            impact=impact_assessment,
            remediation=remediation,
            confidence_level=confidence_lvl,
        )

        incident = self.incident_log.log({
            "pipeline_id": check_id, "name": check.get("name"), "platform": "dq",
            "status": check.get("status"), "signature": f"dq {category}", "source": "rca",
            "root_cause_node": root_key, "category": category, "rca_summary": summary,
        })

        return {
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
            "recommended_next": "fix" if root_task else "investigate",
            "impact_assessment": impact_assessment,
            "remediation": remediation,
            "rca_report": rca_report,
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
            "seen_before": False,
        }

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
