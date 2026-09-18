"""Upstream tracing must reach an explicit source (L1) object or report unresolved."""
import pytest

from app.connectors.lineage_service import LineageService

TASK_KEY = "sf_task_DB.MART.TASK_FACT_SALES"
FACT = "DB.MART.FACT_SALES"
CURATED = "DB.CURATED.DIM_CUSTOMER"
L1 = "DB.L1.RAW_CUSTOMER"

GRAPH = {
    TASK_KEY: {
        "id": TASK_KEY,
        "name": "SF Task: TASK_FACT_SALES",
        "database": "DB",
        "schema": "MART",
        "task_name": "TASK_FACT_SALES",
        "platform": "snowflake",
        "upstream": [],
        "downstream": [],
        "tables": [FACT],
    }
}

RUNS = {
    TASK_KEY: {
        "id": "run-1",
        "name": "SF Task: TASK_FACT_SALES",
        "status": "FAILED",
        "platform": "snowflake",
        "task_key": TASK_KEY,
        "tables": [FACT],
    }
}


@pytest.fixture
def svc(monkeypatch):
    """Offline LineageService with deterministic source-layer rules."""
    monkeypatch.setattr(
        "app.connectors.snowflake_connector.SnowflakeConnector._configured",
        lambda self: False,
    )
    service = LineageService()
    service.lineage_cfg = {
        "source_schemas": ["L1", "RAW"],
        "source_table_prefixes": ["L1_"],
        "source_tag": "DATA_LAYER",
        "source_tag_values": ["L1", "RAW"],
        "max_depth": 8,
        "max_lookups": 50,
    }
    service._graph_cache = GRAPH
    monkeypatch.setattr(
        LineageService, "resolve_task_procedure_io",
        lambda self, db, sch, name, fallback=None: {"inputs": [FACT], "outputs": []},
    )
    return service


def _node(graph, table):
    return next(n for n in graph["nodes"] if n["id"] == f"tbl_{table}")


def test_traversal_continues_through_producers_until_l1(svc, monkeypatch):
    producers = {
        FACT: {
            "kind": "procedure", "id": "procedure_DB.MART.LOAD_FACT_SALES",
            "label": "DB.MART.LOAD_FACT_SALES", "inputs": [CURATED],
            "confidence": "verified",
        },
        CURATED: {
            "kind": "procedure", "id": "procedure_DB.CURATED.LOAD_DIM_CUSTOMER",
            "label": "DB.CURATED.LOAD_DIM_CUSTOMER", "inputs": [L1],
            "confidence": "verified",
        },
    }
    monkeypatch.setattr(
        LineageService, "find_table_producer",
        lambda self, table, graph=None: producers.get(table.upper()),
    )

    graph = svc.build_upstream_lineage_graph(TASK_KEY, TASK_KEY, GRAPH, RUNS)

    # The walk did not stop at FACT_SALES just because no predecessor task exists.
    assert _node(graph, CURATED)["state"] == "healthy"
    l1_node = _node(graph, L1)
    assert l1_node["state"] == "source"
    assert l1_node["layer"] == "L1"

    resolution = graph["resolution"]
    assert resolution["status"] == "complete"
    assert resolution["traced_to_source"] is True
    assert [t["table"] for t in resolution["source_tables"]] == [L1]
    assert resolution["unresolved_tables"] == []


def test_dead_end_is_unresolved_not_assumed_source(svc, monkeypatch):
    monkeypatch.setattr(
        LineageService, "find_table_producer", lambda self, table, graph=None: None)

    graph = svc.build_upstream_lineage_graph(TASK_KEY, TASK_KEY, GRAPH, RUNS)

    node = _node(graph, FACT)
    assert node["state"] == "unresolved"
    assert "no producing task" in node["unresolved_reason"]

    resolution = graph["resolution"]
    assert resolution["status"] == "unresolved"
    assert resolution["traced_to_source"] is False
    assert resolution["source_tables"] == []
    assert resolution["unresolved_tables"][0]["table"] == FACT


def test_depth_limit_reports_unresolved_rather_than_source(svc, monkeypatch):
    def endless_chain(self, table, graph=None):
        level = int(table.rsplit("_", 1)[-1])
        return {
            "kind": "procedure", "id": f"procedure_P{level}", "label": f"P{level}",
            "inputs": [f"DB.MART.STEP_{level + 1}"], "confidence": "verified",
        }

    monkeypatch.setattr(LineageService, "find_table_producer", endless_chain)
    monkeypatch.setattr(
        LineageService, "resolve_task_procedure_io",
        lambda self, db, sch, name, fallback=None: {
            "inputs": ["DB.MART.STEP_0"], "outputs": []},
    )
    svc.lineage_cfg["max_depth"] = 3

    graph = svc.build_upstream_lineage_graph(TASK_KEY, TASK_KEY, GRAPH, RUNS)

    resolution = graph["resolution"]
    assert resolution["truncated"] is True
    assert resolution["status"] == "unresolved"
    assert "depth limit" in resolution["unresolved_tables"][0]["reason"]
    assert all(n["state"] != "source" for n in graph["nodes"])


def test_lookup_budget_exhaustion_is_reported(svc, monkeypatch):
    monkeypatch.setattr(
        LineageService, "find_table_producer",
        lambda self, table, graph=None: {
            "kind": "procedure", "id": "p", "label": "P",
            "inputs": ["DB.MART.OTHER"], "confidence": "verified"},
    )
    svc.lineage_cfg["max_lookups"] = 1

    graph = svc.build_upstream_lineage_graph(TASK_KEY, TASK_KEY, GRAPH, RUNS)

    reasons = [t["reason"] for t in graph["resolution"]["unresolved_tables"]]
    assert any("budget exhausted" in r for r in reasons)
    assert graph["resolution"]["truncated"] is True


def test_classify_table_layer_uses_explicit_rules_only(svc):
    assert svc.classify_table_layer(L1)["signal"] == "schema"
    assert svc.classify_table_layer("DB.STAGING.L1_EVENTS")["signal"] == "name_prefix"
    assert svc.classify_table_layer("DB.MART.FACT_ORDERS")["is_source"] is False


def test_dq_check_graph_traces_chain_inputs_to_l1(svc, monkeypatch):
    monkeypatch.setattr(
        LineageService, "find_table_producer", lambda self, table, graph=None: None)

    upstream, _ = svc.build_dq_check_lineage_graphs(
        [FACT], check_id="QC1", check_label="QC1 check",
        procedure_chain=[{
            "object_name": "DB.MART.LOAD_FACT_SALES",
            "object_type": "procedure",
            "inputs": [L1],
            "outputs": [FACT],
        }],
    )

    # FACT_SALES already has a modelled writer, so it is not a dead end...
    assert _node(upstream, FACT)["state"] == "root_cause"
    assert "unresolved_reason" not in _node(upstream, FACT)
    # ...and the procedure's input is recognised as the source layer.
    assert _node(upstream, L1)["state"] == "source"
    assert upstream["resolution"]["status"] == "complete"


def test_failure_states_survive_the_walk(svc, monkeypatch):
    monkeypatch.setattr(
        LineageService, "find_table_producer", lambda self, table, graph=None: None)

    seeded = {f"tbl_{FACT}": {
        "id": f"tbl_{FACT}", "label": FACT, "type": "table", "state": "root_cause"}}
    graph = svc._walk_upstream(
        table_seeds=[FACT], graph=GRAPH, run_by_key=RUNS, node_by_id=seeded)

    assert _node(graph, FACT)["state"] == "root_cause"
    assert _node(graph, FACT)["unresolved_reason"]
    assert graph["resolution"]["status"] == "unresolved"
