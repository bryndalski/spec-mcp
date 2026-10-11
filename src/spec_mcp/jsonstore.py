"""Read/write JSON collection files: {"items": [...]} pretty-printed, 2-space indent,
fixed key order, items sorted by id — diff-friendly for prettier/jq."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from .errors import SpecError
from .parse import FLOW_KEYS, REQ_KEYS, STEP_KEYS, empty_links, norm_links


def _ordered(item: dict[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in keys:
        if k in item:
            out[k] = item[k]
    for k, v in item.items():  # unknown keys are preserved, after the schema keys
        if k not in out:
            out[k] = v
    return out


def clean_requirement(item: dict[str, Any]) -> dict[str, Any]:
    it = {
        "id": str(item["id"]),
        "title": str(item.get("title") or str(item.get("text") or "")[:80]),
        "text": str(item.get("text") or ""),
        "status": item.get("status") or None,
        "domain": item.get("domain") or None,
        "tags": [str(t) for t in (item.get("tags") or [])],
        "acceptance": [str(a) for a in (item.get("acceptance") or [])],
        "links": norm_links(item.get("links")) if item.get("links") else empty_links(),
    }
    if isinstance(item.get("source"), dict):
        it["source"] = {"file": item["source"].get("file"), "line": item["source"].get("line")}
    for k, v in item.items():
        if k not in it and k not in REQ_KEYS:
            it[k] = v
    return _ordered(it, REQ_KEYS)


def clean_flow(item: dict[str, Any]) -> dict[str, Any]:
    steps = []
    for n, s in enumerate(item.get("steps") or [], start=1):
        s = s if isinstance(s, dict) else {"action": str(s)}
        st = {k: str(s.get(k, "") or "") for k in STEP_KEYS}
        st["n"] = st["n"] or str(n)
        steps.append(_ordered(st, STEP_KEYS))
    it = {
        "id": str(item["id"]),
        "title": str(item.get("title") or item["id"]),
        "roles": [str(r) for r in (item.get("roles") or [])],
        "requirements": [str(r) for r in (item.get("requirements") or [])],
        "steps": steps,
    }
    if isinstance(item.get("source"), dict):
        it["source"] = {"file": item["source"].get("file"), "line": item["source"].get("line")}
    for k, v in item.items():
        if k not in it and k not in FLOW_KEYS:
            it[k] = v
    return _ordered(it, FLOW_KEYS)


def read_items(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SpecError("io", f"{path}: invalid JSON ({e})") from e
    items = data.get("items", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise SpecError("validation", f"{path}: expected {{\"items\": [...]}} or an array")
    return [i for i in items if isinstance(i, dict) and i.get("id")]


def write_items(path: Path, items: list[dict[str, Any]], kind: str) -> None:
    clean = clean_requirement if kind == "requirements" else clean_flow
    rows = sorted((clean(i) for i in items), key=lambda i: i["id"])
    payload = {"$schema": f"spec-mcp/{kind}/v1", "items": rows}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as e:
        raise SpecError("io", f"cannot write {path}: {e}", retryable=True) from e


def upsert_item(path: Path, item: dict[str, Any], kind: str) -> str:
    """Insert or replace by id. Returns 'created' | 'updated'."""
    items = read_items(path)
    action = "created"
    for i, existing in enumerate(items):
        if str(existing.get("id")) == str(item["id"]):
            merged = {**existing, **{k: v for k, v in item.items() if v is not None}}
            items[i] = merged
            action = "updated"
            break
    else:
        items.append(item)
    write_items(path, items, kind)
    return action


def merge_items(path: Path, new: list[dict[str, Any]], kind: str) -> dict[str, int]:
    """Add items whose id is absent; never overwrite existing ones. Returns counts."""
    items = read_items(path)
    have = {str(i["id"]) for i in items}
    added = 0
    skipped = 0
    for it in new:
        if str(it["id"]) in have:
            skipped += 1
            continue
        items.append(it)
        have.add(str(it["id"]))
        added += 1
    write_items(path, items, kind)
    return {"added": added, "skipped_existing": skipped, "total": len(items)}
