"""Data-model collection: DynamoDB-style tables with keys, GSIs, entities, attributes,
pseudo-relations and access patterns, kept as JSON so an agent can query and aggregate
them instead of re-discovering the model from code."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import SpecError
from .parse import rel

TABLE_KEYS = (
    "name",
    "source",
    "description",
    "pk",
    "sk",
    "gsis",
    "ttl",
    "stream",
    "entities",
    "attributes",
    "relations",
    "access_patterns",
)
GSI_KEYS = ("name", "pk", "sk", "projection", "attributes")
ATTR_KEYS = ("name", "type", "required", "pii", "description")
REL_KEYS = ("from", "to", "kind", "enforced_by", "note")
PATTERN_KEYS = ("name", "operation", "index", "key", "repository", "description")
OPERATIONS = (
    "Query",
    "GetItem",
    "BatchGet",
    "Put",
    "Update",
    "Delete",
    "Transact",
    "Scan",
)
KINDS = ("belongs_to", "has_many", "references")


def _ordered(d: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    out = {k: d[k] for k in keys if k in d}
    out.update({k: v for k, v in d.items() if k not in out})
    return out


def _key(v: Any) -> dict[str, Any] | None:
    if not v:
        return None
    if isinstance(v, str):
        return {"name": v, "type": "S"}
    if isinstance(v, dict) and v.get("name"):
        return {"name": str(v["name"]), "type": str(v.get("type") or "S")}
    return None


def norm_table(item: dict[str, Any], *, file: str) -> dict[str, Any]:
    gsis = []
    for g in item.get("gsis") or []:
        if isinstance(g, dict) and g.get("name"):
            gsis.append(
                _ordered(
                    {
                        "name": str(g["name"]),
                        "pk": str(g.get("pk") or ""),
                        "sk": (str(g["sk"]) if g.get("sk") else None),
                        "projection": str(g.get("projection") or "ALL"),
                        "attributes": [str(a) for a in (g.get("attributes") or [])],
                    },
                    GSI_KEYS,
                )
            )
    attrs = []
    for a in item.get("attributes") or []:
        if isinstance(a, dict) and a.get("name"):
            attrs.append(
                _ordered(
                    {
                        "name": str(a["name"]),
                        "type": str(a.get("type") or "S"),
                        "required": bool(a.get("required", False)),
                        "pii": bool(a.get("pii", False)),
                        "description": str(a.get("description") or ""),
                    },
                    ATTR_KEYS,
                )
            )
    rels = []
    for r in item.get("relations") or []:
        if isinstance(r, dict) and r.get("from") and r.get("to"):
            rels.append(
                _ordered(
                    {
                        "from": str(r["from"]),
                        "to": str(r["to"]),
                        "kind": str(r.get("kind") or "references"),
                        "enforced_by": str(r.get("enforced_by") or "none"),
                        "note": str(r.get("note") or ""),
                    },
                    REL_KEYS,
                )
            )
    pats = [
        norm_pattern(p)
        for p in (item.get("access_patterns") or [])
        if isinstance(p, dict) and p.get("name")
    ]
    return _ordered(
        {
            "name": str(item["name"]),
            "source": str(item.get("source") or ""),
            "description": str(item.get("description") or ""),
            "pk": _key(item.get("pk")),
            "sk": _key(item.get("sk")),
            "gsis": gsis,
            "ttl": (str(item["ttl"]) if item.get("ttl") else None),
            "stream": bool(item.get("stream", False)),
            "entities": [str(e) for e in (item.get("entities") or [])],
            "attributes": attrs,
            "relations": rels,
            "access_patterns": pats,
            "file": file,
        },
        TABLE_KEYS,
    )


def norm_pattern(p: dict[str, Any]) -> dict[str, Any]:
    return _ordered(
        {
            "name": str(p["name"]),
            "operation": str(p.get("operation") or "Query"),
            "index": (str(p["index"]) if p.get("index") else None),
            "key": str(p.get("key") or ""),
            "repository": str(p.get("repository") or ""),
            "description": str(p.get("description") or ""),
        },
        PATTERN_KEYS,
    )


def repository_file(repository: str) -> str:
    """'path/to/file.ts:method' -> 'path/to/file.ts'."""
    return repository.split(":", 1)[0] if repository else ""


# ---------------------------------------------------------------- files


def load_tables_file(
    path: Path, warnings: list[dict[str, Any]] | None, root: Path
) -> list[dict[str, Any]]:
    def warn(msg: str) -> list[dict[str, Any]]:
        if warnings is not None:
            warnings.append(
                {"category": "config", "file": rel(path, root), "message": msg}
            )
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return warn(f"skipped: invalid JSON ({e})")
    if isinstance(data, dict):
        if "tables" not in data:
            return warn(
                'skipped: JSON object without a "tables" array (expected {"tables": [...]} or a bare array)'
            )
        items = data["tables"]
    else:
        items = data
    if not isinstance(items, list):
        return warn('skipped: "tables" is not an array')
    file = rel(path, root)
    return [
        norm_table(t, file=file) for t in items if isinstance(t, dict) and t.get("name")
    ]


def read_raw_tables(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SpecError("io", f"{path}: invalid JSON ({e})") from e
    items = data.get("tables", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise SpecError(
            "validation", f'{path}: expected {{"tables": [...]}} or an array'
        )
    return [t for t in items if isinstance(t, dict) and t.get("name")]


def write_tables(path: Path, tables: list[dict[str, Any]]) -> None:
    clean = []
    for t in tables:
        n = norm_table(t, file="")
        n.pop("file", None)
        clean.append(n)
    clean.sort(key=lambda t: t["name"])
    payload = {"$schema": "spec-mcp/datamodel/v1", "tables": clean}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError as e:
        raise SpecError("io", f"cannot write {path}: {e}", retryable=True) from e


# ---------------------------------------------------------------- views


def table_attr_names(t: dict[str, Any]) -> set[str]:
    names = {a["name"] for a in t["attributes"]}
    if t["pk"]:
        names.add(t["pk"]["name"])
    if t["sk"]:
        names.add(t["sk"]["name"])
    for g in t["gsis"]:
        names.add(g["pk"])
        if g["sk"]:
            names.add(g["sk"])
    return names


def all_relations(tables: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for t in tables.values():
        for r in t["relations"]:
            out.append(
                {
                    **r,
                    "from_table": r["from"].split(".", 1)[0],
                    "to_table": r["to"].split(".", 1)[0],
                    "declared_in": t["name"],
                }
            )
    return out


def all_patterns(tables: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for t in tables.values():
        for p in t["access_patterns"]:
            out.append(
                {
                    **p,
                    "table": t["name"],
                    "repository_file": repository_file(p["repository"]),
                }
            )
    return out


_MERMAID_KIND = {"belongs_to": "}o--||", "has_many": "||--o{", "references": "}o..o|"}


def mermaid(relations: list[dict[str, Any]]) -> str:
    lines = ["erDiagram"]
    for r in relations:
        arrow = _MERMAID_KIND.get(r["kind"], "}o..o|")
        label = f"{r['kind']} ({r['enforced_by']}) {r['from'].split('.', 1)[-1]} -> {r['to'].split('.', 1)[-1]}"
        lines.append(f'    {r["from_table"]} {arrow} {r["to_table"]} : "{label}"')
    return "\n".join(lines)


def validate(
    tables: dict[str, dict[str, Any]], raw_count: int, max_gsis: int, root: Path
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    names: dict[str, int] = {}
    for t in tables.values():
        names[t["name"]] = names.get(t["name"], 0) + 1
    if raw_count > len(tables):
        findings.append({"kind": "duplicate_table", "count": raw_count - len(tables)})
    for t in tables.values():
        if len(t["gsis"]) > max_gsis:
            findings.append(
                {
                    "kind": "too_many_gsis",
                    "table": t["name"],
                    "gsis": len(t["gsis"]),
                    "max": max_gsis,
                }
            )
        declared = {a["name"] for a in t["attributes"]}
        for g in t["gsis"]:
            for key in (g["pk"], g["sk"]):
                if key and key not in declared:
                    findings.append(
                        {
                            "kind": "gsi_key_not_in_attributes",
                            "table": t["name"],
                            "index": g["name"],
                            "attribute": key,
                        }
                    )
        gsi_names = {g["name"] for g in t["gsis"]}
        for p in t["access_patterns"]:
            if p["operation"].lower() == "scan":
                findings.append(
                    {
                        "kind": "scan_pattern",
                        "table": t["name"],
                        "pattern": p["name"],
                        "repository": p["repository"],
                    }
                )
            if p["index"] and p["index"] not in gsi_names:
                findings.append(
                    {
                        "kind": "unknown_index",
                        "table": t["name"],
                        "pattern": p["name"],
                        "index": p["index"],
                    }
                )
            rf = repository_file(p["repository"])
            if rf and not (root / rf).is_file():
                findings.append(
                    {
                        "kind": "repository_missing",
                        "table": t["name"],
                        "pattern": p["name"],
                        "path": rf,
                    }
                )
        for r in t["relations"]:
            for end in ("from", "to"):
                tbl, _, attr = r[end].partition(".")
                target = tables.get(tbl)
                if target is None:
                    findings.append(
                        {
                            "kind": "relation_unknown_table",
                            "table": t["name"],
                            "from": r["from"],
                            "to": r["to"],
                            "end": end,
                        }
                    )
                elif attr and attr not in table_attr_names(target):
                    findings.append(
                        {
                            "kind": "relation_unknown_attribute",
                            "table": t["name"],
                            "from": r["from"],
                            "to": r["to"],
                            "end": end,
                        }
                    )
    return findings


def render_index(
    tables: list[dict[str, Any]], rels: list[dict[str, Any]], marker: str
) -> str:
    lines = [
        f"<!-- {marker}; do not edit by hand -->",
        "# Data model index",
        "",
        f"_{len(tables)} tables._",
        "",
    ]
    for t in sorted(tables, key=lambda x: x["name"]):
        pk = t["pk"]["name"] if t["pk"] else "—"
        sk = t["sk"]["name"] if t["sk"] else "—"
        lines += [f"## {t['name']}", ""]
        if t["description"]:
            lines += [t["description"], ""]
        lines += [
            f"- Keys: `{pk}` / `{sk}`"
            + (f" · TTL `{t['ttl']}`" if t["ttl"] else "")
            + (" · stream" if t["stream"] else "")
        ]
        lines += [f"- Entities: {', '.join(t['entities']) or '—'}"]
        if t["source"]:
            lines += [f"- Source: `{t['source']}`"]
        if t["gsis"]:
            lines += ["", "| GSI | PK | SK | Projection |", "|---|---|---|---|"]
            lines += [
                f"| {g['name']} | {g['pk']} | {g['sk'] or ''} | {g['projection']} |"
                for g in t["gsis"]
            ]
        mine = [
            r
            for r in rels
            if r["from_table"] == t["name"] or r["to_table"] == t["name"]
        ]
        if mine:
            lines += ["", "| Relation | Kind | Enforced by |", "|---|---|---|"]
            lines += [
                f"| {r['from']} → {r['to']} | {r['kind']} | {r['enforced_by']} |"
                for r in mine
            ]
        if t["access_patterns"]:
            lines += [
                "",
                "| Access pattern | Operation | Index | Key | Repository |",
                "|---|---|---|---|---|",
            ]
            lines += [
                f"| {p['name']} | {p['operation']} | {p['index'] or '(table)'} | `{p['key']}` | `{p['repository']}` |"
                for p in t["access_patterns"]
            ]
        lines.append("")
    return "\n".join(lines)
