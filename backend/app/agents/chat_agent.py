from __future__ import annotations
import json
from typing import Any, Dict, List, Optional

from app.agents.base import BaseAgent
from app.agents.fix_agent import FixAgent
from app.agents.monitoring_agent import MonitoringAgent
from app.agents.rca_agent import RCAAgent, _classify
from app.agents.test_agent import TestAgent
from app.core.config import (get_dq_monitoring_config, get_task_monitoring_config,
                             load_settings, platforms_configured)
from app.memory.memory_store import incident_log


def _detect_intent(msg: str, context: Optional[Dict[str, Any]] = None) -> str:
    m = msg.lower()
    ctx = context or {}
    dash_view = (ctx.get("dashboard") or {}).get("view", "")

    if any(k in m for k in ("validate", "run test", "test the fix", "zero copy", "clone")):
        return "validate_fix"
    if any(k in m for k in ("modify fix", "change the fix", "use medium", "edit fix")) and "instead" in m:
        return "modify_fix"

    if any(k in m for k in (
        "failed dq", "dq fail", "quality fail", "qc fail", "failed check", "failed checks",
        "dq failure", "data quality check", "dq checks",
    )):
        return "failed_dq"
    if "failed" in m and any(k in m for k in ("dq", "quality", "qc")):
        return "failed_dq"
    if dash_view == "dq" and any(k in m for k in ("failed", "failure", "details", "show", "list")):
        return "failed_dq"

    if any(k in m for k in ("failed task", "task fail", "failed tasks", "task failure", "delayed task")):
        return "failed_tasks"
    if "failed" in m and "task" in m:
        return "failed_tasks"
    if dash_view == "tasks" and any(k in m for k in ("failed", "failure", "delayed", "details", "show", "list")):
        return "failed_tasks"

    if any(k in m for k in ("dq summary", "dq status", "dq kpi", "data quality summary")):
        return "dq_status"
    if any(k in m for k in ("task summary", "task status", "task kpi", "task monitoring summary")):
        return "task_status"

    if any(k in m for k in (
        "resolution plan", "how to fix", "how should i", "what should i do",
        "remediation steps", "resolution suggestion",
    )):
        return "resolution"

    if any(k in m for k in ("suggest fix", "remediat", "patch")):
        return "suggest_fix"
    if "fix" in m and any(k in m for k in ("suggest", "propose", "recommend")):
        return "suggest_fix"

    if any(k in m for k in ("also check", "what about", "consider", "rerun rca", "re-run rca", "refine")):
        return "run_rca"
    if any(k in m for k in ("rca", "root cause", "why did", "why is", "analyze")):
        return "run_rca"

    if any(k in m for k in ("explain", "tell me about", "what happened", "details about")):
        return "explain_failure"

    if any(k in m for k in ("add issue", "report issue", "log issue", "new issue", "not captured")):
        return "add_issue"
    if any(k in m for k in ("status", "summary", "how many", "kpi", "overview")):
        return "status"
    return "general"


def _dash(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("dashboard") or {}


def _iso_range(date_from: Optional[str], date_to: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if date_from and "T" not in date_from:
        date_from = f"{date_from[:10]}T00:00:00+00:00"
    if date_to and "T" not in date_to:
        date_to = f"{date_to[:10]}T23:59:59+00:00"
    return date_from, date_to


def _dates_from_context(context: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    dash = _dash(context)
    return _iso_range(dash.get("dateFrom"), dash.get("dateTo"))


def _active_pipeline(context: Dict[str, Any], pipeline_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if pipeline_id:
        return context.get("activePipeline") or {"id": pipeline_id}
    return context.get("activePipeline")


def _failed_tasks(context: Dict[str, Any]) -> List[Dict[str, Any]]:
    dash = _dash(context)
    cached = dash.get("failedTasks") or []
    if cached:
        return cached
    date_from, date_to = _dates_from_context(context)
    summ = MonitoringAgent().summary("ALL", date_from, date_to)
    return [p for p in summ.get("all_pipelines", []) if p["status"] in ("FAILED", "DELAYED")]


def _failed_dq_checks(context: Dict[str, Any]) -> List[Dict[str, Any]]:
    dash = _dash(context)
    cached = dash.get("failedDqChecks") or []
    if cached:
        return cached
    date_from, date_to = _dates_from_context(context)
    summ = MonitoringAgent().dq_summary(date_from, date_to)
    return [c for c in summ.get("all_checks", []) if c["status"] == "FAILED"]


def _find_dq_check(context: Dict[str, Any], pipeline_id: Optional[str],
                   message: str) -> Optional[Dict[str, Any]]:
    checks = _failed_dq_checks(context)
    if pipeline_id and pipeline_id.startswith("dq_"):
        for c in checks:
            if c.get("id") == pipeline_id:
                return c
    active = _active_pipeline(context, pipeline_id)
    if active and str(active.get("id", "")).startswith("dq_"):
        for c in checks:
            if c.get("id") == active["id"]:
                return c
    m = message.lower()
    for c in checks:
        name = str(c.get("name", "")).lower()
        if name and name in m:
            return c
    return checks[0] if len(checks) == 1 else None


class ChatAgent(BaseAgent):
    name = "chat"
    skill_file = "chat.md"

    # ── LLM generation (multi-turn, context-aware) ────────────────────────────

    def _build_system_prompt(self, intent: str, context: Dict[str, Any]) -> str:
        """Assemble a rich system prompt with personality + domain + dashboard state."""
        dash = _dash(context)
        view = dash.get("view", "tasks")
        date_from = dash.get("dateFrom", "recent")
        date_to = dash.get("dateTo", "now")

        tone_hint = {
            "run_rca": "Be analytical. Explain root causes clearly, referencing specific tasks and errors.",
            "suggest_fix": "Be actionable. Describe exactly what to change and why.",
            "failed_tasks": "Be diagnostic. Summarize failures concisely with error highlights.",
            "failed_dq": "Be diagnostic. Explain which quality checks failed and their implications.",
            "resolution": "Be prescriptive. Give a clear step-by-step resolution path.",
            "explain_failure": "Be thorough but concise. Explain what happened and what it means.",
            "validate_fix": "Be confirmatory. Summarize test results clearly.",
            "status": "Be brief. Lead with numbers and key insights.",
        }.get(intent, "Be helpful and conversational.")

        return (
            "You are an expert data operations analyst in a monitoring dashboard called "
            "'Agentic Ops Monitoring & QC'. You have deep expertise in Snowflake tasks, "
            "data quality pipelines, SQL procedures, and data lineage.\n\n"
            "## Response Guidelines\n"
            "- Lead with concrete data: task names, error messages, table names, counts\n"
            "- Use markdown: **bold** for emphasis, `code` for SQL/identifiers, bullet lists\n"
            "- Keep responses concise (3-8 sentences for simple queries, more for analysis)\n"
            "- Always end with a clear next step the user can take\n"
            "- Never repeat raw JSON or dump structured data verbatim\n"
            "- Never use generic filler like 'I can help with that'\n\n"
            f"## Tone\n{tone_hint}\n\n"
            f"## Current Dashboard State\n"
            f"- View: {view} | Date range: {date_from} → {date_to}\n"
            f"- Task KPIs: {json.dumps(dash.get('taskKpis') or {}, default=str)}\n"
            f"- DQ KPIs: {json.dumps(dash.get('dqKpis') or {}, default=str)}\n\n"
            f"## Domain Knowledge\n{self.skill[:2000]}"
        )

    def _build_messages(self, message: str, data: Any,
                        history: Optional[List[Dict[str, str]]] = None) -> List[Dict[str, str]]:
        """Construct multi-turn messages array with data context."""
        messages: List[Dict[str, str]] = []
        if history:
            messages.extend(history[-20:])

        data_str = json.dumps(data, default=str)[:4000] if data else ""
        user_content = message
        if data_str:
            user_content += (
                f"\n\n---\n[Reference data — use to inform your answer, do not repeat verbatim]\n"
                f"{data_str}"
            )
        messages.append({"role": "user", "content": user_content})
        return messages

    def _generate_reply(self, intent: str, data: Any, message: str,
                        context: Dict[str, Any], history: Optional[List[Dict[str, str]]],
                        fallback: str) -> str:
        """Generate a natural conversational reply using multi-turn speak()."""
        if not self.harness.available:
            return fallback
        system = self._build_system_prompt(intent, context)
        messages = self._build_messages(message, data, history)
        max_tok = 2000 if intent in ("run_rca", "resolution", "explain_failure") else 1500
        result = self.harness.speak(system, messages, max_tokens=max_tok)
        return result.strip() if result else fallback

    def _generate_reply_stream(self, intent: str, data: Any, message: str,
                               context: Dict[str, Any],
                               history: Optional[List[Dict[str, str]]],
                               fallback: str):
        """Streaming version — yields text chunks for SSE."""
        if not self.harness.available:
            yield fallback
            return
        system = self._build_system_prompt(intent, context)
        messages = self._build_messages(message, data, history)
        max_tok = 2000 if intent in ("run_rca", "resolution", "explain_failure") else 1500
        streamed = False
        for chunk in self.harness.speak_stream(system, messages, max_tokens=max_tok):
            streamed = True
            yield chunk
        if not streamed:
            yield fallback

    def handle(self, message: str, pipeline_id: Optional[str] = None,
               context: Optional[Dict[str, Any]] = None,
               history: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
        context = context or {}
        intent = _detect_intent(message, context)
        payload: Optional[Dict[str, Any]] = None
        agent = "chat"
        reply = ""

        needs_pipeline = intent in ("run_rca", "suggest_fix", "modify_fix", "validate_fix", "resolution", "explain_failure")

        if needs_pipeline and pipeline_id:
            if intent == "run_rca":
                extra = message if any(k in message.lower() for k in
                                       ("also", "check", "what about", "consider", "because", "source")) else None
                payload = RCAAgent().analyze(pipeline_id, extra_context=extra)
                agent = "rca"
                fallback = self._format_rca_reply(payload, extra)
                reply = payload.get("chat_narrative") or self._generate_reply(intent, payload, message, context, history, fallback)

            elif intent == "resolution":
                fallback, payload, agent = self._resolution_reply(pipeline_id, context)
                reply = self._generate_reply(intent, payload, message, context, history, fallback)

            elif intent == "explain_failure":
                if pipeline_id.startswith("dq_"):
                    fallback, payload, agent = self._dq_detail_reply(pipeline_id, message, context)
                    intent = "failed_dq"
                else:
                    payload = RCAAgent().analyze(pipeline_id)
                    agent = "rca"
                    fallback = self._format_rca_reply(payload, None, detailed=True)
                reply = payload.get("chat_narrative") if payload and payload.get("chat_narrative") else self._generate_reply(intent, payload, message, context, history, fallback)

            elif intent == "suggest_fix":
                if pipeline_id.startswith("dq_"):
                    fallback, payload, agent = self._dq_resolution_reply(pipeline_id, context)
                    intent = "failed_dq"
                else:
                    payload = FixAgent().suggest(pipeline_id)
                    agent = "fix"
                    fallback = (f"Suggested fix: {payload['title']} (risk={payload['risk']}).\n"
                                f"{payload.get('rationale', '')}\n\n"
                                f"Open Workbench to review the before/after diff, or ask me to validate it.")
                reply = self._generate_reply(intent, payload, message, context, history, fallback)

            elif intent == "modify_fix":
                payload = FixAgent().suggest(pipeline_id, user_edit=message)
                agent = "fix"
                fallback = f"Applied your edit. Updated fix: {payload['title']}. Want me to validate it?"
                reply = self._generate_reply(intent, payload, message, context, history, fallback)

            elif intent == "validate_fix":
                payload = TestAgent().validate(pipeline_id)
                agent = "test"
                s = payload["summary"]
                fallback = (f"Validation on zero-copy clone {payload['clone_name']} ({payload['environment']}):\n"
                            f"{s['passed']}/{s['total']} passed, overall {payload['overall']}.")
                reply = self._generate_reply(intent, payload, message, context, history, fallback)

        elif intent == "failed_tasks":
            payload, fallback = self._failed_tasks_reply(context)
            agent = "monitoring"
            reply = self._generate_reply(intent, payload, message, context, history, fallback)

        elif intent == "failed_dq":
            payload, fallback = self._failed_dq_reply(context, message)
            agent = "monitoring"
            reply = self._generate_reply(intent, payload, message, context, history, fallback)

        elif intent == "task_status":
            payload, fallback = self._task_status_reply(context)
            agent = "monitoring"
            reply = self._generate_reply(intent, payload, message, context, history, fallback)

        elif intent == "dq_status":
            payload, fallback = self._dq_status_reply(context)
            agent = "monitoring"
            reply = self._generate_reply(intent, payload, message, context, history, fallback)

        elif intent == "resolution":
            fallback, payload, agent = self._resolution_reply(pipeline_id, context)
            reply = self._generate_reply(intent, payload, message, context, history, fallback)

        elif intent == "explain_failure":
            active = _active_pipeline(context, pipeline_id)
            if active and str(active.get("id", "")).startswith("dq_"):
                fallback, payload, agent = self._dq_detail_reply(active["id"], message, context)
                intent = "failed_dq"
            elif active and active.get("id"):
                payload = RCAAgent().analyze(active["id"])
                agent = "rca"
                fallback = self._format_rca_reply(payload, None, detailed=True)
            else:
                dash_view = _dash(context).get("view", "tasks")
                if dash_view == "dq":
                    payload, fallback = self._failed_dq_reply(context, message)
                    agent = "monitoring"
                    intent = "failed_dq"
                else:
                    payload, fallback = self._failed_tasks_reply(context)
                    agent = "monitoring"
                    intent = "failed_tasks"
            reply = self._generate_reply(intent, payload, message, context, history, fallback)

        elif intent == "add_issue":
            payload = self._add_issue(message, pipeline_id)
            agent = "memory"
            fallback = (f"Logged your issue to agent memory as {payload['incident_id']}. "
                        f"I'll match future incidents against it for faster resolution.")
            reply = self._generate_reply(intent, payload, message, context, history, fallback)

        elif intent == "status":
            dash_view = _dash(context).get("view")
            if dash_view == "dq":
                payload, fallback = self._dq_status_reply(context)
            elif dash_view == "tasks":
                payload, fallback = self._task_status_reply(context)
            else:
                payload, fallback = self._combined_status_reply(context)
            agent = "monitoring"
            reply = self._generate_reply(intent, payload, message, context, history, fallback)

        elif needs_pipeline and not pipeline_id:
            fallback = self._no_pipeline_hint(intent, message, context)
            reply = self._generate_reply(intent, None, message, context, history, fallback)

        else:
            intent = "general"
            fallback = self._general_fallback(message, pipeline_id, context)
            reply = self._generate_reply(intent, None, message, context, history, fallback)

        self.learn({"signature": f"chat {intent}", "message": message,
                    "pipeline_id": pipeline_id, "intent": intent})
        return {"reply": reply, "intent": intent, "agent": agent, "payload": payload}

    def handle_stream(self, message: str, pipeline_id: Optional[str] = None,
                      context: Optional[Dict[str, Any]] = None,
                      history: Optional[List[Dict[str, str]]] = None):
        """Same as handle() but yields text chunks for SSE streaming.

        Returns a tuple: (metadata_dict, text_generator)
        """
        context = context or {}
        intent = _detect_intent(message, context)
        payload: Optional[Dict[str, Any]] = None
        agent = "chat"
        fallback = ""

        needs_pipeline = intent in ("run_rca", "suggest_fix", "modify_fix", "validate_fix", "resolution", "explain_failure")

        if needs_pipeline and pipeline_id:
            if intent == "run_rca":
                extra = message if any(k in message.lower() for k in
                                       ("also", "check", "what about", "consider", "because", "source")) else None
                payload = RCAAgent().analyze(pipeline_id, extra_context=extra)
                agent = "rca"
                fallback = payload.get("chat_narrative") or self._format_rca_reply(payload, extra)
            elif intent == "resolution":
                fallback, payload, agent = self._resolution_reply(pipeline_id, context)
            elif intent == "explain_failure":
                if pipeline_id.startswith("dq_"):
                    fallback, payload, agent = self._dq_detail_reply(pipeline_id, message, context)
                    intent = "failed_dq"
                else:
                    payload = RCAAgent().analyze(pipeline_id)
                    agent = "rca"
                    fallback = payload.get("chat_narrative") or self._format_rca_reply(payload, None, detailed=True)
            elif intent == "suggest_fix":
                if pipeline_id.startswith("dq_"):
                    fallback, payload, agent = self._dq_resolution_reply(pipeline_id, context)
                    intent = "failed_dq"
                else:
                    payload = FixAgent().suggest(pipeline_id)
                    agent = "fix"
                    fallback = (f"Suggested fix: {payload['title']} (risk={payload['risk']}).\n"
                                f"{payload.get('rationale', '')}")
            elif intent == "modify_fix":
                payload = FixAgent().suggest(pipeline_id, user_edit=message)
                agent = "fix"
                fallback = f"Applied your edit. Updated fix: {payload['title']}."
            elif intent == "validate_fix":
                payload = TestAgent().validate(pipeline_id)
                agent = "test"
                s = payload["summary"]
                fallback = f"Validation: {s['passed']}/{s['total']} passed, overall {payload['overall']}."
        elif intent == "failed_tasks":
            payload, fallback = self._failed_tasks_reply(context)
            agent = "monitoring"
        elif intent == "failed_dq":
            payload, fallback = self._failed_dq_reply(context, message)
            agent = "monitoring"
        elif intent == "task_status":
            payload, fallback = self._task_status_reply(context)
            agent = "monitoring"
        elif intent == "dq_status":
            payload, fallback = self._dq_status_reply(context)
            agent = "monitoring"
        elif intent == "resolution":
            fallback, payload, agent = self._resolution_reply(pipeline_id, context)
        elif intent == "explain_failure":
            active = _active_pipeline(context, pipeline_id)
            if active and str(active.get("id", "")).startswith("dq_"):
                fallback, payload, agent = self._dq_detail_reply(active["id"], message, context)
                intent = "failed_dq"
            elif active and active.get("id"):
                payload = RCAAgent().analyze(active["id"])
                agent = "rca"
                fallback = payload.get("chat_narrative") or self._format_rca_reply(payload, None, detailed=True)
            else:
                dash_view = _dash(context).get("view", "tasks")
                if dash_view == "dq":
                    payload, fallback = self._failed_dq_reply(context, message)
                    agent = "monitoring"
                    intent = "failed_dq"
                else:
                    payload, fallback = self._failed_tasks_reply(context)
                    agent = "monitoring"
                    intent = "failed_tasks"
        elif intent == "status":
            dash_view = _dash(context).get("view")
            if dash_view == "dq":
                payload, fallback = self._dq_status_reply(context)
            elif dash_view == "tasks":
                payload, fallback = self._task_status_reply(context)
            else:
                payload, fallback = self._combined_status_reply(context)
            agent = "monitoring"
        elif needs_pipeline and not pipeline_id:
            fallback = self._no_pipeline_hint(intent, message, context)
        else:
            intent = "general"
            fallback = self._general_fallback(message, pipeline_id, context)

        self.learn({"signature": f"chat {intent}", "message": message,
                    "pipeline_id": pipeline_id, "intent": intent})

        meta = {"intent": intent, "agent": agent, "payload": payload}
        generator = self._generate_reply_stream(intent, payload, message, context, history, fallback)
        return meta, generator

    def _format_rca_reply(self, payload: Dict[str, Any], extra: Optional[str],
                          detailed: bool = False) -> str:
        if payload.get("error"):
            return f"RCA could not run: {payload['error']}"
        lines = [
            f"RCA — {payload.get('pipeline_id', 'pipeline')}",
            f"Summary: {payload['summary']}",
            f"Category: {payload['category']} · confidence {payload['confidence']}",
            f"Root cause: {payload.get('root_cause_name', 'unknown')}",
            f"Downstream impact: {len(payload.get('impacted_nodes', []))} node(s)",
        ]
        tables = payload.get("affected_tables") or []
        if tables:
            lines.append(f"\nAffected tables ({len(tables)}):")
            for t in tables[:12]:
                lines.append(f"  • {t.get('table')} ({t.get('source')}, {t.get('role')})")
            if len(tables) > 12:
                lines.append(f"  … and {len(tables) - 12} more (see Workbench lineage table)")
        if payload.get("related_dq_failures"):
            names = [d.get("name") for d in payload["related_dq_failures"][:5] if d.get("name")]
            if names:
                lines.append(f"\nRelated DQ failures: {', '.join(names)}")
        if payload.get("evidence"):
            lines.append("\nEvidence:")
            for ev in payload["evidence"][:4]:
                lines.append(f"  • {ev[:200]}")
        if detailed:
            lines.append("\nNext steps:")
            lines.append("  • Ask for a 'resolution plan' or 'suggest fix'")
            lines.append("  • Open Workbench for lineage graph and fix diff")
            lines.append("  • Say 'validate fix' after approving a change")
        if extra:
            lines.append("\n(Incorporated your additional context.)")
        return "\n".join(lines)

    def _failed_tasks_reply(self, context: Dict[str, Any]) -> tuple[Dict[str, Any], str]:
        failed = _failed_tasks(context)
        dash = _dash(context)
        kpis = dash.get("taskKpis") or {}
        date_from = dash.get("dateFrom", "?")
        date_to = dash.get("dateTo", "?")
        task_cfg = get_task_monitoring_config(load_settings())

        if not failed:
            return (
                {"failed": [], "kpis": kpis},
                (f"No failed or delayed tasks in {date_from} → {date_to}.\n"
                 f"Monitoring: {task_cfg['monitor_database']} · pattern {task_cfg['name_pattern']}\n"
                 f"KPIs — failed: {kpis.get('failed', 0)}, delayed: {kpis.get('delayed', 0)}, "
                 f"success: {kpis.get('success', 0)}."),
            )

        lines = [
            f"Failed / delayed tasks ({date_from} → {date_to})",
            f"Source: {task_cfg['monitor_database']} · {task_cfg['name_pattern']}",
            f"Count: {len(failed)} (dashboard KPIs: {kpis.get('failed', 0)} failed, "
            f"{kpis.get('delayed', 0)} delayed)\n",
        ]
        for i, p in enumerate(failed[:10], 1):
            err = (p.get("error") or "no error message recorded")[:160]
            dur = f"{p.get('duration_s')}s" if p.get("duration_s") is not None else "—"
            lines.append(
                f"{i}. {p['name']} ({p.get('platform', '?')}) · {p['status']} · duration {dur}\n"
                f"   Error: {err}\n"
                f"   → Click the row, then ask: Run RCA · resolution plan · suggest fix"
            )
        if len(failed) > 10:
            lines.append(f"\n… and {len(failed) - 10} more. Narrow the date range or filter to FAILED on the Dashboard.")
        return {"failed": failed[:10], "kpis": kpis}, "\n".join(lines)

    def _failed_dq_reply(self, context: Dict[str, Any], message: str) -> tuple[Dict[str, Any], str]:
        failed = _failed_dq_checks(context)
        dash = _dash(context)
        kpis = dash.get("dqKpis") or {}
        source = dash.get("dqSource") or {}
        date_from = dash.get("dateFrom", "?")
        date_to = dash.get("dateTo", "?")
        dq_cfg = get_dq_monitoring_config(load_settings())
        table = source.get("table") or dq_cfg["table_fqn"]
        area = source.get("subject_area") or dq_cfg["subject_area"]

        if not failed:
            return (
                {"failed": [], "kpis": kpis},
                (f"No failed DQ checks in {date_from} → {date_to}.\n"
                 f"Source: {table} · subject area '{area}'\n"
                 f"KPIs — failed: {kpis.get('failed', 0)}, success: {kpis.get('success', 0)}."),
            )

        lines = [
            f"Failed data quality checks ({date_from} → {date_to})",
            f"Source: {table} · subject area '{area}'",
            f"Count: {len(failed)} (dashboard shows {kpis.get('failed', 0)} failed)\n",
        ]
        for i, c in enumerate(failed[:10], 1):
            detail = (c.get("error") or "no details")[:160]
            lines.append(
                f"{i}. QC {c.get('name', '?')} · {c.get('column_name') or 'check'} · "
                f"run {c.get('run_at') or '—'}\n"
                f"   Subject: {c.get('table_name') or area} · {detail}\n"
                f"   → Click the row, then ask: explain failure · resolution plan"
            )
        if len(failed) > 10:
            lines.append(f"\n… and {len(failed) - 10} more failed checks in this range.")
        return {"failed": failed[:10], "kpis": kpis, "source": {"table": table, "subject_area": area}}, "\n".join(lines)

    def _dq_detail_reply(self, check_id: str, message: str,
                         context: Dict[str, Any]) -> tuple[str, Dict[str, Any], str]:
        check = _find_dq_check(context, check_id, message)
        if not check:
            _, summary = self._failed_dq_reply(context, message)
            return summary + "\n\nSelect a specific failed DQ row on the Dashboard for drill-down.", {}, "monitoring"

        category = _classify(check.get("error"))
        payload = {"check": check, "category": category}
        lines = [
            f"DQ check detail — QC {check.get('name')}",
            f"Status: {check.get('status')} · run at {check.get('run_at') or '—'}",
            f"Subject area: {check.get('table_name') or '—'} · check type: {check.get('column_name') or '—'}",
            f"Details: {check.get('error') or '—'}",
            f"Likely category: {category}",
            "\nRecommended resolution:",
        ]
        lines.extend(self._dq_resolution_steps(check, category))
        return "\n".join(lines), payload, "monitoring"

    def _dq_resolution_steps(self, check: Dict[str, Any], category: str) -> List[str]:
        steps = []
        if category == "data":
            steps += [
                "Inspect source rows that fail the QC rule (nulls, grain breaks, invalid casts).",
                "Compare PASS_COUNT vs FAIL_COUNT trends for this QC_ID in recent runs.",
                "Fix upstream ETL or adjust the QC threshold if the rule is too strict.",
            ]
        elif category == "dependency":
            steps += [
                "Verify upstream tasks feeding this subject area completed successfully.",
                "Check whether source tables were refreshed before the DQ run.",
            ]
        elif category == "permission":
            steps += [
                "Confirm the monitoring role can read the DQ summary and underlying tables.",
            ]
        else:
            steps += [
                "Review QC_DESCRIPTION and failing record samples in Snowflake.",
                "Re-run the validation after fixing source data or the pipeline.",
                "Log a manual issue if this is a recurring false positive.",
            ]
        steps.append("Ask me for a 'resolution plan' on a selected task if the root cause is a failed upstream job.")
        return [f"  • {s}" for s in steps[:5]]

    def _dq_resolution_reply(self, check_id: str,
                             context: Dict[str, Any]) -> tuple[str, Dict[str, Any], str]:
        return self._dq_detail_reply(check_id, "resolution", context)

    def _resolution_reply(self, pipeline_id: Optional[str],
                          context: Dict[str, Any]) -> tuple[str, Optional[Dict[str, Any]], str]:
        active = _active_pipeline(context, pipeline_id)
        pid = pipeline_id or (active or {}).get("id")

        if pid and str(pid).startswith("dq_"):
            return self._dq_detail_reply(pid, "resolution", context)

        if not pid:
            dash_view = _dash(context).get("view", "tasks")
            hint = ("Select a failed task row on the Dashboard (Tasks tab) or a DQ check (Data Quality tab), "
                    "then ask again for a resolution plan.")
            if dash_view == "dq":
                _, summary = self._failed_dq_reply(context, "resolution")
                return summary + f"\n\n{hint}", None, "monitoring"
            _, summary = self._failed_tasks_reply(context)
            return summary + f"\n\n{hint}", None, "monitoring"

        rca = RCAAgent().analyze(pid)
        if rca.get("error"):
            return f"Could not build a resolution plan: {rca['error']}", {"rca": rca}, "rca"

        fix = FixAgent().suggest(pid, incident_id=rca.get("incident_id"))
        lines = [
            f"Resolution plan — {pid}",
            f"\n1. Root cause ({rca['category']}, confidence {rca['confidence']})",
            f"   {rca['summary']}",
            f"\n2. Recommended fix ({fix.get('risk', '?')} risk)",
            f"   {fix.get('title', 'N/A')}",
            f"   {fix.get('rationale', '')}",
        ]
        hints = fix.get("validation_hints") or []
        if hints:
            lines.append("\n3. Validation criteria")
            for h in hints[:4]:
                lines.append(f"   • {h}")
        lines.append("\n4. Next steps")
        lines.append("   • Open Workbench to review the before/after diff")
        lines.append("   • Ask 'validate fix' to test on a zero-copy Snowflake clone")
        lines.append("   • Say 'modify fix' with your edits if the suggestion needs tweaking")
        payload = {"rca": rca, "fix": fix}
        return "\n".join(lines), payload, "rca"

    def _task_status_reply(self, context: Dict[str, Any]) -> tuple[Dict[str, Any], str]:
        dash = _dash(context)
        kpis = dash.get("taskKpis")
        date_from, date_to = _dates_from_context(context)
        if not kpis:
            summ = MonitoringAgent().summary("ALL", date_from, date_to)
            kpis = summ["kpis"]
        else:
            summ = {"kpis": kpis, "errors": []}
        task_cfg = get_task_monitoring_config(load_settings())
        k = kpis
        reply = (
            f"Task monitoring ({dash.get('dateFrom', '?')} → {dash.get('dateTo', '?')})\n"
            f"Database: {task_cfg['monitor_database']} · pattern {task_cfg['name_pattern']}\n\n"
            f"KPIs: {k.get('success', 0)} success · {k.get('failed', 0)} failed · "
            f"{k.get('delayed', 0)} delayed · {k.get('skipped', 0)} skipped · "
            f"{k.get('running', 0)} running · {k.get('total', 0)} total\n\n"
            f"Ask 'failed tasks' for error details or select a row and ask for RCA / resolution plan."
        )
        return summ, reply

    def _dq_status_reply(self, context: Dict[str, Any]) -> tuple[Dict[str, Any], str]:
        dash = _dash(context)
        kpis = dash.get("dqKpis")
        source = dash.get("dqSource") or {}
        date_from, date_to = _dates_from_context(context)
        if not kpis:
            summ = MonitoringAgent().dq_summary(date_from, date_to)
            kpis = summ["kpis"]
        else:
            summ = {"kpis": kpis, "errors": [], "table": source.get("table"), "subject_area": source.get("subject_area")}
        dq_cfg = get_dq_monitoring_config(load_settings())
        table = source.get("table") or dq_cfg["table_fqn"]
        area = source.get("subject_area") or dq_cfg["subject_area"]
        k = kpis
        reply = (
            f"Data quality ({dash.get('dateFrom', '?')} → {dash.get('dateTo', '?')})\n"
            f"Source: {table} · subject area '{area}'\n\n"
            f"KPIs: {k.get('success', 0)} passed · {k.get('failed', 0)} failed · "
            f"{k.get('delayed', 0)} warning · {k.get('skipped', 0)} skipped · "
            f"{k.get('total', 0)} total\n\n"
            f"Ask 'failed DQ checks' for details or select a row for resolution guidance."
        )
        return summ, reply

    def _combined_status_reply(self, context: Dict[str, Any]) -> tuple[Dict[str, Any], str]:
        _, task_reply = self._task_status_reply(context)
        _, dq_reply = self._dq_status_reply(context)
        return {}, f"{task_reply}\n\n---\n\n{dq_reply}"

    def report_activity_error(self, error_ctx: Dict[str, Any]) -> Dict[str, Any]:
        llm = self.think({"type": "activity_error", **error_ctx})
        reply = (llm or {}).get("reply") if llm else None
        if not reply:
            reply = self._activity_error_reply(error_ctx)
        self.learn({"signature": f"activity-error {error_ctx.get('action', '')[:40]}",
                    "error": error_ctx.get("error"), "tab": error_ctx.get("tab")})
        return {"reply": reply, "intent": "activity_error", "agent": "chat", "payload": error_ctx}

    def _activity_error_reply(self, ctx: Dict[str, Any]) -> str:
        tab = ctx.get("tab") or "the app"
        action = ctx.get("action") or "an operation"
        error = ctx.get("error") or "Unknown error"
        details = ctx.get("details") or {}
        steps = self._error_next_steps(tab, action, error, details)
        step_text = "\n".join(f"• {s}" for s in steps)
        return (
            f"I noticed a problem while you were on {tab} trying to {action}.\n\n"
            f"What happened: {error}\n\n"
            f"What to try next:\n{step_text}"
        )

    def _error_next_steps(self, tab: str, action: str, error: str,
                          details: Dict[str, Any]) -> list[str]:
        e = error.lower()
        steps: list[str] = []

        if "401" in e or "403" in e or "unauthorized" in e or "authentication" in e:
            steps += ["Open Settings and verify your credentials are correct.",
                      "Re-enter secrets — masked values (with ***) are not saved again.",
                      "For Snowflake SSO, complete the browser login on the backend machine."]
        if "400" in e or "required" in e or "missing" in e:
            steps += ["Check Settings for missing mandatory fields: Account, User, Warehouse, Role, and Password or SSO.",
                      "Optional fields (Database, Schema, Pre-prod Account) can be left blank."]
        if "timeout" in e or "timed out" in e:
            steps += ["The request timed out — retry in a moment.",
                      "For SSO, ensure you complete browser sign-in within 2 minutes."]
        if "not found" in e or "404" in e:
            steps += ["The requested pipeline or resource may no longer exist — refresh the Dashboard.",
                      "Select a pipeline from the current list before running RCA or fix actions."]
        if tab == "dashboard":
            steps += ["Empty results mean no matching jobs were found in your live Snowflake/AWS data.",
                      "Verify credentials in Settings if you expected pipelines to appear.",
                      "Ask me about 'failed tasks' or 'failed DQ checks' for the current date range."]
        if "snowflake" in e or details.get("platform") == "snowflake":
            steps += ["Verify Snowflake Account, User, Warehouse, and Role in Settings.",
                      "For SSO: select SSO login method and ensure browser auth completes on the backend host."]
        if "aws" in e or details.get("platform") == "aws":
            steps += ["Verify AWS Access Key ID, Secret Access Key, and Region in Settings."]

        if not steps:
            steps = [
                "Refresh the page and retry the action.",
                "Check Settings → connectivity status for failed services.",
                "Ask me a specific question about the error and I'll help troubleshoot.",
            ]
        return steps[:5]

    def _no_pipeline_hint(self, intent: str, message: str,
                          context: Dict[str, Any]) -> str:
        action = {"run_rca": "run RCA", "suggest_fix": "suggest a fix",
                  "modify_fix": "modify a fix", "validate_fix": "validate a fix",
                  "resolution": "build a resolution plan",
                  "explain_failure": "explain the failure"}.get(intent, "do that")
        dash = _dash(context)
        view = dash.get("view", "tasks")
        view_hint = "Data Quality" if view == "dq" else "Tasks"
        return (
            f"To {action}, select a failed row on the Dashboard ({view_hint} tab) or open it in Workbench.\n\n"
            f"You can also ask:\n"
            f"  • 'failed tasks' or 'failed DQ checks' — list failures with details\n"
            f"  • 'task summary' or 'DQ summary' — KPI overview\n"
            f"  • 'resolution plan' — after selecting a specific failure"
        )

    def _general_fallback(self, message: str, pipeline_id: Optional[str],
                          context: Dict[str, Any]) -> str:
        """Template-only fallback for general intent when LLM is unavailable or fails."""
        settings = load_settings()
        m = message.lower()

        if any(k in m for k in ("help", "what can you", "what do you")):
            return self._help_reply(context)

        if any(k in m for k in ("sso", "single sign", "okta", "saml")):
            return ("SSO uses browser-based login on the machine running the backend. "
                    "In Settings → Snowflake, choose SSO, enter Account, User, Warehouse, and Role, "
                    "then Save & Test. A browser window should open — complete sign-in within 2 minutes.")

        if any(k in m for k in ("data source", "configured", "empty", "no pipeline")):
            platforms = platforms_configured(settings)
            on = [p for p, v in platforms.items() if v]
            if not on:
                return ("No data platforms are configured yet. Go to Settings and add Snowflake credentials. "
                        "The dashboard shows live task and DQ data from Snowflake.")
            return (f"Configured: {', '.join(on)}. "
                    "Tasks tab reads SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY; "
                    "Data Quality tab reads your DQM_VALIDATION_SUMMARY table (configurable in Settings).")

        if any(k in m for k in ("connect", "credential", "settings", "password")):
            return ("Settings has three areas: Connections, Task monitoring, and Data quality. "
                    "Configure Snowflake credentials plus which database/table to monitor. "
                    "I can help troubleshoot specific connection errors.")

        if any(k in m for k in ("hello", "hi", "hey", "how are you", "good morning",
                                   "good afternoon", "good evening", "greetings", "what's up")):
            return (
                "Hey! I'm doing great — ready to help you investigate any data pipeline issues. "
                "I can run root cause analysis on failed tasks, trace lineage, identify fixes, "
                "and explain what went wrong in plain language.\n\n"
                "What would you like to look into? You can ask about failed tasks, DQ checks, "
                "or select a row from the Dashboard and I'll dig into it for you."
            )

        active = _active_pipeline(context, pipeline_id)
        if active and active.get("id"):
            name = active.get("name", active["id"])
            status = active.get("status", "?")
            err = active.get("error") or ""
            return (
                f"Context: {name} ({status}).\n"
                f"{f'Error: {err[:200]}' if err else ''}\n\n"
                f"Try: 'explain failure' · 'Run RCA' · 'resolution plan' · 'suggest fix'"
            ).strip()

        return (
            f"I'm your data ops assistant! I monitor Snowflake tasks and data quality checks in real-time. "
            f"Here's what I can help with:\n\n"
            f"  • **Failed tasks** — find and investigate task failures with root cause analysis\n"
            f"  • **Data Quality** — check DQ failures, trace lineage, suggest fixes\n"
            f"  • **Resolution** — end-to-end: RCA → Fix → Validate on zero-copy clone\n\n"
            f"Try asking: 'show failed tasks', 'failed DQ checks', or select a row from the Dashboard."
        )

    def _help_reply(self, context: Dict[str, Any]) -> str:
        dash = _dash(context)
        view = dash.get("view", "tasks")
        date_from = dash.get("dateFrom", "last 2 days")
        date_to = dash.get("dateTo", "today")
        lines = [
            "I'm your ops-monitor assistant, connected to this dashboard.",
            f"Current view: {view} · date range {date_from} → {date_to}\n",
            "Quick commands:",
            "  • failed tasks — list failed/delayed tasks with errors",
            "  • failed DQ checks — list failed quality checks",
            "  • task summary / DQ summary — KPI overview",
            "  • Run RCA — root-cause analysis (select a task first)",
            "  • resolution plan — RCA + suggested fix",
            "  • suggest fix / validate fix — in Workbench flow",
            "\nClick any table row to set context, then ask follow-up questions.",
        ]
        failed_t = len(dash.get("failedTasks") or [])
        failed_d = len(dash.get("failedDqChecks") or [])
        if failed_t or failed_d:
            lines.append(f"\nRight now: {failed_t} failed task(s), {failed_d} failed DQ check(s) in view.")
        return "\n".join(lines)

    def _add_issue(self, message: str, pipeline_id: Optional[str]) -> Dict[str, Any]:
        inc = incident_log.log({
            "pipeline_id": pipeline_id, "name": "User-reported issue", "platform": "manual",
            "status": "USER_REPORTED", "error": message, "source": "user",
            "signature": f"user {message[:50]}",
        })
        self.learn({"signature": f"user-issue {message[:50]}", "incident_id": inc["incident_id"],
                    "description": message})
        return inc
