"""Backend filter helpers for Dashboard Tasks / DQ tables."""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

_DQ_PAGE_SIZE_DEFAULT = 10
_DQ_PAGE_SIZE_MAX = 500

_STATUS_ALIASES = {
    "SUCCESS": ["SUCCESS", "SUCCEEDED", "PASS", "PASSED", "OK"],
    "FAILED": ["FAILED", "FAIL", "TIMEOUT", "FAILED_AND_AUTO_SUSPENDED", "ERROR"],
    "SKIPPED": ["SKIPPED", "SKIP", "CANCELLED"],
    "RUNNING": ["RUNNING", "EXECUTING", "PENDING"],
    "SCHEDULED": ["SCHEDULED", "PLANNED"],
    "DELAYED": ["DELAYED"],
    "WARNING": ["WARNING", "WARN"],
}

_NUMERIC_QC = re.compile(r"^\d+$")


def matches_status(row: Dict[str, Any], status_filter: Optional[str]) -> bool:
    if not status_filter or str(status_filter).upper() == "ALL":
        return True
    needle = str(status_filter).upper()
    status = str(row.get("status") or "").upper()
    aliases = _STATUS_ALIASES.get(needle, [needle])
    return status in aliases


def matches_dq_text(check: Dict[str, Any], q: Optional[str]) -> bool:
    needle = (q or "").strip().lower()
    if not needle:
        return True
    # Purely numeric → exact QC ID (avoid matching 23 when searching "3").
    if _NUMERIC_QC.match(needle):
        return str(check.get("name") or "").strip().lower() == needle
    haystack = [
        check.get("name"),
        check.get("table_name"),
        check.get("column_name"),
    ]
    return any(needle in str(v).lower() for v in haystack if v is not None and str(v))


def matches_task_text(pipeline: Dict[str, Any], q: Optional[str]) -> bool:
    needle = (q or "").strip().lower()
    if not needle:
        return True
    haystack = [pipeline.get("name"), pipeline.get("platform")]
    return any(needle in str(v).lower() for v in haystack if v is not None and str(v))


def matches_platform(pipeline: Dict[str, Any], platform_filter: Optional[str]) -> bool:
    if not platform_filter or str(platform_filter).upper() == "ALL":
        return True
    return str(pipeline.get("platform") or "").lower() == str(platform_filter).lower()


def filter_dq_checks(
    checks: Iterable[Dict[str, Any]],
    *,
    status: Optional[str] = None,
    q: Optional[str] = None,
) -> List[Dict[str, Any]]:
    return [
        c for c in checks
        if matches_status(c, status) and matches_dq_text(c, q)
    ]


def filter_task_pipelines(
    pipelines: Iterable[Dict[str, Any]],
    *,
    status: Optional[str] = None,
    q: Optional[str] = None,
    platform: Optional[str] = None,
) -> List[Dict[str, Any]]:
    return [
        p for p in pipelines
        if matches_status(p, status)
        and matches_task_text(p, q)
        and matches_platform(p, platform)
    ]


def normalize_page(page: Any = None, page_size: Any = None) -> Tuple[int, int]:
    try:
        page_n = int(page) if page not in (None, "") else 1
    except (TypeError, ValueError):
        page_n = 1
    try:
        size_n = int(page_size) if page_size not in (None, "") else _DQ_PAGE_SIZE_DEFAULT
    except (TypeError, ValueError):
        size_n = _DQ_PAGE_SIZE_DEFAULT
    return max(1, page_n), min(_DQ_PAGE_SIZE_MAX, max(1, size_n))


def paginate_rows(
    rows: List[Dict[str, Any]],
    *,
    page: Any = None,
    page_size: Any = None,
) -> Dict[str, Any]:
    """Slice rows for API response. page is 1-based; page_from/page_to are inclusive."""
    page_n, size_n = normalize_page(page, page_size)
    total = len(rows)
    start = (page_n - 1) * size_n
    if start >= total:
        # Past the end → last page (or empty).
        page_n = max(1, (total + size_n - 1) // size_n) if total else 1
        start = (page_n - 1) * size_n
    end = min(start + size_n, total)
    page_rows = rows[start:end]
    return {
        "rows": page_rows,
        "page": page_n,
        "page_size": size_n,
        "match_count": total,
        "page_from": (start + 1) if page_rows else 0,
        "page_to": end if page_rows else 0,
    }


def failing_checks_preview(
    checks: Iterable[Dict[str, Any]],
    *,
    limit: int = 30,
) -> List[Dict[str, Any]]:
    """Bounded failing rows for chat context (avoids shipping full inventory)."""
    out: List[Dict[str, Any]] = []
    for c in checks:
        if str(c.get("status") or "").upper() in ("FAILED", "WARNING", "DELAYED"):
            out.append(c)
            if len(out) >= limit:
                break
    return out
