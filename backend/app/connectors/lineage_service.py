"""Task dependency graph + table resolution for RCA."""
from __future__ import annotations

import json
import re
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from app.connectors.dq_connector import DQConnector
from app.connectors.snowflake_connector import SnowflakeConnector
from app.core.config import (get_dq_monitoring_config, get_lineage_config,
                             get_task_monitoring_config, load_settings)

_TABLE_RE = re.compile(
    r"\b([A-Z][A-Z0-9_]*\.[A-Z][A-Z0-9_]*\.[A-Z][A-Z0-9_]+)\b",
    re.IGNORECASE,
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_TASK_PREFIX_RE = re.compile(r"^(?:SF\s+Task:\s*)?(?:TASK_?)?", re.IGNORECASE)

# States that mean "this node is part of the failure" — the upstream walk annotates
# such nodes but never relabels them as healthy/source.
_FAILURE_STATES = frozenset({"root_cause", "failed"})

_BARE_TABLE_RE = re.compile(r"\b(ANLT[A-Z0-9_]+|DIM[A-Z0-9_]+|FACT[A-Z0-9_]+|STG[A-Z0-9_]+|RAW[A-Z0-9_]+|VW_[A-Z0-9_]+|V_[A-Z0-9_]+)\b", re.IGNORECASE)


def task_key(database: str, schema: str, name: str) -> str:
    return f"sf_task_{database}.{schema}.{name}"


def parse_tables_from_text(*parts: Any) -> List[str]:
    found: Set[str] = set()
    for part in parts:
        if not part:
            continue
        text = str(part)
        for match in _TABLE_RE.findall(text):
            found.add(match.upper())
    return sorted(found)


def extract_table_name_from_identifier(name: str) -> Optional[str]:
    """Extract a probable table name from a task/DQ identifier like TASK_ANLT_BASE_FACT_EVNT_SPND."""
    cleaned = _TASK_PREFIX_RE.sub("", name).strip()
    if not cleaned or len(cleaned) < 4:
        return None
    cleaned = cleaned.upper()
    if _IDENTIFIER_RE.match(cleaned):
        return cleaned
    return None


def parse_bare_tables_from_text(*parts: Any) -> List[str]:
    """Extract bare table name patterns (ANLT_, DIM_, FACT_, STG_, RAW_, VW_, V_) from text."""
    found: Set[str] = set()
    for part in parts:
        if not part:
            continue
        text = str(part)
        for match in _BARE_TABLE_RE.findall(text):
            found.add(match.upper())
    return sorted(found)


class LineageService:
    """Load Snowflake task DAG, resolve tables, correlate DQ failures."""

    def __init__(self):
        self.settings = load_settings()
        self.sf = SnowflakeConnector()
        self.lineage_cfg = get_lineage_config(self.settings)
        self._graph_cache: Optional[Dict[str, Dict[str, Any]]] = None
        self._def_cache: Dict[Tuple[str, str, str, str], Optional[str]] = {}
        self._io_cache: Dict[Tuple[str, str, str], Dict[str, List[str]]] = {}
        self._table_meta_cache: Dict[str, Dict[str, Any]] = {}
        self._tag_cache: Dict[Tuple[str, str, str, str], Optional[str]] = {}
        self._layer_cache: Dict[str, Dict[str, Any]] = {}
        self._producer_cache: Dict[str, Optional[Dict[str, Any]]] = {}

    def load_task_graph(self) -> Dict[str, Dict[str, Any]]:
        """task_key -> {name, database, schema, upstream[], downstream[], label}."""
        if self._graph_cache is not None:
            return self._graph_cache

        graph: Dict[str, Dict[str, Any]] = {}
        if not self.sf._configured():
            self._graph_cache = graph
            return graph

        task_cfg = get_task_monitoring_config(self.settings)
        monitor_db = task_cfg["monitor_database"]

        try:
            cur = self.sf._connect().cursor()
            cur.execute(
                """
                SELECT DATABASE_NAME, SCHEMA_NAME, NAME, PREDECESSORS
                FROM SNOWFLAKE.ACCOUNT_USAGE.TASKS
                WHERE DELETED IS NULL
                  AND DATABASE_NAME = %s
                  AND NAME ILIKE %s
                """,
                (monitor_db, task_cfg["name_pattern"]),
            )
            for db, schema, name, predecessors in cur.fetchall():
                key = task_key(db, schema, name)
                graph[key] = {
                    "id": key,
                    "name": f"SF Task: {name}",
                    "database": db,
                    "schema": schema,
                    "task_name": name,
                    "platform": "snowflake",
                    "upstream": [],
                    "downstream": [],
                    "tables": [],
                }
                preds = predecessors
                if isinstance(preds, str):
                    try:
                        preds = json.loads(preds)
                    except json.JSONDecodeError:
                        preds = []
                if preds:
                    for p in preds:
                        if isinstance(p, dict):
                            pdb = p.get("database_name") or p.get("database") or db
                            pschema = p.get("schema_name") or p.get("schema") or schema
                            pname = p.get("name") or p.get("task_name")
                        else:
                            pdb, pschema, pname = db, schema, str(p)
                        if not pname:
                            continue
                        pkey = task_key(pdb, pschema, pname)
                        graph[key]["upstream"].append(pkey)
                        graph.setdefault(pkey, {
                            "id": pkey,
                            "name": f"SF Task: {pname}",
                            "database": pdb,
                            "schema": pschema,
                            "task_name": pname,
                            "platform": "snowflake",
                            "upstream": [],
                            "downstream": [],
                            "tables": [],
                        })
                        graph[pkey]["downstream"].append(key)
        except Exception:  # noqa: BLE001
            pass

        self._graph_cache = graph
        return graph

    def enrich_pipeline(self, record: Dict[str, Any], *, resolve_tables: bool = False) -> Dict[str, Any]:
        """Attach task_key and upstream/downstream. Table resolution is optional (RCA only)."""
        db = record.get("database") or ""
        schema = record.get("schema") or ""
        name = (record.get("name") or "").replace("SF Task: ", "").strip()
        if not name and record.get("id", "").startswith("sf_"):
            parts = record["id"].split("_", 2)
            name = parts[2].rsplit("_", 1)[0] if len(parts) > 2 else ""

        key = task_key(db, schema, name) if db and schema and name else record.get("id", "")
        graph = self.load_task_graph()
        node = graph.get(key, {})
        tables = list(record.get("tables") or [])
        if resolve_tables:
            tables = self.resolve_task_tables(
                record.get("query_id"), record.get("error"), db, schema, task_name=name)
        return {
            **record,
            "task_key": key,
            "upstream": list(node.get("upstream", [])),
            "downstream": list(node.get("downstream", [])),
            "tables": tables,
        }

    def resolve_task_tables(self, query_id: Optional[str], error: Any,
                            database: str, schema: str,
                            task_name: Optional[str] = None) -> List[str]:
        tables = parse_tables_from_text(error)
        if query_id and self.sf._configured():
            try:
                cur = self.sf._connect().cursor()
                cur.execute(
                    """
                    SELECT query_text
                    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                    WHERE query_id = %s
                    LIMIT 1
                    """,
                    (query_id,),
                )
                row = cur.fetchone()
                if row:
                    tables.extend(parse_tables_from_text(row[0]))
            except Exception:  # noqa: BLE001
                pass

        bare_names: List[str] = []
        if task_name:
            extracted = extract_table_name_from_identifier(task_name)
            if extracted:
                bare_names.append(extracted)
        bare_names.extend(parse_bare_tables_from_text(error, task_name))

        if database and schema and bare_names:
            for bn in bare_names:
                fqn = f"{database}.{schema}.{bn}"
                if fqn not in tables:
                    tables.append(fqn)

        return sorted(set(tables))

    def resolve_dq_tables(self, check: Dict[str, Any]) -> List[str]:
        raw = check.get("raw") or {}
        subject_area = check.get("table_name") or raw.get("SUBJECT_AREA")
        tables = parse_tables_from_text(
            check.get("error"),
            raw.get("QC_DESCRIPTION"),
            check.get("name"),
            check.get("column_name"),
            subject_area,
        )

        dq_cfg = get_dq_monitoring_config(self.settings)
        table_fqn = dq_cfg["table_fqn"]
        parts = table_fqn.split(".")
        ctx_db = parts[0] if len(parts) >= 3 else ""
        ctx_schema = parts[1] if len(parts) >= 3 else ""

        bare_names: List[str] = []
        qc_name = check.get("name") or ""
        extracted = extract_table_name_from_identifier(qc_name)
        if extracted:
            bare_names.append(extracted)
        # SUBJECT_AREA is the strongest table hint on a DQ row — treat it as a
        # bare table candidate even if it doesn't match the ANLT_/DIM_/FACT_ prefixes.
        subject_bare = extract_table_name_from_identifier(str(subject_area)) if subject_area else None
        if subject_bare:
            bare_names.append(subject_bare)
        bare_names.extend(parse_bare_tables_from_text(
            check.get("error"), raw.get("QC_DESCRIPTION"), qc_name,
            check.get("column_name"), subject_area,
        ))

        if ctx_db and ctx_schema and bare_names:
            for bn in bare_names:
                fqn = f"{ctx_db}.{ctx_schema}.{bn}"
                if fqn not in tables:
                    tables.append(fqn)

        return sorted(set(tables))

    def failed_dq_checks(self, date_from: Optional[str] = None,
                           date_to: Optional[str] = None) -> List[Dict[str, Any]]:
        # Correlation only needs recorded statuses — do not re-run every failing rule SQL.
        checks = DQConnector().read_results(date_from, date_to, revalidate=False)
        failed = [c for c in checks if c.get("status") in ("FAILED", "WARNING", "DELAYED")]
        for c in failed:
            c["tables"] = self.resolve_dq_tables(c)
        return failed

    def correlate_dq(self, target: Dict[str, Any],
                     dq_failed: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        """DQ failures likely related to a failed task (shared tables / time window)."""
        if dq_failed is None:
            dq_failed = self.failed_dq_checks()
        if not dq_failed:
            return []

        task_tables = set(target.get("tables") or [])
        task_time = _parse_time(target.get("started_at") or target.get("ended_at"))
        related: List[Tuple[int, Dict[str, Any]]] = []

        for check in dq_failed:
            score = 0
            check_tables = set(check.get("tables") or self.resolve_dq_tables(check))
            overlap = task_tables & check_tables
            if overlap:
                score += 10 * len(overlap)
            check_time = _parse_time(check.get("run_at"))
            if task_time and check_time:
                delta_h = abs((task_time - check_time).total_seconds()) / 3600
                if delta_h <= 48:
                    score += max(0, 5 - int(delta_h / 6))
            task_name = (target.get("name") or "").upper()
            qc_id = str(check.get("name") or "").upper()
            if qc_id and qc_id in task_name:
                score += 8
            if score > 0:
                related.append((score, {**check, "match_score": score, "shared_tables": sorted(overlap)}))

        related.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in related[:15]]

    def build_lineage_table(self, target: Dict[str, Any], root_key: str,
                            impacted_keys: List[str],
                            graph: Dict[str, Dict[str, Any]],
                            run_by_key: Dict[str, Dict[str, Any]],
                            dq_related: List[Dict[str, Any]],
                            table_roles: Dict[str, str]) -> List[Dict[str, Any]]:
        """Tabular lineage for UI: tasks, DQ checks, and tables."""
        rows: List[Dict[str, Any]] = []
        seen_nodes: Set[str] = set()

        def add_task_row(tkey: str, role: str) -> None:
            if tkey in seen_nodes:
                return
            seen_nodes.add(tkey)
            meta = graph.get(tkey, {})
            run = run_by_key.get(tkey, {})
            status = run.get("status") or "UNKNOWN"
            if tkey == root_key:
                state = "root_cause"
            elif status in ("FAILED", "DELAYED"):
                state = "failed"
            elif tkey in impacted_keys:
                state = "impacted"
            else:
                state = "healthy"
            ups = ", ".join(graph.get(tkey, {}).get("upstream", [])[:3]) or "—"
            tbls = run.get("tables") or []
            rows.append({
                "kind": "task",
                "id": tkey,
                "name": meta.get("name") or run.get("name") or tkey,
                "status": status,
                "state": state,
                "role": role,
                "upstream": ups,
                "tables": ", ".join(tbls[:5]) if tbls else "—",
                "table_count": len(tbls),
                "error": (run.get("error") or "")[:120] or "—",
            })

        relevant = {target.get("task_key", target["id"]), root_key, *impacted_keys}
        stack = [target.get("task_key", target["id"])]
        while stack:
            cur = stack.pop()
            if cur not in relevant:
                relevant.add(cur)
            for u in graph.get(cur, {}).get("upstream", []):
                if u not in relevant:
                    relevant.add(u)
                    stack.append(u)

        for tkey in relevant:
            if tkey in graph or tkey in run_by_key:
                role = "root_cause" if tkey == root_key else (
                    "target" if tkey == target.get("task_key") else "upstream/downstream")
                add_task_row(tkey, role)

        for check in dq_related:
            cid = check.get("id", "")
            if cid in seen_nodes:
                continue
            seen_nodes.add(cid)
            tbls = check.get("tables") or self.resolve_dq_tables(check)
            for t in tbls:
                table_roles.setdefault(t, "dq_failed")
            rows.append({
                "kind": "dq",
                "id": cid,
                "name": f"DQ: {check.get('name')}",
                "status": check.get("status", "FAILED"),
                "state": "failed",
                "role": "data_quality",
                "upstream": check.get("table_name") or "—",
                "tables": ", ".join(tbls[:5]) if tbls else "—",
                "table_count": len(tbls),
                "error": (check.get("error") or "")[:120] or "—",
            })

        for tbl, role in sorted(table_roles.items()):
            rows.append({
                "kind": "table",
                "id": tbl,
                "name": tbl,
                "status": role.replace("_", " ").upper(),
                "state": "failed" if "fail" in role or role == "root_cause" else "impacted",
                "role": role,
                "upstream": "—",
                "tables": tbl,
                "table_count": 1,
                "error": "—",
            })

        return rows

    # ---- Object definition fetching ----------------------------------------

    def fetch_object_definition(
        self,
        database: str,
        schema: str,
        object_name: str,
        object_type: str = "task",
    ) -> Optional[str]:
        """
        Fetch the SQL definition of a Snowflake object (task, procedure, or view).

        Returns the SQL body as a string (up to 4000 chars), or None if unavailable.
        For tasks that CALL a procedure, returns the procedure body instead.
        """
        cache_key = (database.upper(), schema.upper(), object_name.upper(), object_type.lower())
        if cache_key in self._def_cache:
            return self._def_cache[cache_key]
        if not self.sf._configured() or not database or not schema or not object_name:
            return None
        try:
            cur = self.sf._connect().cursor()
            if object_type == "view":
                if not _IDENTIFIER_RE.match(database):
                    self._def_cache[cache_key] = None
                    return None
                cur.execute(
                    f"SELECT VIEW_DEFINITION FROM {database}.INFORMATION_SCHEMA.VIEWS"
                    f" WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s LIMIT 1",
                    (schema, object_name),
                )
                row = cur.fetchone()
                result = (row[0][:4000] if row and row[0] else None)
                self._def_cache[cache_key] = result
                return result

            if object_type == "procedure":
                if not _IDENTIFIER_RE.match(database):
                    self._def_cache[cache_key] = None
                    return None
                cur.execute(
                    f"SELECT PROCEDURE_DEFINITION FROM {database}.INFORMATION_SCHEMA.PROCEDURES"
                    f" WHERE PROCEDURE_SCHEMA = %s AND PROCEDURE_NAME = %s LIMIT 1",
                    (schema, object_name),
                )
                row = cur.fetchone()
                result = (row[0][:4000] if row and row[0] else None)
                self._def_cache[cache_key] = result
                return result

            # Default: task — fetch DEFINITION, and if it CALLs a proc, fetch that too
            cur.execute(
                """
                SELECT DEFINITION
                FROM SNOWFLAKE.ACCOUNT_USAGE.TASKS
                WHERE DATABASE_NAME = %s AND SCHEMA_NAME = %s AND NAME = %s
                  AND DELETED IS NULL
                LIMIT 1
                """,
                (database, schema, object_name),
            )
            row = cur.fetchone()
            task_body = (row[0] if row else "") or ""
            if not task_body:
                self._def_cache[cache_key] = None
                return None

            proc_match = _CALL_RE.search(task_body)
            if proc_match:
                proc_ref = proc_match.group(1)
                p_parts = proc_ref.split(".")
                p_db = p_parts[0] if len(p_parts) > 2 else database
                p_schema = p_parts[-2] if len(p_parts) > 1 else schema
                p_name = p_parts[-1]
                if _IDENTIFIER_RE.match(p_db):
                    cur.execute(
                        f"SELECT PROCEDURE_DEFINITION FROM {p_db}.INFORMATION_SCHEMA.PROCEDURES"
                        f" WHERE PROCEDURE_SCHEMA = %s AND PROCEDURE_NAME = %s LIMIT 1",
                        (p_schema, p_name),
                    )
                    proc_row = cur.fetchone()
                    if proc_row and proc_row[0]:
                        result = proc_row[0][:4000]
                        self._def_cache[cache_key] = result
                        return result
            result = task_body[:4000]
            self._def_cache[cache_key] = result
            return result
        except Exception:  # noqa: BLE001
            self._def_cache[cache_key] = None
            return None

    # ---- Procedure-driven I/O resolution ---------------------------------

    def resolve_task_procedure_io(
        self,
        database: str,
        schema: str,
        task_name: str,
        fallback_tables: Optional[List[str]] = None,
    ) -> Dict[str, List[str]]:
        """
        Identify input and output tables for a Snowflake task by inspecting its procedure body.

        Algorithm (mirrors RCA skill procedure):
          1. Fetch task body from ACCOUNT_USAGE.TASKS.DEFINITION.
          2. If the body contains a CALL statement, fetch the procedure definition from
             INFORMATION_SCHEMA.PROCEDURES.
          3. Parse SQL: INSERT INTO / MERGE INTO / CREATE ... AS → outputs; FROM / JOIN → inputs.
        Falls back gracefully when not connected or when task has no procedure.
        """
        cache_key = (database.upper(), schema.upper(), task_name.upper())
        if cache_key in self._io_cache:
            cached = self._io_cache[cache_key]
            if not cached["inputs"] and not cached["outputs"] and fallback_tables:
                return {"inputs": list(fallback_tables), "outputs": []}
            return {"inputs": list(cached["inputs"]), "outputs": list(cached["outputs"])}
        if not self.sf._configured() or not database or not schema or not task_name:
            return {"inputs": list(fallback_tables or []), "outputs": []}
        try:
            cur = self.sf._connect().cursor()
            cur.execute(
                """
                SELECT DEFINITION
                FROM SNOWFLAKE.ACCOUNT_USAGE.TASKS
                WHERE DATABASE_NAME = %s AND SCHEMA_NAME = %s AND NAME = %s
                  AND DELETED IS NULL
                LIMIT 1
                """,
                (database, schema, task_name),
            )
            row = cur.fetchone()
            task_body = (row[0] if row else "") or ""

            proc_match = _CALL_RE.search(task_body)
            if proc_match:
                proc_ref = proc_match.group(1)
                p_parts = proc_ref.split(".")
                p_db = p_parts[0] if len(p_parts) > 2 else database
                p_schema = p_parts[-2] if len(p_parts) > 1 else schema
                p_name = p_parts[-1]
                if _IDENTIFIER_RE.match(p_db):
                    cur.execute(
                        f"SELECT PROCEDURE_DEFINITION"
                        f" FROM {p_db}.INFORMATION_SCHEMA.PROCEDURES"
                        f" WHERE PROCEDURE_SCHEMA = %s AND PROCEDURE_NAME = %s LIMIT 1",
                        (p_schema, p_name),
                    )
                    proc_row = cur.fetchone()
                    sql_body = (proc_row[0] if proc_row else "") or task_body
                else:
                    sql_body = task_body
            else:
                sql_body = task_body

            result = _classify_sql_io(sql_body, database, schema)
            self._io_cache[cache_key] = {
                "inputs": list(result["inputs"]),
                "outputs": list(result["outputs"]),
            }
            if not result["inputs"] and not result["outputs"]:
                return {"inputs": list(fallback_tables or []), "outputs": []}
            return result
        except Exception:  # noqa: BLE001
            self._io_cache[cache_key] = {"inputs": [], "outputs": []}
            return {"inputs": list(fallback_tables or []), "outputs": []}

    def fetch_upstream_procedure_chain(
        self,
        database: str,
        schema: str,
        task_name: str,
        depth: int = 3,
    ) -> List[Dict[str, Any]]:
        """Recursively fetch upstream procedure definitions to trace the data transformation chain.

        For a given task, fetches its procedure body, identifies input tables,
        then for each input table finds the task/procedure that produces it and
        repeats up to `depth` levels.

        Returns a list of {level, object_name, object_type, sql_body, inputs, outputs}.
        """
        if not self.sf._configured() or not database or not schema or not task_name:
            return []

        chain: List[Dict[str, Any]] = []
        visited: Set[str] = set()
        queue: List[tuple] = [(database, schema, task_name, "task", 0)]

        while queue:
            db, sch, name, obj_type, level = queue.pop(0)
            if level >= depth:
                continue
            obj_key = f"{db}.{sch}.{name}".upper()
            if obj_key in visited:
                continue
            visited.add(obj_key)

            sql_body = self.fetch_object_definition(db, sch, name, obj_type)
            if not sql_body:
                continue

            io = _classify_sql_io(sql_body, db, sch)
            chain.append({
                "level": level,
                "object_name": f"{db}.{sch}.{name}",
                "object_type": obj_type,
                "sql_body": sql_body[:3000],
                "inputs": io["inputs"],
                "outputs": io["outputs"],
            })

            if level + 1 >= depth:
                continue
            graph = self._graph_cache or {}
            for input_table in io["inputs"][:10]:
                parts = input_table.split(".")
                if len(parts) < 3:
                    continue
                t_db, t_schema, t_name = parts[0], parts[1], parts[2]
                for tkey, meta in graph.items():
                    g_name = (meta.get("task_name") or "").upper()
                    g_db = (meta.get("database") or "").upper()
                    g_schema = (meta.get("schema") or "").upper()
                    table_from_task = extract_table_name_from_identifier(g_name)
                    if (g_db == t_db.upper() and g_schema == t_schema.upper()
                            and table_from_task and table_from_task == t_name.upper()):
                        queue.append((g_db, g_schema, g_name, "task", level + 1))
                        break
                else:
                    view_def = self.fetch_object_definition(t_db, t_schema, t_name, "view")
                    if view_def:
                        chain.append({
                            "level": level + 1,
                            "object_name": input_table,
                            "object_type": "view",
                            "sql_body": view_def[:3000],
                            "inputs": _classify_sql_io(view_def, t_db, t_schema)["inputs"],
                            "outputs": [input_table],
                        })
                        visited.add(input_table.upper())

        return chain

    # ---- Source-layer (L1) detection --------------------------------------

    def fetch_table_metadata(self, database: str, schema: str,
                             name: str) -> Dict[str, Any]:
        """Ingestion-relevant metadata for one table (type, comment, Snowpipe target)."""
        key = f"{database}.{schema}.{name}".upper()
        cached = self._table_meta_cache.get(key)
        if cached is not None:
            return cached

        meta: Dict[str, Any] = {
            "exists": False, "table_type": "", "comment": "", "is_pipe_target": False,
        }
        if not self.sf._configured() or not _IDENTIFIER_RE.match(database or ""):
            self._table_meta_cache[key] = meta
            return meta
        try:
            cur = self.sf._connect().cursor()
            cur.execute(
                f"SELECT TABLE_TYPE, COMMENT FROM {database}.INFORMATION_SCHEMA.TABLES"
                f" WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s LIMIT 1",
                (schema, name),
            )
            row = cur.fetchone()
            if row:
                meta["exists"] = True
                meta["table_type"] = str(row[0] or "").upper()
                meta["comment"] = str(row[1] or "")
            if meta["table_type"] != "EXTERNAL TABLE":
                cur.execute(
                    f"SELECT COUNT(*) FROM {database}.INFORMATION_SCHEMA.PIPES"
                    f" WHERE DEFINITION ILIKE %s",
                    (f"%{name}%",),
                )
                pipe_row = cur.fetchone()
                meta["is_pipe_target"] = bool(pipe_row and pipe_row[0])
        except Exception:  # noqa: BLE001
            pass
        self._table_meta_cache[key] = meta
        return meta

    def fetch_object_tag(self, database: str, schema: str, name: str,
                         tag_name: str) -> Optional[str]:
        """Read a governance tag on the table, falling back to its schema."""
        if not tag_name:
            return None
        key = (database.upper(), schema.upper(), name.upper(), tag_name.upper())
        if key in self._tag_cache:
            return self._tag_cache[key]
        value: Optional[str] = None
        if self.sf._configured() and database and schema and name:
            try:
                cur = self.sf._connect().cursor()
                cur.execute(
                    """
                    SELECT TAG_VALUE
                    FROM SNOWFLAKE.ACCOUNT_USAGE.TAG_REFERENCES
                    WHERE UPPER(TAG_NAME) = %s
                      AND UPPER(OBJECT_DATABASE) = %s
                      AND (
                        (DOMAIN = 'TABLE' AND UPPER(OBJECT_SCHEMA) = %s
                         AND UPPER(OBJECT_NAME) = %s)
                        OR (DOMAIN = 'SCHEMA' AND UPPER(OBJECT_NAME) = %s)
                      )
                    ORDER BY CASE WHEN DOMAIN = 'TABLE' THEN 0 ELSE 1 END
                    LIMIT 1
                    """,
                    (tag_name.upper(), database.upper(), schema.upper(),
                     name.upper(), schema.upper()),
                )
                row = cur.fetchone()
                if row and row[0]:
                    value = str(row[0])
            except Exception:  # noqa: BLE001
                value = None
        self._tag_cache[key] = value
        return value

    def classify_table_layer(self, table_fqn: str,
                             allow_queries: bool = True) -> Dict[str, Any]:
        """Decide whether a table is an explicitly marked source/ingestion object.

        Only explicit evidence terminates upstream tracing. ``is_source`` False means
        "keep tracing / report unresolved", never "assume this is the source".
        Signals are checked cheapest-first; with ``allow_queries`` False only the
        configured schema and prefix rules apply, so a negative answer is not cached.
        """
        key = str(table_fqn or "").upper()
        cached = self._layer_cache.get(key)
        if cached is not None:
            return dict(cached)

        cfg = self.lineage_cfg
        parts = key.split(".")
        db = parts[0] if len(parts) == 3 else ""
        schema = parts[1] if len(parts) == 3 else ""
        name = parts[-1] if parts else ""
        verdict = {"is_source": False, "layer": None, "signal": "", "reason": ""}

        if schema and schema in cfg["source_schemas"]:
            verdict = {
                "is_source": True, "layer": schema, "signal": "schema",
                "reason": f"table lives in configured source schema {schema}",
            }
        elif allow_queries and db and schema:
            tag_value = self.fetch_object_tag(db, schema, name, cfg["source_tag"])
            meta = self.fetch_table_metadata(db, schema, name)
            if tag_value and tag_value.strip().upper() in cfg["source_tag_values"]:
                verdict = {
                    "is_source": True, "layer": tag_value.strip().upper(),
                    "signal": "tag",
                    "reason": f"tagged {cfg['source_tag']}={tag_value.strip().upper()}",
                }
            elif meta.get("table_type") == "EXTERNAL TABLE":
                verdict = {
                    "is_source": True, "layer": "EXTERNAL", "signal": "external_table",
                    "reason": "declared as an external table",
                }
            elif meta.get("is_pipe_target"):
                verdict = {
                    "is_source": True, "layer": "INGESTION", "signal": "snowpipe",
                    "reason": "loaded by a Snowpipe definition",
                }

        if not verdict["is_source"]:
            prefix = next(
                (p for p in cfg["source_table_prefixes"] if name.startswith(p)), None)
            if prefix:
                verdict = {
                    "is_source": True, "layer": prefix.rstrip("_"),
                    "signal": "name_prefix",
                    "reason": f"name matches configured source prefix {prefix}",
                }

        if verdict["is_source"] or allow_queries:
            self._layer_cache[key] = dict(verdict)
        return verdict

    # ---- Producer resolution ----------------------------------------------

    def find_table_producer(
        self,
        table_fqn: str,
        graph: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Find the task, view, or procedure that writes ``table_fqn``.

        Returns None only when nothing in Snowflake claims to produce the table —
        callers must treat that as unresolved lineage, not as the source layer.
        """
        key = str(table_fqn or "").upper()
        if key in self._producer_cache:
            cached = self._producer_cache[key]
            return dict(cached) if cached else None

        parts = key.split(".")
        if len(parts) != 3:
            self._producer_cache[key] = None
            return None
        db, schema, name = parts
        graph = graph if graph is not None else self.load_task_graph()
        producer: Optional[Dict[str, Any]] = None

        # 1. A monitored task whose procedure writes the table.
        exact, fuzzy = [], []
        for tkey, meta in graph.items():
            if (meta.get("database") or "").upper() != db:
                continue
            derived = extract_table_name_from_identifier(
                (meta.get("task_name") or "").upper()) or ""
            if not derived:
                continue
            if derived == name:
                exact.append((tkey, meta))
            elif derived in name or name in derived:
                fuzzy.append((tkey, meta))
        for tkey, meta in (exact + fuzzy)[:3]:
            io = self.resolve_task_procedure_io(
                meta.get("database") or db, meta.get("schema") or schema,
                meta.get("task_name") or "")
            outputs = {str(o).upper() for o in io.get("outputs") or []}
            writes = key in outputs or any(o.split(".")[-1] == name for o in outputs)
            if writes or not outputs:
                producer = {
                    "kind": "task",
                    "id": tkey,
                    "label": meta.get("name") or tkey,
                    "database": meta.get("database") or db,
                    "schema": meta.get("schema") or schema,
                    "name": meta.get("task_name") or "",
                    "inputs": list(io.get("inputs") or []),
                    "confidence": "verified" if writes else "heuristic",
                }
                break

        # 2. The object is itself a view — its SELECT is the producing logic.
        if producer is None:
            view_def = self.fetch_object_definition(db, schema, name, "view")
            if view_def:
                io = _classify_sql_io(view_def, db, schema)
                producer = {
                    "kind": "view",
                    "id": f"view_{key}",
                    "label": table_fqn,
                    "database": db, "schema": schema, "name": name,
                    "inputs": list(io.get("inputs") or []),
                    "confidence": "verified",
                    "self_object": True,
                }

        # 3. A stored procedure that writes the table.
        if producer is None:
            for item in self.discover_downstream_procedures(db, name)[:5]:
                body = self.fetch_object_definition(
                    item.get("database") or db, item.get("schema") or "",
                    item.get("name") or "", "procedure")
                if not body:
                    continue
                io = _classify_sql_io(
                    body, item.get("database") or db, item.get("schema") or "")
                outputs = {str(o).upper() for o in io.get("outputs") or []}
                if key in outputs or any(o.split(".")[-1] == name for o in outputs):
                    producer = {
                        "kind": "procedure",
                        "id": f"procedure_{item['fqn']}",
                        "label": item["fqn"],
                        "database": item.get("database") or db,
                        "schema": item.get("schema") or "",
                        "name": item.get("name") or "",
                        "inputs": list(io.get("inputs") or []),
                        "confidence": "verified",
                    }
                    break

        self._producer_cache[key] = dict(producer) if producer else None
        return producer

    # ---- Upstream lineage graph -------------------------------------------

    def build_upstream_lineage_graph(
        self,
        target_key: str,
        root_key: str,
        graph: Dict[str, Dict[str, Any]],
        run_by_key: Dict[str, Dict[str, Any]],
        resolve_io_keys: Optional[Set[str]] = None,
        max_depth: Optional[int] = None,
        max_lookups: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Build upstream lineage from the failed task back to the source (L1) layer.

        The walk follows task predecessors and, when a table has no predecessor task,
        keeps resolving whichever task, view, or procedure writes it. It stops at a
        table only when ``classify_table_layer`` confirms a source/ingestion object;
        every other dead end is reported as unresolved in ``result["resolution"]``.
        The failed/root task appears on the rightmost side.
        """
        return self._walk_upstream(
            task_seeds=[target_key],
            table_seeds=[],
            graph=graph,
            run_by_key=run_by_key,
            root_key=root_key,
            resolve_io_keys=resolve_io_keys,
            max_depth=max_depth,
            max_lookups=max_lookups,
        )

    def _walk_upstream(
        self,
        *,
        task_seeds: Optional[List[str]] = None,
        table_seeds: Optional[List[str]] = None,
        graph: Optional[Dict[str, Dict[str, Any]]] = None,
        run_by_key: Optional[Dict[str, Dict[str, Any]]] = None,
        root_key: str = "",
        resolve_io_keys: Optional[Set[str]] = None,
        max_depth: Optional[int] = None,
        max_lookups: Optional[int] = None,
        node_by_id: Optional[Dict[str, Dict[str, Any]]] = None,
        edges: Optional[List[Dict[str, str]]] = None,
        edge_seen: Optional[Set[Tuple[str, str]]] = None,
    ) -> Dict[str, Any]:
        """Breadth-first upstream walk over tasks and tables, terminating at L1.

        Existing ``node_by_id`` / ``edges`` containers can be passed in so a caller
        can seed the graph with its own nodes and have the walk extend them.
        """
        cfg = self.lineage_cfg
        graph = graph if graph is not None else self.load_task_graph()
        run_by_key = run_by_key or {}
        depth_limit = int(max_depth if max_depth is not None else cfg["max_depth"])
        remaining = [int(max_lookups if max_lookups is not None else cfg["max_lookups"])]

        nodes: Dict[str, Dict[str, Any]] = node_by_id if node_by_id is not None else {}
        edge_list: List[Dict[str, str]] = edges if edges is not None else []
        seen_edges: Set[Tuple[str, str]] = (
            edge_seen if edge_seen is not None
            else {(e["from"], e["to"]) for e in edge_list}
        )

        resolution: Dict[str, Any] = {
            "status": "complete",
            "traced_to_source": False,
            "depth_limit": depth_limit,
            "deepest_level": 0,
            "truncated": False,
            "producer_lookups": 0,
            "source_tables": [],
            "unresolved_tables": [],
        }

        def spend() -> bool:
            if remaining[0] <= 0:
                return False
            remaining[0] -= 1
            resolution["producer_lookups"] += 1
            return True

        def put_node(nid: str, **kw: Any) -> Dict[str, Any]:
            node = nodes.get(nid)
            if node is None:
                node = {"id": nid}
                nodes[nid] = node
            for key, value in kw.items():
                node.setdefault(key, value)
            return node

        def mark_node(nid: str, **kw: Any) -> None:
            put_node(nid).update(kw)

        def set_state(nid: str, state: str) -> None:
            """Never downgrade a failure state that a caller already established."""
            node = put_node(nid)
            if node.get("state") in _FAILURE_STATES and state not in _FAILURE_STATES:
                return
            node["state"] = state

        producers_of: Dict[str, Set[str]] = {}
        for edge in edge_list:
            producers_of.setdefault(edge["to"], set()).add(edge["from"])

        def add_edge(from_id: str, to_id: str) -> None:
            pair = (from_id, to_id)
            if from_id == to_id or pair in seen_edges:
                return
            seen_edges.add(pair)
            edge_list.append({"from": from_id, "to": to_id})
            producers_of.setdefault(to_id, set()).add(from_id)

        def has_modelled_producer(nid: str) -> bool:
            """True when something other than a table already writes into this node."""
            return any(
                nodes.get(src, {}).get("type", "table") != "table"
                for src in producers_of.get(nid, ())
            )

        def task_state(tkey: str) -> str:
            if tkey == root_key:
                return "root_cause"
            if run_by_key.get(tkey, {}).get("status") in ("FAILED", "DELAYED"):
                return "failed"
            return "healthy"

        def unresolved(table: str, reason: str) -> None:
            mark_node(f"tbl_{table}", unresolved_reason=reason)
            set_state(f"tbl_{table}", "unresolved")
            resolution["unresolved_tables"].append({"table": table, "reason": reason})

        visited_tasks: Set[str] = set()
        visited_tables: Set[str] = set()
        queue: deque = deque(
            [("task", key, 0) for key in (task_seeds or [])]
            + [("table", tbl, 0) for tbl in (table_seeds or [])]
        )

        while queue:
            kind, ident, depth = queue.popleft()
            resolution["deepest_level"] = max(resolution["deepest_level"], depth)

            if kind == "task":
                if ident in visited_tasks:
                    continue
                visited_tasks.add(ident)
                meta = graph.get(ident, {})
                run = run_by_key.get(ident, {})
                put_node(
                    ident,
                    label=run.get("name") or meta.get("name") or ident,
                    type="task",
                    platform=run.get("platform") or meta.get("platform") or "snowflake",
                )
                set_state(ident, task_state(ident))
                if depth >= depth_limit:
                    resolution["truncated"] = True
                    continue

                fallback = list(run.get("tables") or meta.get("tables") or [])
                db = meta.get("database") or run.get("database") or ""
                sch = meta.get("schema") or run.get("schema") or ""
                tname = meta.get("task_name") or ""
                forced = resolve_io_keys is None or ident in resolve_io_keys
                if db and sch and tname and (forced or spend()):
                    io = self.resolve_task_procedure_io(db, sch, tname, fallback)
                    input_tables = (
                        io["inputs"] if (io["inputs"] or io["outputs"]) else fallback)
                else:
                    input_tables = fallback

                upstream_keys = list(meta.get("upstream", []))
                producing_task: Dict[str, str] = {}
                for up_key in upstream_keys:
                    up_run = run_by_key.get(up_key, {})
                    up_meta = graph.get(up_key, {})
                    for tbl in (up_run.get("tables") or up_meta.get("tables") or []):
                        producing_task.setdefault(tbl, up_key)

                for tbl in input_tables:
                    tbl_id = f"tbl_{tbl}"
                    put_node(tbl_id, label=tbl, name=tbl, type="table", state="healthy")
                    add_edge(tbl_id, ident)
                    prod = producing_task.get(tbl)
                    if prod:
                        set_state(tbl_id, task_state(prod))
                        add_edge(prod, tbl_id)
                        queue.append(("task", prod, depth + 1))
                    else:
                        queue.append(("table", tbl, depth + 1))

                for up_key in upstream_keys:
                    queue.append(("task", up_key, depth + 1))
                continue

            table = str(ident)
            table_upper = table.upper()
            if table_upper in visited_tables:
                continue
            visited_tables.add(table_upper)
            tbl_id = f"tbl_{table}"
            put_node(tbl_id, label=table, name=table, type="table", state="healthy")

            # Free rules first; only pay for tag / ingestion metadata if they miss.
            verdict = self.classify_table_layer(table, allow_queries=False)
            if not verdict["is_source"] and spend():
                verdict = self.classify_table_layer(table, allow_queries=True)
            if verdict["is_source"]:
                mark_node(
                    tbl_id,
                    layer=verdict["layer"] or "L1",
                    source_signal=verdict["signal"],
                    source_reason=verdict["reason"],
                )
                set_state(tbl_id, "source")
                resolution["source_tables"].append({"table": table, **verdict})
                continue

            if has_modelled_producer(tbl_id):
                # A caller (or an earlier hop) already attached the writing object;
                # its own inputs are traced separately.
                continue

            if depth >= depth_limit:
                resolution["truncated"] = True
                unresolved(
                    table,
                    f"traversal stopped at depth limit {depth_limit} before reaching "
                    "a source-layer object",
                )
                continue

            if not spend():
                resolution["truncated"] = True
                unresolved(
                    table,
                    "lineage lookup budget exhausted before a producer could be resolved",
                )
                continue

            producer = self.find_table_producer(table, graph)
            if producer is None:
                unresolved(
                    table,
                    "no producing task, view, or procedure found and no source-layer "
                    "rule matched",
                )
                continue

            if producer.get("self_object"):
                # The object is itself a view; its SELECT is the producing logic.
                mark_node(tbl_id, type="view", producer_kind="view")
                producer_id = tbl_id
            else:
                producer_id = producer["id"]
                put_node(
                    producer_id,
                    label=producer["label"],
                    name=producer["label"],
                    type=producer["kind"],
                    state="healthy",
                    platform="snowflake",
                )
                add_edge(producer_id, tbl_id)

            if producer["kind"] == "task":
                queue.append(("task", producer["id"], depth + 1))
                continue

            inputs = [
                i for i in (producer.get("inputs") or [])
                if str(i).upper() != table_upper
            ][:8]
            if not inputs:
                unresolved(
                    table,
                    f"producer {producer['label']} has no resolvable input tables",
                )
                continue
            for inp in inputs:
                inp_id = f"tbl_{inp}"
                put_node(inp_id, label=inp, name=inp, type="table", state="healthy")
                add_edge(inp_id, producer_id)
                queue.append(("table", inp, depth + 1))

        if resolution["unresolved_tables"]:
            resolution["status"] = (
                "partial" if resolution["source_tables"] else "unresolved")
        resolution["traced_to_source"] = bool(
            resolution["source_tables"]) and not resolution["unresolved_tables"]
        return {
            "nodes": list(nodes.values()),
            "edges": edge_list,
            "resolution": resolution,
        }

    def build_dq_check_lineage_graphs(
        self,
        tables: List[str],
        check_id: str,
        check_label: str,
        downstream_map: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        procedure_chain: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Build upstream/downstream graphs for a DQ check without a correlated failed task.

        Upstream: writer procedures/tasks (and their inputs) → DQ tables → DQ check.
        Downstream: DQ tables → views/procedures from INFORMATION_SCHEMA discovery.
        """
        unique_tables = [t for t in list(dict.fromkeys(tables or [])) if t][:8]
        check_node_id = check_id or "dq_check"
        check_node_label = check_label or check_id or "DQ check"

        def _empty() -> Dict[str, Any]:
            return {"nodes": [], "edges": []}

        # ── Upstream ────────────────────────────────────────────────────────
        up_by_id: Dict[str, Dict[str, Any]] = {}
        up_edges: List[Dict[str, Any]] = []
        up_edge_seen: Set[Tuple[str, str]] = set()

        def up_add(nid: str, **kw: Any) -> None:
            if nid not in up_by_id:
                # TableLineageGraph reads label || name
                if "label" not in kw and "name" in kw:
                    kw["label"] = kw["name"]
                up_by_id[nid] = {"id": nid, **kw}

        def up_edge(a: str, b: str) -> None:
            pair = (a, b)
            if pair not in up_edge_seen and a != b:
                up_edge_seen.add(pair)
                up_edges.append({"from": a, "to": b})

        if unique_tables or check_id:
            up_add(
                check_node_id,
                label=f"DQ: {check_node_label}",
                name=f"DQ: {check_node_label}",
                type="task",
                state="failed",
                platform="dq",
            )

        for i, tbl in enumerate(unique_tables):
            tbl_id = f"tbl_{tbl}"
            up_add(
                tbl_id,
                label=tbl,
                name=tbl,
                type="table",
                state="root_cause" if i == 0 else "failed",
            )
            up_edge(tbl_id, check_node_id)

        for proc in (procedure_chain or [])[:5]:
            obj_name = proc.get("object_name") or ""
            if not obj_name:
                continue
            obj_type = proc.get("object_type") or "procedure"
            # Match the node ids the upstream walk produces so both agree on one node.
            proc_id = (
                task_key(*obj_name.split(".")) if obj_type == "task"
                and len(obj_name.split(".")) == 3 else f"{obj_type}_{obj_name}"
            )
            up_add(
                proc_id,
                label=obj_name,
                name=obj_name.split(".")[-1] if "." in obj_name else obj_name,
                type="procedure" if obj_type != "task" else "task",
                state="healthy",
                platform="snowflake",
            )
            outputs = [str(o).upper() for o in (proc.get("outputs") or [])]
            linked = False
            for tbl in unique_tables:
                bare = tbl.split(".")[-1].upper()
                tbl_u = tbl.upper()
                if any(o == tbl_u or o.endswith(f".{bare}") or o.split(".")[-1] == bare for o in outputs) or not outputs:
                    up_edge(proc_id, f"tbl_{tbl}")
                    linked = True
                    if outputs:
                        break
            if not linked and unique_tables:
                up_edge(proc_id, f"tbl_{unique_tables[0]}")
            for inp in (proc.get("inputs") or [])[:6]:
                inp_s = str(inp)
                if not inp_s:
                    continue
                if inp_s.upper() in {t.upper() for t in unique_tables}:
                    continue
                src_id = f"tbl_{inp_s}"
                up_add(src_id, label=inp_s, name=inp_s, type="table")
                up_edge(src_id, proc_id)

        # Keep tracing every table in the graph back to an explicit source-layer
        # object; whatever cannot be traced is reported as unresolved.
        seeds = list(dict.fromkeys(
            unique_tables
            + [
                str(n.get("name") or n.get("label") or "")
                for n in up_by_id.values() if n.get("type") == "table"
            ]
        ))
        walked = self._walk_upstream(
            table_seeds=[s for s in seeds if s],
            node_by_id=up_by_id,
            edges=up_edges,
            edge_seen=up_edge_seen,
        )
        upstream = (
            {"nodes": walked["nodes"], "edges": walked["edges"],
             "resolution": walked["resolution"]}
            if walked["nodes"] else _empty()
        )

        # ── Downstream ──────────────────────────────────────────────────────
        dn_nodes: List[Dict[str, Any]] = []
        dn_edges: List[Dict[str, Any]] = []
        dn_seen: Set[str] = set()
        dn_edge_seen: Set[Tuple[str, str]] = set()

        def dn_add(nid: str, **kw: Any) -> None:
            if nid not in dn_seen:
                dn_seen.add(nid)
                if "label" not in kw and "name" in kw:
                    kw["label"] = kw["name"]
                dn_nodes.append({"id": nid, **kw})

        def dn_edge(a: str, b: str) -> None:
            pair = (a, b)
            if pair not in dn_edge_seen and a != b:
                dn_edge_seen.add(pair)
                dn_edges.append({"from": a, "to": b})

        dmap = downstream_map or {}
        for i, tbl in enumerate(unique_tables):
            tbl_id = f"tbl_{tbl}"
            dn_add(
                tbl_id,
                label=tbl,
                name=tbl,
                type="table",
                state="root_cause" if i == 0 else "failed",
            )
            consumers = dmap.get(tbl) or []
            # Also match by bare / case-insensitive key
            if not consumers:
                bare = tbl.split(".")[-1].upper()
                for k, items in dmap.items():
                    if str(k).upper() == tbl.upper() or str(k).split(".")[-1].upper() == bare:
                        consumers = items
                        break
            for item in (consumers or [])[:10]:
                fqn = item.get("fqn") or item.get("name") or ""
                if not fqn:
                    continue
                ntype = item.get("type") or "view"
                node_id = f"{ntype}_{fqn}"
                dn_add(
                    node_id,
                    label=item.get("name") or fqn,
                    name=item.get("name") or fqn,
                    type=ntype,
                    state="impacted",
                )
                dn_edge(tbl_id, node_id)

        # If discovery found consumers under keys not in unique_tables, still show them.
        if not dn_edges and dmap:
            for parent_tbl, consumers in dmap.items():
                parent_id = f"tbl_{parent_tbl}"
                dn_add(parent_id, label=parent_tbl, name=parent_tbl, type="table", state="failed")
                for item in (consumers or [])[:10]:
                    fqn = item.get("fqn") or item.get("name") or ""
                    if not fqn:
                        continue
                    ntype = item.get("type") or "view"
                    node_id = f"{ntype}_{fqn}"
                    dn_add(
                        node_id,
                        label=item.get("name") or fqn,
                        name=item.get("name") or fqn,
                        type=ntype,
                        state="impacted",
                    )
                    dn_edge(parent_id, node_id)

        downstream = {"nodes": dn_nodes, "edges": dn_edges} if dn_nodes else _empty()
        return upstream, downstream

    # ---- Downstream lineage graph ----------------------------------------

    def build_downstream_lineage_graph(
        self,
        target_key: str,
        root_key: str,
        impacted_keys: List[str],
        graph: Dict[str, Dict[str, Any]],
        run_by_key: Dict[str, Dict[str, Any]],
        downstream_map: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        resolve_io_keys: Optional[Set[str]] = None,
    ) -> Dict[str, Any]:
        """
        Build downstream lineage: failed task → downstream procedures/tasks → final sinks.

        For each task in the forward walk:
          - Resolve procedure definition → identify output tables.
          - Connect output tables to the downstream tasks/views/procedures that consume them.
        Views and procedures discovered via INFORMATION_SCHEMA are added as terminal sink nodes.
        The failed/root task appears on the leftmost side.
        """
        nodes: List[Dict[str, Any]] = []
        edges: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()
        visited_tasks: Set[str] = set()
        impacted_set = set(impacted_keys)

        def add_node(nid: str, **kw: Any) -> None:
            if nid not in seen_ids:
                seen_ids.add(nid)
                nodes.append({"id": nid, **kw})

        def add_edge(from_id: str, to_id: str) -> None:
            pair = (from_id, to_id)
            if pair not in _seen_edges:
                _seen_edges.add(pair)
                edges.append({"from": from_id, "to": to_id})

        _seen_edges: Set[Tuple[str, str]] = set()

        def task_state(tkey: str) -> str:
            if tkey == root_key or tkey == target_key:
                return "root_cause"
            run = run_by_key.get(tkey, {})
            if run.get("status") in ("FAILED", "DELAYED"):
                return "failed"
            if tkey in impacted_set:
                return "impacted"
            return "impacted"

        queue = [target_key]
        while queue:
            tkey = queue.pop(0)
            if tkey in visited_tasks:
                continue
            visited_tasks.add(tkey)

            meta = graph.get(tkey, {})
            run = run_by_key.get(tkey, {})
            label = run.get("name") or meta.get("name") or tkey
            platform = run.get("platform") or meta.get("platform") or "snowflake"
            state = task_state(tkey)
            add_node(tkey, label=label, type="task", state=state, platform=platform)

            fallback = list(run.get("tables") or meta.get("tables") or [])
            db = meta.get("database") or run.get("database") or ""
            sch = meta.get("schema") or run.get("schema") or ""
            tname = meta.get("task_name") or ""
            should_resolve = resolve_io_keys is None or tkey in resolve_io_keys
            if db and sch and tname and should_resolve:
                io = self.resolve_task_procedure_io(db, sch, tname, fallback)
                output_tables = io["outputs"] if io["outputs"] else fallback
            else:
                output_tables = fallback

            downstream_keys = list(meta.get("downstream", []))

            # Build: table → downstream_task_that_consumes_it
            consuming_task: Dict[str, str] = {}
            for dn_key in downstream_keys:
                dn_run = run_by_key.get(dn_key, {})
                dn_meta = graph.get(dn_key, {})
                for tbl in (dn_run.get("tables") or dn_meta.get("tables") or []):
                    consuming_task.setdefault(tbl, dn_key)

            for tbl in output_tables:
                tbl_id = f"tbl_{tbl}"
                add_node(tbl_id, label=tbl, type="table", state="impacted")
                add_edge(tkey, tbl_id)          # current task → output table
                consumer = consuming_task.get(tbl)
                if consumer:
                    add_edge(tbl_id, consumer)  # output table → downstream task

            for dn_key in downstream_keys:
                if dn_key not in visited_tasks:
                    queue.append(dn_key)

        # Append views/procedures from INFORMATION_SCHEMA downstream discovery
        if downstream_map:
            for parent_tbl, consumers in downstream_map.items():
                parent_id = f"tbl_{parent_tbl}"
                add_node(parent_id, label=parent_tbl, type="table", state="impacted")
                for item in consumers[:10]:
                    node_id = f"{item['type']}_{item['fqn']}"
                    add_node(node_id, label=item["name"], type=item["type"], state="impacted")
                    add_edge(parent_id, node_id)

        return {"nodes": nodes, "edges": edges}

    def resolve_writer_procedures_for_tables(
        self,
        tables: List[str],
        context_database: str = "",
        max_procs: int = 3,
    ) -> List[Dict[str, Any]]:
        """Find procedures that write/reference failing tables when no task correlation exists.

        Searches INFORMATION_SCHEMA.PROCEDURES for definitions mentioning the bare table
        name, fetches DDL, and prefers procedures whose outputs include the target table.
        Returns procedure_chain-compatible entries: object_name, object_type, sql_body,
        inputs, outputs, level.
        """
        if not self.sf._configured() or not tables:
            return []

        chain: List[Dict[str, Any]] = []
        seen: Set[str] = set()

        for table_fqn in list(dict.fromkeys(tables or []))[:5]:
            parts = str(table_fqn).split(".")
            if len(parts) == 3:
                db, _sch, tbl = parts[0], parts[1], parts[2]
            elif len(parts) == 2:
                db, tbl = context_database or parts[0], parts[-1]
            else:
                db, tbl = context_database, parts[0]
            if not db or not tbl or not _IDENTIFIER_RE.match(db):
                continue

            candidates = self.discover_downstream_procedures(db, tbl)
            # Also search task graph for tasks named after the table (common ETL pattern).
            graph = self._graph_cache or self.load_task_graph()
            bare = tbl.upper()
            for tkey, meta in graph.items():
                g_name = (meta.get("task_name") or "").upper()
                g_db = (meta.get("database") or "").upper()
                extracted = extract_table_name_from_identifier(g_name) or ""
                if g_db == db.upper() and extracted and (
                    extracted == bare or bare in extracted or extracted in bare
                ):
                    candidates.append({
                        "type": "task",
                        "database": meta.get("database") or db,
                        "schema": meta.get("schema") or "",
                        "name": meta.get("task_name") or "",
                        "fqn": f"{meta.get('database')}.{meta.get('schema')}.{meta.get('task_name')}",
                    })

            scored: List[Tuple[int, Dict[str, Any]]] = []
            for item in candidates:
                fqn = (item.get("fqn") or "").upper()
                if not fqn or fqn in seen:
                    continue
                obj_type = item.get("type") or "procedure"
                sql_body = self.fetch_object_definition(
                    item.get("database") or db,
                    item.get("schema") or "",
                    item.get("name") or "",
                    "task" if obj_type == "task" else "procedure",
                )
                if not sql_body:
                    continue
                io = _classify_sql_io(
                    sql_body, item.get("database") or db, item.get("schema") or "",
                )
                outputs = [o.upper() for o in io.get("outputs") or []]
                score = 0
                if any(bare == o.split(".")[-1] for o in outputs):
                    score += 10
                if any(table_fqn.upper() == o or o.endswith(f".{bare}") for o in outputs):
                    score += 5
                if re.search(rf"\b(INSERT|MERGE)\b[\s\S]{{0,80}}\b{re.escape(bare)}\b", sql_body, re.I):
                    score += 8
                if score <= 0 and bare not in sql_body.upper():
                    continue
                scored.append((score, {
                    "level": 0,
                    "object_name": item.get("fqn") or f"{db}.{item.get('schema')}.{item.get('name')}",
                    "object_type": "procedure" if obj_type != "task" else "task",
                    "sql_body": sql_body[:3000],
                    "inputs": io.get("inputs") or [],
                    "outputs": io.get("outputs") or [],
                }))

            scored.sort(key=lambda x: x[0], reverse=True)
            for score, entry in scored:
                fqn = entry["object_name"].upper()
                if fqn in seen:
                    continue
                # Prefer writers; allow reference-only if nothing scored as writer yet.
                if score < 5 and any(s >= 5 for s, _ in scored):
                    continue
                seen.add(fqn)
                chain.append(entry)
                if len(chain) >= max_procs:
                    return chain

        return chain

    # ---- INFORMATION_SCHEMA downstream discovery --------------------------

    def discover_downstream_views(self, database: str, table_name: str) -> List[Dict[str, Any]]:
        """Find views whose definition references table_name."""
        if not self.sf._configured() or not _IDENTIFIER_RE.match(database):
            return []
        try:
            cur = self.sf._connect().cursor()
            cur.execute(
                f"SELECT TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME"
                f" FROM {database}.INFORMATION_SCHEMA.VIEWS"
                f" WHERE LOWER(VIEW_DEFINITION) LIKE LOWER(%s)"
                f" LIMIT 50",
                (f"%{table_name}%",),
            )
            results = []
            for cat, sch, name in cur.fetchall():
                fqn = f"{cat}.{sch}.{name}"
                results.append({"type": "view", "database": cat, "schema": sch,
                                "name": name, "fqn": fqn})
            return results
        except Exception:
            return []

    def discover_downstream_procedures(self, database: str, table_name: str) -> List[Dict[str, Any]]:
        """Find procedures whose definition references table_name."""
        if not self.sf._configured() or not _IDENTIFIER_RE.match(database):
            return []
        try:
            cur = self.sf._connect().cursor()
            cur.execute(
                f"SELECT PROCEDURE_CATALOG, PROCEDURE_SCHEMA, PROCEDURE_NAME"
                f" FROM {database}.INFORMATION_SCHEMA.PROCEDURES"
                f" WHERE LOWER(PROCEDURE_DEFINITION) LIKE LOWER(%s)"
                f" LIMIT 50",
                (f"%{table_name}%",),
            )
            results = []
            for cat, sch, name in cur.fetchall():
                fqn = f"{cat}.{sch}.{name}"
                results.append({"type": "procedure", "database": cat, "schema": sch,
                                "name": name, "fqn": fqn})
            return results
        except Exception:
            return []

    def discover_all_downstream(self, tables: List[str],
                                context_database: str = "",
                                context_schema: str = "",
                                max_tables: int = 3) -> Dict[str, List[Dict[str, Any]]]:
        """For each table, discover downstream views and procedures via INFORMATION_SCHEMA."""
        result: Dict[str, List[Dict[str, Any]]] = {}
        seen_fqns: Set[str] = set()
        unique_tables = list(dict.fromkeys(tables or []))[:max(1, int(max_tables))]

        def _discover_one(table_fqn: str) -> Tuple[str, List[Dict[str, Any]]]:
            parts = table_fqn.split(".")
            if len(parts) == 3:
                db, _schema, tbl = parts
            elif len(parts) == 2:
                db = context_database
                tbl = parts[1]
            else:
                db = context_database
                tbl = parts[0]

            if not db or not tbl:
                return table_fqn, []

            downstream: List[Dict[str, Any]] = []
            for item in self.discover_downstream_views(db, tbl):
                downstream.append(item)
            for item in self.discover_downstream_procedures(db, tbl):
                downstream.append(item)
            return table_fqn, downstream

        if not unique_tables:
            return result

        with ThreadPoolExecutor(max_workers=min(4, len(unique_tables))) as pool:
            futures = [pool.submit(_discover_one, t) for t in unique_tables]
            for fut in as_completed(futures):
                table_fqn, items = fut.result()
                filtered: List[Dict[str, Any]] = []
                for item in items:
                    if item["fqn"] not in seen_fqns:
                        seen_fqns.add(item["fqn"])
                        filtered.append(item)
                if filtered:
                    result[table_fqn] = filtered

        return result

    def enrich_table_lineage_with_downstream(
        self,
        table_lineage: Dict[str, Any],
        downstream_map: Dict[str, List[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        """Add downstream view/procedure nodes and edges to an existing table lineage graph."""
        nodes = list(table_lineage.get("nodes", []))
        edges = list(table_lineage.get("edges", []))
        existing_ids = {n["id"] for n in nodes}
        added = 0
        max_nodes = 30

        for table_fqn, consumers in downstream_map.items():
            parent_id = f"tbl_{table_fqn}"
            if parent_id not in existing_ids:
                nodes.append({"id": parent_id, "name": table_fqn, "type": "table", "state": "failed"})
                existing_ids.add(parent_id)

            for item in consumers:
                if added >= max_nodes:
                    break
                node_id = f"{item['type']}_{item['fqn']}"
                if node_id not in existing_ids:
                    existing_ids.add(node_id)
                    nodes.append({
                        "id": node_id,
                        "name": item["name"],
                        "type": item["type"],
                        "state": "impacted",
                    })
                    added += 1
                edges.append({"from": parent_id, "to": node_id})

        return {"nodes": nodes, "edges": edges}

    # ---- table lineage graph (existing) ----------------------------------

    def build_table_lineage_graph(
        self,
        relevant_keys: Set[str],
        root_key: str,
        impacted_keys: List[str],
        graph: Dict[str, Dict[str, Any]],
        run_by_key: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Build a table-level lineage graph showing how tables connect through tasks."""
        nodes: List[Dict[str, Any]] = []
        edges: List[Dict[str, Any]] = []
        seen_tables: Set[str] = set()
        impacted_set = set(impacted_keys)

        task_tables: Dict[str, List[str]] = {}
        for tkey in relevant_keys:
            run = run_by_key.get(tkey, {})
            meta = graph.get(tkey, {})
            tbls = list(run.get("tables") or meta.get("tables") or [])
            if tbls:
                task_tables[tkey] = tbls

        def table_state(tkey: str) -> str:
            if tkey == root_key:
                return "root_cause"
            run = run_by_key.get(tkey, {})
            if run.get("status") in ("FAILED", "DELAYED"):
                return "failed"
            if tkey in impacted_set:
                return "impacted"
            return "healthy"

        for tkey in relevant_keys:
            if tkey not in task_tables:
                continue
            state = table_state(tkey)
            run = run_by_key.get(tkey, {})
            meta = graph.get(tkey, {})
            task_label = run.get("name") or meta.get("name") or tkey
            nodes.append({
                "id": tkey,
                "name": task_label,
                "type": "task",
                "state": state,
            })
            for tbl in task_tables[tkey]:
                if tbl not in seen_tables:
                    seen_tables.add(tbl)
                    nodes.append({
                        "id": f"tbl_{tbl}",
                        "name": tbl,
                        "type": "table",
                        "state": state,
                    })
                edges.append({"from": tkey, "to": f"tbl_{tbl}"})

        for tkey in relevant_keys:
            for upstream_key in graph.get(tkey, {}).get("upstream", []):
                if upstream_key not in relevant_keys:
                    continue
                up_tables = task_tables.get(upstream_key, [])
                if up_tables and tkey in task_tables:
                    for tbl in up_tables:
                        edges.append({"from": f"tbl_{tbl}", "to": tkey})

        return {"nodes": nodes, "edges": edges}


_INSERT_OUT_RE = re.compile(
    r"(?:INSERT\s+(?:OVERWRITE\s+)?INTO|MERGE\s+INTO"
    r"|CREATE\s+(?:OR\s+REPLACE\s+)?(?:TRANSIENT\s+)?(?:TABLE|VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?)"
    r"\s+([\w.]+)",
    re.IGNORECASE,
)
_TRUNCATE_OUT_RE = re.compile(r"TRUNCATE\s+(?:TABLE\s+)?([\w.]+)", re.IGNORECASE)
_CALL_RE = re.compile(r"\bCALL\s+([\w.]+)\s*\(", re.IGNORECASE)


def _classify_sql_io(sql: str, default_db: str = "", default_schema: str = "") -> Dict[str, List[str]]:
    """Heuristic: INSERT INTO / MERGE INTO / CREATE AS are outputs; FROM/JOIN are inputs."""
    all_tables: Set[str] = set(parse_tables_from_text(sql))
    output_bare: Set[str] = set()

    for pattern in (_INSERT_OUT_RE, _TRUNCATE_OUT_RE):
        for m in pattern.finditer(sql):
            ref = m.group(1).upper()
            parts = ref.split(".")
            if len(parts) == 3:
                all_tables.add(ref)
                output_bare.add(ref)
            elif len(parts) == 2 and default_db:
                fqn = f"{default_db.upper()}.{ref}"
                all_tables.add(fqn)
                output_bare.add(fqn)
                output_bare.add(ref)
            elif len(parts) == 1 and default_db and default_schema:
                fqn = f"{default_db.upper()}.{default_schema.upper()}.{ref}"
                all_tables.add(fqn)
                output_bare.add(fqn)
                output_bare.add(ref)
            output_bare.add(parts[-1])

    inputs, outputs = [], []
    for tbl in sorted(all_tables):
        bare = tbl.split(".")[-1].upper()
        if tbl.upper() in output_bare or bare in output_bare:
            outputs.append(tbl)
        else:
            inputs.append(tbl)
    return {"inputs": inputs, "outputs": outputs}


def _parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip().replace("Z", "+00:00")
    if len(s) == 15 and s[8] == "_":
        try:
            return datetime.strptime(s, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s[:26])
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[: len(fmt)], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def index_runs_by_task_key(pipelines: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Latest run per task_key (prefers FAILED/DELAYED over SUCCESS)."""
    by_key: Dict[str, Dict[str, Any]] = {}
    priority = {"FAILED": 3, "DELAYED": 2, "RUNNING": 1, "SUCCESS": 0, "SKIPPED": 0}

    for p in pipelines:
        key = p.get("task_key") or p.get("id")
        existing = by_key.get(key)
        if not existing:
            by_key[key] = p
            continue
        if priority.get(p.get("status", ""), 0) > priority.get(existing.get("status", ""), 0):
            by_key[key] = p
    return by_key
