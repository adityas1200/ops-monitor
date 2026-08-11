"""Task dependency graph + table resolution for RCA."""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from app.connectors.dq_connector import DQConnector
from app.connectors.snowflake_connector import SnowflakeConnector
from app.core.config import get_dq_monitoring_config, get_task_monitoring_config, load_settings

_TABLE_RE = re.compile(
    r"\b([A-Z][A-Z0-9_]*\.[A-Z][A-Z0-9_]*\.[A-Z][A-Z0-9_]+)\b",
    re.IGNORECASE,
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_TASK_PREFIX_RE = re.compile(r"^(?:SF\s+Task:\s*)?(?:TASK_?)?", re.IGNORECASE)

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
        self._graph_cache: Optional[Dict[str, Dict[str, Any]]] = None
        self._def_cache: Dict[Tuple[str, str, str, str], Optional[str]] = {}
        self._io_cache: Dict[Tuple[str, str, str], Dict[str, List[str]]] = {}

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
        checks = DQConnector().read_results(date_from, date_to)
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

    # ---- Upstream lineage graph -------------------------------------------

    def build_upstream_lineage_graph(
        self,
        target_key: str,
        root_key: str,
        graph: Dict[str, Dict[str, Any]],
        run_by_key: Dict[str, Dict[str, Any]],
        resolve_io_keys: Optional[Set[str]] = None,
    ) -> Dict[str, Any]:
        """
        Build upstream lineage: raw source tables → upstream procedures/tasks → failed task.

        For each task in the backward walk:
          - Resolve procedure definition → separate input tables from output tables.
          - Input tables that are produced by an upstream task get a task→table→task chain.
          - Input tables with no upstream producer are marked as 'source' (raw/seed tables).
        The failed/root task appears on the rightmost side.
        """
        nodes: List[Dict[str, Any]] = []
        edges: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()
        visited_tasks: Set[str] = set()

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
            if tkey == root_key:
                return "root_cause"
            run = run_by_key.get(tkey, {})
            if run.get("status") in ("FAILED", "DELAYED"):
                return "failed"
            return "healthy"

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

            # Resolve input tables via procedure I/O (falls back to task tables)
            fallback = list(run.get("tables") or meta.get("tables") or [])
            db = meta.get("database") or run.get("database") or ""
            sch = meta.get("schema") or run.get("schema") or ""
            tname = meta.get("task_name") or ""
            should_resolve = resolve_io_keys is None or tkey in resolve_io_keys
            if db and sch and tname and should_resolve:
                io = self.resolve_task_procedure_io(db, sch, tname, fallback)
                input_tables = io["inputs"] if (io["inputs"] or io["outputs"]) else fallback
            else:
                input_tables = fallback

            upstream_keys = list(meta.get("upstream", []))

            # Build: table → upstream_task_that_produces_it
            producing_task: Dict[str, str] = {}
            for up_key in upstream_keys:
                up_run = run_by_key.get(up_key, {})
                up_meta = graph.get(up_key, {})
                for tbl in (up_run.get("tables") or up_meta.get("tables") or []):
                    producing_task.setdefault(tbl, up_key)

            for tbl in input_tables:
                tbl_id = f"tbl_{tbl}"
                prod = producing_task.get(tbl)
                if prod:
                    add_node(tbl_id, label=tbl, type="table", state=task_state(prod))
                    add_edge(prod, tbl_id)   # upstream task → table
                    add_edge(tbl_id, tkey)   # table → current task
                else:
                    add_node(tbl_id, label=tbl, type="table", state="source")
                    add_edge(tbl_id, tkey)   # raw source → current task

            for up_key in upstream_keys:
                if up_key not in visited_tasks:
                    queue.append(up_key)

        return {"nodes": nodes, "edges": edges}

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
