from __future__ import annotations
import json
from typing import Any, Dict, Optional

from app.agents.base import BaseAgent
from app.agents.monitoring_agent import MonitoringAgent
from app.agents.rca_agent import RCAAgent, _classify


def _artifact_from_pipeline(pipeline_id: str, idx: Dict[str, Any]) -> Dict[str, str]:
    p = idx.get(pipeline_id, {})
    platform = p.get("platform", "unknown")
    lang = "sql" if platform == "snowflake" else ("json" if platform == "dynamodb" else "python")
    err = p.get("error") or ""
    name = pipeline_id.split("_", 1)[-1] if "_" in pipeline_id else pipeline_id
    body = err if err else f"-- Source artifact for {name} ({platform}) — fetch from live catalog --"
    return {"platform": platform, "object_name": name, "language": lang, "body": body}


# remediation templates per (category) producing before/after edits
def _remediate(category: str, artifact: Dict[str, str]) -> Dict[str, Any]:
    before = artifact["body"]
    lang = artifact["language"]
    obj = artifact["object_name"]

    if category == "infra" and lang == "python":
        after = before.replace(
            "# broad join on skewed key causes OOM\n    result = df.join(dim, 'txn_id')",
            "# salt the skewed key + broadcast small dim to avoid OOM\n"
            "    from pyspark.sql.functions import broadcast\n"
            "    result = df.join(broadcast(dim), 'txn_id')",
        )
        if after == before:  # fallback simple transform
            after = before.replace("df.join(dim, 'txn_id')",
                                   "df.join(broadcast(dim), 'txn_id')  # broadcast small dim")
            after = "from pyspark.sql.functions import broadcast\n" + after
        return {"title": f"Fix OOM in {obj} via broadcast join + repartition",
                "rationale": "Join on a skewed key materialized a huge shuffle causing executor OOM. "
                             "Broadcasting the small dimension and bumping DPU removes the shuffle.",
                "risk": "low", "rollback": "Revert script + restore previous DPU=10.",
                "before": before, "after": after, "language": lang,
                "validation_hints": ["job completes without OOM", "output row count ~ input row count",
                                     "no duplicate txn_id"]}

    if category == "data" and lang == "sql":
        after = before.replace(
            "CAST(amount AS NUMBER(18,2)) AS amount",
            "CAST(COALESCE(TRY_TO_NUMBER(amount), 0) AS NUMBER(18,2)) AS amount",
        )
        return {"title": f"Guard NaN/NULL cast in {obj}",
                "rationale": "Upstream produced 'NaN'/null amounts that fail a hard CAST. "
                             "TRY_TO_NUMBER + COALESCE makes the load resilient.",
                "risk": "low", "rollback": "Restore prior task body (hard CAST).",
                "before": before, "after": after, "language": lang,
                "validation_hints": ["task succeeds", "no null amounts in FACT_TRANSACTIONS",
                                     "row count matches CURATED.TRANSACTIONS"]}

    if category == "infra" and lang == "json":
        after = before.replace('"WriteCapacityUnits": 1000', '"WriteCapacityUnits": 2000') \
                      .replace('"BillingMode": "PROVISIONED"', '"BillingMode": "PAY_PER_REQUEST"')
        return {"title": f"Resolve throttling on {obj}",
                "rationale": "Write throughput exceeded provisioned WCU. Switch to on-demand "
                             "(or raise WCU) to absorb burst writes from feature_store_write.",
                "risk": "medium", "rollback": "Revert to PROVISIONED WCU=1000.",
                "before": before, "after": after, "language": lang,
                "validation_hints": ["no ProvisionedThroughputExceededException",
                                     "write latency p99 < 50ms"]}

    # generic
    return {"title": f"Proposed remediation for {obj}",
            "rationale": f"Category '{category}' remediation pattern applied.",
            "risk": "medium", "rollback": "Revert to prior artifact.",
            "before": before, "after": before + "\n-- TODO: apply category-specific fix --\n",
            "language": lang, "validation_hints": ["failure no longer reproduces"]}


class FixAgent(BaseAgent):
    name = "fix"
    skill_file = "fix.md"

    def _llm_suggest(self, artifact: Dict[str, str], category: str,
                     error: str, rca_result: Optional[Dict[str, Any]],
                     user_edit: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Use LLM to generate a specific fix based on actual code and error."""
        if not self.harness.available:
            return None
        task_sql = ""
        if rca_result and rca_result.get("code_analysis", {}).get("task_sql"):
            task_sql = rca_result["code_analysis"]["task_sql"][:2000]

        system = (
            f"You are a senior data engineer. Given a failing task/procedure, its error, and "
            f"optionally the source SQL, produce a concrete fix.\n\n"
            f"=== SKILL ===\n{self.skill}\n\n"
            f"CRITICAL RULES:\n"
            f"- Output raw JSON only — NO markdown code fences, NO ```json wrapper\n"
            f"- Start with {{ and end with }}\n"
            f"- Required keys: title, rationale, risk, rollback, before, after, language, validation_hints\n"
            f"- The 'before' field: the relevant failing code section\n"
            f"- The 'after' field: the corrected version\n"
            f"- Be SPECIFIC — reference exact table names, column names, SQL fragments"
        )
        payload = {
            "artifact_platform": artifact["platform"],
            "artifact_name": artifact["object_name"],
            "category": category,
            "error_message": error[:500],
            "task_sql": task_sql,
            "user_edit": user_edit,
        }
        if rca_result:
            payload["root_cause_summary"] = rca_result.get("summary", "")[:300]
            payload["detailed_analysis"] = rca_result.get("detailed_analysis", "")[:500]

        txt = self.harness.reason(system, json.dumps(payload, default=str), max_tokens=2500)
        if not txt:
            return None
        result = self._extract_json(txt)
        if result and (result.get("after") or result.get("rationale")):
            return result
        return None

    def suggest(self, pipeline_id: str, incident_id: Optional[str] = None,
                user_edit: Optional[str] = None) -> Dict[str, Any]:
        idx = {p["id"]: p for p in MonitoringAgent().collect()}
        target = idx.get(pipeline_id, {})
        rca = RCAAgent().analyze(pipeline_id) if not incident_id else None
        root_id = rca["root_cause_node"] if rca else pipeline_id
        category = rca["category"] if rca else _classify(target.get("error"))
        artifact = _artifact_from_pipeline(root_id, idx)

        signature = f"{artifact['platform']} {category}"
        prior = self.recall(signature)

        error = target.get("error") or ""
        if rca and rca.get("evidence"):
            for ev in rca["evidence"]:
                if "Error" in ev:
                    error = ev
                    break

        # LLM-first: try to generate a specific fix from real context
        llm_fix = self._llm_suggest(artifact, category, error, rca, user_edit)

        # Template fallback
        proposal = _remediate(category, artifact)

        fix = {
            "fix_id": f"FIX-{pipeline_id}",
            "incident_id": incident_id or (rca["incident_id"] if rca else None),
            "target": {"platform": artifact["platform"], "artifact": artifact["object_name"],
                       "object_name": root_id},
            "category": category,
            "reused_from_memory": bool(prior),
            **proposal,
        }

        # Override with LLM results if available
        if llm_fix:
            if llm_fix.get("title"):
                fix["title"] = llm_fix["title"]
            if llm_fix.get("rationale"):
                fix["rationale"] = llm_fix["rationale"]
            if llm_fix.get("before"):
                fix["before"] = llm_fix["before"]
            if llm_fix.get("after"):
                fix["after"] = llm_fix["after"]
            if llm_fix.get("risk"):
                fix["risk"] = llm_fix["risk"]
            if llm_fix.get("rollback"):
                fix["rollback"] = llm_fix["rollback"]
            if llm_fix.get("validation_hints"):
                fix["validation_hints"] = llm_fix["validation_hints"]

        if user_edit and not llm_fix:
            fix = self._apply_user_edit(fix, user_edit)

        self.learn({"signature": signature, "fix_id": fix["fix_id"],
                    "title": fix["title"], "user_edit": user_edit})
        return fix

    def _apply_user_edit(self, fix: Dict[str, Any], edit: str) -> Dict[str, Any]:
        """Interactive fix editing: apply simple directives to the after-snippet."""
        note = f"\n-- user edit applied: {edit} --\n"
        e = edit.lower()
        after = fix["after"]
        # naive directive handling (real impl would parse structured edits)
        if "medium" in e and "large" in fix["after"].lower():
            after = after.replace("LARGE", "MEDIUM").replace("Large", "Medium")
        if "coalesce to 0" in e:
            after = after.replace("COALESCE(TRY_TO_NUMBER(amount), -1)", "COALESCE(TRY_TO_NUMBER(amount), 0)")
        fix["after"] = after + note
        fix["title"] += " (user-modified)"
        fix["user_edit"] = edit
        return fix
