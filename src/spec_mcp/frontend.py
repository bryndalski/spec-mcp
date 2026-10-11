"""Frontend collections: a UI component registry (one *.registry.json per component)
and design records (one JSON per view) that tie components, routes, requirements,
artboards and acceptance together."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .errors import SpecError
from .parse import rel

STATUSES = ("brief", "designed", "specified", "implemented", "verified")
SOURCES = ("duotio-ui", "feature", "new-duotio-ui")
DESIGN_KEYS = (
    "id",
    "title",
    "route",
    "roles",
    "flow",
    "requirements",
    "status",
    "artifact_url",
    "artboards",
    "brief",
    "components",
    "acceptance",
    "decisions",
    "evidence",
)
_TOKEN_DEF = re.compile(r"(--[A-Za-z0-9][A-Za-z0-9_-]*)\s*:")
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_PASCAL = re.compile(r"^[A-Z][A-Za-z0-9]*$")


def _warn(
    warnings: list[dict[str, Any]] | None, path: Path, root: Path, msg: str
) -> None:
    if warnings is not None:
        warnings.append({"category": "config", "file": rel(path, root), "message": msg})


# ---------------------------------------------------------------- components


def load_component(
    path: Path, warnings: list[dict[str, Any]] | None, root: Path
) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _warn(warnings, path, root, f"skipped: invalid JSON ({e})")
        return None
    if not isinstance(data, dict) or not data.get("name"):
        _warn(warnings, path, root, "skipped: registry file without a name")
        return None
    props = [
        {
            "name": str(p.get("name") or ""),
            "type": str(p.get("type") or ""),
            "default": str(p.get("default") or ""),
            "required": bool(p.get("required", False)),
            "desc": str(p.get("desc") or p.get("description") or ""),
        }
        for p in (data.get("props") or [])
        if isinstance(p, dict) and p.get("name")
    ]
    return {
        "name": str(data["name"]),
        "import": str(data.get("import") or ""),
        "description": str(data.get("description") or ""),
        "category": str(data.get("category") or ""),
        "props": props,
        "variants": data.get("variants")
        if isinstance(data.get("variants"), dict)
        else {},
        "tokensUsed": [str(t) for t in (data.get("tokensUsed") or [])],
        "a11y": data.get("a11y") if isinstance(data.get("a11y"), dict) else {},
        "examples": [e for e in (data.get("examples") or []) if isinstance(e, dict)],
        "dependsOn": [str(d) for d in (data.get("dependsOn") or [])],
        "file": rel(path, root),
        "dir": rel(path.parent, root),
    }


def load_index_names(
    path: Path, warnings: list[dict[str, Any]] | None, root: Path
) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _warn(warnings, path, root, f"skipped: invalid JSON ({e})")
        return []
    items = data.get("components", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        _warn(warnings, path, root, 'skipped: no "components" array')
        return []
    return [str(c["name"]) for c in items if isinstance(c, dict) and c.get("name")]


def load_tokens(path: Path) -> list[str]:
    """Every `--name:` declaration in the file — several per line, inside any block
    (:root, .dark, media queries) — with /* … */ comments stripped first."""
    if not path.is_file():
        return []
    css = _CSS_COMMENT.sub(" ", path.read_text(encoding="utf-8", errors="replace"))
    return sorted(set(_TOKEN_DEF.findall(css)))


def expected_files(comp: dict[str, Any], patterns: list[str]) -> list[str]:
    return [f"{comp['name']}{p}" if p.startswith(".") else p for p in patterns]


def file_presence(
    comp: dict[str, Any], patterns: list[str], root: Path
) -> dict[str, bool]:
    d = root / comp["dir"]
    return {f: (d / f).is_file() for f in expected_files(comp, patterns)}


def component_text(c: dict[str, Any]) -> str:
    parts = [c["name"], c["name"], c["category"], c["description"]]
    parts += [f"{p['name']} {p['desc']}" for p in c["props"]]
    parts += [str(e.get("title") or "") for e in c["examples"]]
    return "\n".join(parts)


def validate_components(
    comps: dict[str, dict[str, Any]],
    index_names: list[str],
    tokens: list[str],
    patterns: list[str],
    root: Path,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    token_set = set(tokens)
    index_set = set(index_names)
    for c in comps.values():
        for f, present in file_presence(c, patterns, root).items():
            if not present:
                findings.append(
                    {"kind": "missing_file", "component": c["name"], "file": f}
                )
        if tokens:
            for t in c["tokensUsed"]:
                if t not in token_set:
                    findings.append(
                        {"kind": "unknown_token", "component": c["name"], "token": t}
                    )
        if index_names and c["name"] not in index_set:
            findings.append({"kind": "not_in_index", "component": c["name"]})
        for d in c["dependsOn"]:
            if _PASCAL.match(d) and d not in comps:
                findings.append(
                    {
                        "kind": "unknown_dependency",
                        "component": c["name"],
                        "dependsOn": d,
                    }
                )
    for n in index_names:
        if n not in comps:
            findings.append({"kind": "not_on_disk", "component": n})
    return findings


# ---------------------------------------------------------------- designs


def norm_design(item: dict[str, Any], *, file: str | None) -> dict[str, Any]:
    def lst(k: str) -> list[Any]:
        v = item.get(k)
        return list(v) if isinstance(v, list) else []

    brief = item.get("brief") if isinstance(item.get("brief"), dict) else {}
    comps = []
    for c in lst("components"):
        if isinstance(c, dict) and c.get("name"):
            comps.append(
                {
                    "name": str(c["name"]),
                    "source": str(c.get("source") or "duotio-ui"),
                    "path": str(c.get("path") or ""),
                    "props": [str(x) for x in (c.get("props") or [])],
                    "states": [str(x) for x in (c.get("states") or [])],
                    "i18n_keys": [str(x) for x in (c.get("i18n_keys") or [])],
                    "tests": [str(x) for x in (c.get("tests") or [])],
                }
            )
    boards = []
    for b in lst("artboards"):
        if isinstance(b, dict):
            try:
                vp = int(b.get("viewport") or 0)
            except (TypeError, ValueError):
                vp = 0
            boards.append(
                {
                    "name": str(b.get("name") or ""),
                    "viewport": vp,
                    "theme": str(b.get("theme") or "light"),
                    "url": str(b.get("url") or ""),
                }
            )
    out = {
        "id": str(item["id"]),
        "title": str(item.get("title") or item["id"]),
        "route": str(item.get("route") or ""),
        "roles": [str(r) for r in lst("roles")],
        "flow": str(item.get("flow") or ""),
        "requirements": [str(r) for r in lst("requirements")],
        "status": str(item.get("status") or "brief"),
        "artifact_url": str(item.get("artifact_url") or ""),
        "artboards": boards,
        "brief": {
            "goal": str(brief.get("goal") or ""),
            "primary_action": str(brief.get("primary_action") or ""),
            "style": str(brief.get("style") or ""),
            "decoration_budget": brief.get("decoration_budget"),
            "references": [str(x) for x in (brief.get("references") or [])],
        },
        "components": comps,
        "acceptance": [str(a) for a in lst("acceptance")],
        "decisions": [str(d) for d in lst("decisions")],
        "evidence": [str(e) for e in lst("evidence")],
    }
    for k, v in item.items():
        if k not in out:
            out[k] = v
    if file:
        out["file"] = file
    return out


def load_design(
    path: Path, warnings: list[dict[str, Any]] | None, root: Path
) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _warn(warnings, path, root, f"skipped: invalid JSON ({e})")
        return None
    if not isinstance(data, dict) or not data.get("id"):
        _warn(warnings, path, root, "skipped: design file without an id")
        return None
    return norm_design(data, file=rel(path, root))


def write_design(path: Path, record: dict[str, Any]) -> None:
    clean = norm_design(record, file=None)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(clean, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError as e:
        raise SpecError("io", f"cannot write {path}: {e}", retryable=True) from e


def status_rank(s: str) -> int:
    return STATUSES.index(s) if s in STATUSES else -1


def load_messages(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def has_key(messages: dict[str, Any], dotted: str) -> bool:
    cur: Any = messages
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


def validate_designs(
    designs: list[dict[str, Any]],
    comps: dict[str, dict[str, Any]],
    requirements: set[str],
    messages: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for d in designs:
        did = d["id"]
        rank = status_rank(d["status"])
        if rank < 0:
            findings.append(
                {"kind": "unknown_status", "design": did, "status": d["status"]}
            )
        for c in d["components"]:
            if c["source"] == "duotio-ui" and c["name"] not in comps:
                findings.append(
                    {
                        "kind": "component_not_in_registry",
                        "design": did,
                        "component": c["name"],
                    }
                )
            if c["source"] in ("feature", "new-duotio-ui") and not c["path"]:
                findings.append(
                    {
                        "kind": "component_without_path",
                        "design": did,
                        "component": c["name"],
                        "source": c["source"],
                    }
                )
            if c["source"] not in SOURCES:
                findings.append(
                    {
                        "kind": "unknown_component_source",
                        "design": did,
                        "component": c["name"],
                        "source": c["source"],
                    }
                )
            if rank >= status_rank("implemented") and messages is not None:
                for k in c["i18n_keys"]:
                    if not has_key(messages, k):
                        findings.append(
                            {
                                "kind": "missing_i18n_key",
                                "design": did,
                                "component": c["name"],
                                "key": k,
                            }
                        )
        for r in d["requirements"]:
            if r not in requirements:
                findings.append(
                    {"kind": "unknown_requirement", "design": did, "requirement": r}
                )
        if rank >= status_rank("designed"):
            if not d["artifact_url"]:
                findings.append(
                    {
                        "kind": "missing_artifact_url",
                        "design": did,
                        "status": d["status"],
                    }
                )
            have = sorted({f"{b['viewport']}/{b['theme']}" for b in d["artboards"]})
            need = ["390/dark", "390/light"]
            if any(n not in have for n in need):
                findings.append(
                    {
                        "kind": "missing_artboards",
                        "design": did,
                        "need": need,
                        "have": have,
                    }
                )
        if rank >= status_rank("specified") and not d["acceptance"]:
            findings.append(
                {"kind": "empty_acceptance", "design": did, "status": d["status"]}
            )
    return findings


def render_design_index(designs: list[dict[str, Any]], marker: str) -> str:
    lines = [
        f"<!-- {marker}; do not edit by hand -->",
        "# Design views index",
        "",
        f"_{len(designs)} views._",
        "",
        "| ID | Title | Route | Status | Roles | Components | Artboards | Requirements |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for d in sorted(designs, key=lambda x: x["id"]):
        lines.append(
            f"| {d['id']} | {d['title'].replace('|', '\\|')} | `{d['route']}` | {d['status']} | {', '.join(d['roles'])} | "
            f"{len(d['components'])} | {len(d['artboards'])} | {', '.join(d['requirements'])} |"
        )
    lines.append("")
    return "\n".join(lines)
