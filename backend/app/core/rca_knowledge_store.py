"""Read/write helpers for user-contributed RCA knowledge in rca_knowledge.md."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

_RULE_KEYS = (
    "PATTERN", "CATEGORY", "ROOT_CAUSE", "FIX", "SQL_CODE",
    "ADDED_BY", "ADDED_ON", "NOTES",
)
_DEFAULT_PREAMBLE = (
    "# RCA Knowledge Base (User-contributed)\n\n"
    "Rules added by operations team. The RCA agent consults these before running analysis.\n\n"
    "## Rules\n\n"
)


def _encode_multiline(value: str) -> str:
    """Keep KEY: value on one line; store real newlines as \\n."""
    return (
        (value or "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
    )


def _decode_multiline(value: str) -> str:
    out: List[str] = []
    i = 0
    text = value or ""
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
            if nxt == "\\":
                out.append("\\")
                i += 2
                continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _rules_section(content: str) -> Tuple[str, str]:
    """Return (preamble_including_##_Rules, body_after_##_Rules)."""
    marker = "## Rules"
    idx = content.find(marker)
    if idx < 0:
        return _DEFAULT_PREAMBLE, content
    split_at = idx + len(marker)
    preamble = content[:split_at].rstrip() + "\n\n"
    return preamble, content[split_at:]


def parse_rules(content: str) -> List[Dict[str, str]]:
    """Parse rule blocks. Prefer the ## Rules section so the Format example is ignored."""
    _, body = _rules_section(content)
    rules: List[Dict[str, str]] = []
    current: Dict[str, str] = {}
    for line in body.split("\n"):
        stripped = line.strip()
        if stripped == "---":
            if current.get("PATTERN"):
                rules.append(current)
            current = {}
            continue
        if ":" in stripped and stripped.split(":")[0].strip().upper() in _RULE_KEYS:
            key, _, val = stripped.partition(":")
            key_u = key.strip().upper()
            raw = val.strip()
            current[key_u] = _decode_multiline(raw) if key_u == "SQL_CODE" else raw
    if current.get("PATTERN"):
        rules.append(current)
    return rules


def format_rule(rule: Dict[str, Any]) -> str:
    sql = str(rule.get("SQL_CODE") or rule.get("sql_code") or "")
    return (
        "---\n"
        f"PATTERN: {str(rule.get('PATTERN') or rule.get('pattern') or '').strip()}\n"
        f"CATEGORY: {str(rule.get('CATEGORY') or rule.get('category') or 'Unknown Failure').strip()}\n"
        f"ROOT_CAUSE: {str(rule.get('ROOT_CAUSE') or rule.get('root_cause') or '').strip()}\n"
        f"FIX: {str(rule.get('FIX') or rule.get('fix') or '').strip()}\n"
        f"SQL_CODE: {_encode_multiline(sql)}\n"
        f"ADDED_BY: {str(rule.get('ADDED_BY') or rule.get('added_by') or 'user').strip()}\n"
        f"ADDED_ON: {str(rule.get('ADDED_ON') or rule.get('added_on') or '').strip()}\n"
        f"NOTES: {str(rule.get('NOTES') or rule.get('notes') or '').strip()}\n"
        "---\n"
    )


def _field(payload: Dict[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        if key in payload and payload[key] is not None:
            return str(payload[key]).strip()
    return default


def payload_to_rule(payload: Dict[str, Any], *, existing: Dict[str, str] | None = None) -> Dict[str, str]:
    base = dict(existing or {})
    pattern = _field(payload, "pattern", "PATTERN", default=base.get("PATTERN") or "")
    if not pattern:
        raise ValueError("pattern is required")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {
        "PATTERN": pattern,
        "CATEGORY": _field(
            payload, "category", "CATEGORY",
            default=base.get("CATEGORY") or "Unknown Failure",
        ) or "Unknown Failure",
        "ROOT_CAUSE": _field(payload, "root_cause", "ROOT_CAUSE", default=base.get("ROOT_CAUSE") or ""),
        "FIX": _field(payload, "fix", "FIX", default=base.get("FIX") or ""),
        "SQL_CODE": _field(payload, "sql_code", "SQL_CODE", default=base.get("SQL_CODE") or ""),
        "ADDED_BY": _field(payload, "added_by", "ADDED_BY", default=base.get("ADDED_BY") or "user") or "user",
        "ADDED_ON": _field(payload, "added_on", "ADDED_ON", default=base.get("ADDED_ON") or today) or today,
        "NOTES": _field(payload, "notes", "NOTES", default=base.get("NOTES") or ""),
    }


def read_rules(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    return parse_rules(path.read_text(encoding="utf-8"))


def write_rules(path: Path, rules: List[Dict[str, Any]], *, content: str | None = None) -> None:
    raw = content if content is not None else (path.read_text(encoding="utf-8") if path.exists() else "")
    preamble, _ = _rules_section(raw) if raw else (_DEFAULT_PREAMBLE, "")
    body = "".join(format_rule(r) for r in rules)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(preamble + body, encoding="utf-8")


def append_rule(path: Path, payload: Dict[str, Any]) -> Dict[str, str]:
    rule = payload_to_rule(payload)
    rules = read_rules(path)
    rules.append(rule)
    write_rules(path, rules)
    return rule


def update_rule(path: Path, index: int, payload: Dict[str, Any]) -> Dict[str, str]:
    rules = read_rules(path)
    if index < 0 or index >= len(rules):
        raise IndexError(f"rule index {index} out of range (0..{len(rules) - 1})")
    updated = payload_to_rule(payload, existing=rules[index])
    # Preserve original ADDED_ON unless caller sent one explicitly.
    if "added_on" not in payload and "ADDED_ON" not in payload:
        updated["ADDED_ON"] = rules[index].get("ADDED_ON") or updated["ADDED_ON"]
    rules[index] = updated
    write_rules(path, rules)
    return updated


def delete_rule(path: Path, index: int) -> Dict[str, str]:
    rules = read_rules(path)
    if index < 0 or index >= len(rules):
        raise IndexError(f"rule index {index} out of range (0..{len(rules) - 1})")
    removed = rules.pop(index)
    write_rules(path, rules)
    return removed
