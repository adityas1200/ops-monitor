import json

from app.agents import rca_agent as rca_module
from app.agents.chat_agent import ChatAgent, _detect_intent
from app.agents.rca_agent import (RCAAgent, _format_impact_summary,
                                  _impact_counts, _merge_impact_lists)


def _prior_rca():
    return {
        "incident_id": "inc-1",
        "pipeline_id": "dq_17_run",
        "analysis_type": "dq",
        "root_cause_node": "dq_17_run",
        "root_cause_name": "QC 17",
        "failure_type": "Data Quality Failure",
        "category": "Data Quality Failure",
        "summary": "Old conclusion",
        "detailed_analysis": "Old details",
        "confidence": 0.7,
        "confidence_level": "Medium",
        "evidence": ["QC 17 returned one mismatched row"],
        "root_cause": {"explanation": "Old root"},
        "code_analysis": {
            "task_sql": "select count(*) from SOURCE_TABLE",
            "llm_explanation": "Old code analysis",
        },
        "diagnostic_results": {"sample_rows": [{"ID": 42, "ACTUAL": 9, "EXPECTED": 10}]},
        "dq_execution_result": {"executed": True, "row_count": 1},
        "procedure_chain": [{"object_name": "LOAD_TARGET", "sql_body": "merge into TARGET"}],
        "affected_tables": [{"table": "SOURCE_TABLE", "role": "source"}],
        "lineage_table": [{"source": "SOURCE_TABLE", "target": "TARGET_TABLE"}],
        "investigation_journey": [],
        "impact_assessment": {},
        "remediation": {},
    }


def test_cached_result_is_returned_as_an_isolated_copy():
    rca_module._RCA_RESULT_CACHE.clear()
    prior = _prior_rca()
    rca_module._cache_rca_result(prior)

    cached = RCAAgent.get_cached_result(prior["pipeline_id"])
    cached["summary"] = "mutated"

    assert RCAAgent.get_cached_result(prior["pipeline_id"])["summary"] == "Old conclusion"


def test_correct_from_cached_reuses_structural_evidence(monkeypatch):
    rca_module._RCA_RESULT_CACHE.clear()
    prior = _prior_rca()
    rca_module._cache_rca_result(prior)
    agent = RCAAgent()
    agent.harness._client = object()
    llm_result = {
        "failure_type": "Data Quality Failure",
        "summary": "The mismatch is caused by missing ID 42 in the target.",
        "detailed_analysis": "Cached diagnostics show ACTUAL 9 versus EXPECTED 10 for ID 42.",
        "code_analysis": "The merge does not insert ID 42.",
        "root_cause": {
            "explanation": "ID 42 is absent from the target.",
            "business_explanation": "One expected record is missing.",
            "technical_explanation": "The target merge omitted ID 42.",
            "entities": [{"name": "TARGET_TABLE", "type": "table"}],
            "code_snippets": [],
            "comparison": {"expected": "10", "actual": "9"},
        },
        "evidence": ["ID 42 has ACTUAL 9 and EXPECTED 10"],
        "remediation": {"immediate_fix": "Reload ID 42"},
        "confidence": 0.91,
        "confidence_level": "High",
    }
    monkeypatch.setattr(
        agent.harness,
        "reason",
        lambda system, message, max_tokens: json.dumps(llm_result),
    )

    corrected = agent.correct_from_cached(
        prior["pipeline_id"],
        "Operator guidance: Correct RCA; inspect ID 42.",
    )

    assert corrected["summary"] == llm_result["summary"]
    assert corrected["incident_id"] == prior["incident_id"]
    assert corrected["diagnostic_results"] == prior["diagnostic_results"]
    assert corrected["code_analysis"]["task_sql"] == prior["code_analysis"]["task_sql"]
    assert corrected["refinement"]["reused_evidence"] is True
    assert corrected["investigation_journey"][-1]["step"] == "Operator correction applied"


def test_chat_correction_returns_fresh_payload_for_workbench(monkeypatch):
    regenerated = _prior_rca()
    regenerated["summary"] = "Fresh corrected RCA"
    captured = {}

    def analyze(self, pipeline_id, *args, **kwargs):
        captured["pipeline_id"] = pipeline_id
        captured.update(kwargs)
        return regenerated

    monkeypatch.setattr(RCAAgent, "analyze", analyze)
    payload, extra, _ = ChatAgent()._run_rca_from_chat(
        "Correct RCA with my suggestion: inspect ID 42",
        regenerated["pipeline_id"],
        {"rca": {"summary": "Old conclusion"}},
    )

    assert payload is regenerated
    assert "inspect ID 42" in extra
    assert captured["include_lineage"] is True


def test_impact_summary_includes_tables_pipelines_and_reports(caplog):
    impact = {
        "impacted_tables": ["DB.SC.TABLE_A"],
        "impacted_pipelines": ["PIPELINE_A"],
        "impacted_reports": ["REPORT_A", "REPORT_B"],
        "downstream_tasks": [],
    }

    with caplog.at_level("INFO", logger="app.agents.rca_agent"):
        summary = _format_impact_summary(impact)

    assert summary == (
        "Validation results impact:\n"
        "• Data Table: DB.SC.TABLE_A\n"
        "• Pipeline: PIPELINE_A\n"
        "• Reports: REPORT_A, REPORT_B\n"
        "Total Impact:\n"
        "1 Table • 1 Pipeline • 2 Reports"
    )
    assert _impact_counts(impact) == {
        "tables": 1, "pipelines": 1, "reports": 2, "tasks": 0}
    assert "tables" in caplog.text
    assert "pipelines" in caplog.text
    assert "reports" in caplog.text
    assert "tasks" in caplog.text


def test_impact_summary_includes_downstream_tasks_and_omits_zero_metrics():
    impact = {
        "impacted_tables": ["T1", "T2", "T3"],
        "impacted_pipelines": [],
        "impacted_reports": [],
        "downstream_tasks": ["TASK_A", "TASK_B"],
    }

    assert _format_impact_summary(impact) == (
        "Validation results impact:\n"
        "• Data Tables: T1, T2, T3\n"
        "Total Impact:\n"
        "3 Tables"
    )


def test_partial_lineage_merge_never_reduces_collected_impact():
    existing = {
        "impacted_tables": ["T1"],
        "impacted_pipelines": ["P1"],
        "impacted_reports": ["R1", "R2"],
        "downstream_tasks": [],
    }
    partial_discovery = {
        "impacted_tables": ["T1"],
        "impacted_pipelines": [],
        "impacted_reports": [],
        "downstream_tasks": ["D1"],
        "business_severity": "Critical",
    }

    merged = _merge_impact_lists(partial_discovery, existing)

    assert merged["impacted_pipelines"] == ["P1"]
    assert merged["impacted_reports"] == ["R1", "R2"]
    assert merged["downstream_tasks"] == ["D1"]
    assert _format_impact_summary(merged) == (
        "Validation results impact:\n"
        "• Data Table: T1\n"
        "• Pipeline: P1\n"
        "• Reports: R1, R2\n"
        "Total Impact:\n"
        "1 Table • 1 Pipeline • 2 Reports")


def test_summary_counts_final_report_collection_without_name_matching():
    impact = {
        "impacted_tables": [],
        "impacted_pipelines": [],
        "impacted_reports": ["A", "B"],
        "downstream_tasks": [],
    }

    assert _format_impact_summary(impact) == (
        "Validation results impact:\n"
        "• Reports: A, B\n"
        "Total Impact:\n"
        "2 Reports")


def test_impact_summary_limits_each_asset_type_to_three_names():
    impact = {
        "impacted_tables": ["T1", "T2", "T3", "T4"],
        "impacted_pipelines": ["P1"],
        "impacted_reports": ["R1", "R2", "R3", "R4"],
    }

    summary = _format_impact_summary(impact)

    assert "T1, T2, T3" in summary
    assert "R1, R2, R3" in summary
    assert "T4" not in summary
    assert "R4" not in summary
    assert summary.endswith("4 Tables • 1 Pipeline • 4 Reports")


def test_impact_summary_falls_back_to_counts_when_assets_have_no_names():
    impact = {
        "impacted_tables": [{}, {}],
        "impacted_pipelines": [],
        "impacted_reports": [{}],
    }

    assert _format_impact_summary(impact) == "2 Tables • 1 Report"


def test_analyze_existing_rca_is_explanation_not_rerun():
    context = {"rca": {
        "pipeline_id": "task-1",
        "summary": "Existing RCA",
        "root_cause_name": "TASK_A",
        "analysis_type": "task",
    }}

    assert _detect_intent("Analyze this RCA", context) == "explain_failure"
    assert _detect_intent("Explain this RCA", context) == "explain_failure"
    assert _detect_intent("Re-run RCA", context) == "run_rca"
