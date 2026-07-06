from __future__ import annotations
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

    def suggest(self, pipeline_id: str, incident_id: Optional[str] = None,
                user_edit: Optional[str] = None) -> Dict[str, Any]:
        idx = {p["id"]: p for p in MonitoringAgent().collect()}
        target = idx.get(pipeline_id, {})
        # determine the artifact to fix: prefer the RCA root node's artifact
        rca = RCAAgent().analyze(pipeline_id) if not incident_id else None
        root_id = rca["root_cause_node"] if rca else pipeline_id
        category = rca["category"] if rca else _classify(target.get("error"))
        artifact = _artifact_from_pipeline(root_id, idx)

        # memory: reuse an accepted fix for this signature
        signature = f"{artifact['platform']} {category}"
        prior = self.recall(signature)

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

        if user_edit:
            fix = self._apply_user_edit(fix, user_edit)

        # optional richer reasoning via Claude
        llm = self.think({"target": target, "category": category, "artifact": artifact,
                          "user_edit": user_edit})
        if llm and llm.get("after"):
            fix["after"] = llm["after"]
            fix["rationale"] = llm.get("rationale", fix["rationale"])

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
