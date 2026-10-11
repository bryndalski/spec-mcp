"""spec-mcp — MCP server for project docs, user flows and requirements.

Every tool returns a plain dict. Failures come back as {"error": {category, message,
retryable}} instead of exceptions, and every list answer states what was scanned so an
empty result is distinguishable from "nothing was looked at"."""

from __future__ import annotations

import fnmatch
import functools
import re
from pathlib import Path
from typing import Any, Callable

try:  # mcp >= 2: FastMCP was renamed to MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP  # type: ignore[no-redef]

from .bm25 import tokenize
from .config import config_path, load_config
from .errors import SpecError
from .index import GENERATED_MARKER, SpecIndex
from . import datamodel as dm
from . import frontend as fe
from .jsonstore import merge_items, upsert_item
from .parse import is_test_evidence, md_flow_to_item, md_requirement_to_item, rel

mcp = FastMCP(
    "spec",
    instructions=(
        "Project specification tools: searchable docs, user flows and requirements with "
        "coverage and validation. Read-mostly; the only writers are req_upsert, req_link, "
        "flow_upsert and spec_index, and they never rewrite prose files they did not create."
    ),
)

_index: SpecIndex | None = None


def reset() -> None:
    """Drop the in-memory index (tests / config change)."""
    global _index
    _index = None


def _idx(force: bool = False) -> SpecIndex:
    global _index
    if _index is None:
        _index = SpecIndex(load_config(config_path()))
    _index.ensure(force=force)
    return _index


def _tool(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return fn(*args, **kwargs)
        except SpecError as e:
            return e.to_dict()
        except FileNotFoundError as e:
            return SpecError("not_found", str(e)).to_dict()
        except OSError as e:
            return SpecError("io", str(e), retryable=True).to_dict()

    return mcp.tool()(wrapper)


def _req_view(idx: SpecIndex, r: dict[str, Any], sidecar: dict[str, dict[str, list[str]]]) -> dict[str, Any]:
    inline = r.get("links") or {}
    extra = sidecar.get(r["id"], {}) if r.get("format") != "json" else {}
    links = {k: list(inline.get(k, [])) + [v for v in extra.get(k, []) if v not in inline.get(k, [])] for k in ("flows", "docs", "tests")}
    flows = [f["id"] for f in idx.flows.values() if r["id"] in f["requirements"]]
    for f in links["flows"]:
        if f not in flows:
            flows.append(f)
    tests = [e for e in r["evidence"] if is_test_evidence(e)]
    for t in links["tests"]:
        if t not in tests:
            tests.append(t)
    return {**r, "links": links, "flows": flows, "tests": tests, "docs": list(links["docs"])}


# ---------------------------------------------------------------- meta


@_tool
def spec_config() -> dict[str, Any]:
    """Show the effective configuration (defaults merged with SPEC_CONFIG), item counts per
    collection and the age of the index. Call first when results look unexpected."""
    idx = _idx()
    cfg = {k: v for k, v in idx.cfg.items() if not k.startswith("_")}
    return {
        "config": cfg,
        "config_file": idx.cfg["_source"],
        "root": idx.cfg["_root"],
        "counts": {"docs": idx.doc_files, "requirements": len(idx.requirements), "flows": len(idx.flows), "datamodel": len(idx.tables), "components": len(idx.components), "designs": len(idx.designs)},
        "files": {"requirements": idx.req_files, "flows": idx.flow_files, "datamodel": idx.dm_files, "components": idx.component_files, "component_index": idx.index_files, "designs": idx.design_files},
        "index_file": str(idx.cache_path),
        "index_age_seconds": idx.age_seconds(),
        "warnings": idx.warnings,
    }


# ---------------------------------------------------------------- docs


@_tool
def docs_search(query: str, limit: int = 10, glob: str | None = None) -> dict[str, Any]:
    """BM25 search over documentation chunks (one chunk per markdown heading). Returns
    file, heading, line, score and a snippet, plus how many chunks/files were searched.
    `glob` narrows the files (e.g. "docs/flows/**")."""
    idx = _idx()
    chunks = idx.chunks
    if glob:
        chunks = [c for c in chunks if fnmatch.fnmatch(c["file"], glob)]
        from .bm25 import BM25

        engine = BM25([tokenize(f"{c['heading']}\n{c['text']}") for c in chunks])
    else:
        engine = idx.bm25()
    toks = tokenize(query)
    hits = []
    for i, score in engine.search(toks, limit=max(1, min(limit, 100))):
        c = chunks[i]
        hits.append(
            {
                "file": c["file"],
                "heading": c["heading"],
                "line": c["line"],
                "score": round(score, 3),
                "snippet": _snippet(c["text"], toks),
            }
        )
    return {
        "query": query,
        "hits": hits,
        "searched_chunks": len(chunks),
        "searched_files": len({c["file"] for c in chunks}),
    }


def _snippet(text: str, toks: list[str], width: int = 220) -> str:
    low = text.lower()
    pos = -1
    for t in toks:
        pos = low.find(t)
        if pos >= 0:
            break
    start = max(0, pos - width // 3) if pos >= 0 else 0
    s = text[start : start + width].replace("\n", " ").strip()
    return ("…" if start > 0 else "") + s + ("…" if start + width < len(text) else "")


@_tool
def docs_get(path: str, heading: str | None = None) -> dict[str, Any]:
    """Read a documentation file, or only the section under `heading` (case-insensitive
    match on the heading text). Returns text with its 1-based line range."""
    idx = _idx()
    p = idx.resolve(path)
    if not p.is_file():
        raise SpecError("not_found", f"no such file: {path}")
    file = rel(p, idx.root)
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    if heading is None:
        return {"file": file, "line_start": 1, "line_end": len(lines), "text": "\n".join(lines)}
    want = heading.strip().lower()
    chunk = next((c for c in idx.chunks if c["file"] == file and c["heading"].lower() == want), None)
    if chunk is None:
        from .parse import chunk_doc

        chunk = next((c for c in chunk_doc(p, idx.root) if c["heading"].lower() == want), None)
    if chunk is None:
        headings = [c["heading"] for c in idx.chunks if c["file"] == file]
        raise SpecError("not_found", f"heading {heading!r} not in {file}", available=headings[:50])
    return {
        "file": file,
        "heading": chunk["heading"],
        "line_start": chunk["line"],
        "line_end": chunk["line_end"],
        "text": "\n".join(lines[chunk["line"] - 1 : chunk["line_end"]]),
    }


@_tool
def docs_list(glob: str | None = None, limit: int = 200) -> dict[str, Any]:
    """List documentation files (path, title = first H1, size). `glob` filters paths."""
    idx = _idx()
    files = idx.doc_list
    if glob:
        files = [f for f in files if fnmatch.fnmatch(f["file"], glob)]
    return {"files": files[: max(1, limit)], "total": len(files), "scanned_files": idx.doc_files}


# ---------------------------------------------------------------- requirements


@_tool
def req_list(
    domain: str | None = None,
    status: str | None = None,
    with_flow: bool | None = None,
    with_test: bool | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """List requirements. Filters: domain (file stem), status, with_flow / with_test
    (True = only covered, False = only uncovered). `total` counts matches before `limit`."""
    idx = _idx()
    links = idx.load_links()
    items = []
    for r in idx.requirements.values():
        v = _req_view(idx, r, links)
        if domain and v["domain"] != domain:
            continue
        if status and (v["status"] or "").upper() != status.upper():
            continue
        if with_flow is not None and bool(v["flows"]) != with_flow:
            continue
        if with_test is not None and bool(v["tests"]) != with_test:
            continue
        items.append(v)
    return {
        "items": items[: max(1, limit)],
        "total": len(items),
        "scanned": {"requirements": len(idx.requirements), "files": len(idx.req_files)},
    }


@_tool
def req_get(id: str) -> dict[str, Any]:
    """One requirement with its inline refs, evidence, sidecar links and the flows that
    mention it. Also lists duplicate definitions if the ID appears in several files."""
    idx = _idx()
    r = idx.requirements.get(id)
    if r is None:
        raise SpecError("not_found", f"requirement {id} not found", scanned=len(idx.requirements))
    v = _req_view(idx, r, idx.load_links())
    dups = [{"file": x["file"], "line": x["line"]} for x in idx.req_all if x["id"] == id]
    return {"requirement": v, "flows": v["flows"], "links": idx.links_for(id), "definitions": dups}


@_tool
def req_upsert(
    id: str,
    text: str,
    status: str | None = None,
    domain: str | None = None,
    file: str | None = None,
    title: str | None = None,
    tags: list[str] | None = None,
    acceptance: list[str] | None = None,
) -> dict[str, Any]:
    """Create or update a requirement in a JSON collection file (default:
    requirements.write_file). Items are stored pretty-printed, 2-space, fixed key order,
    sorted by id. Refuses when the ID is already defined in another file (markdown or JSON),
    so hand-written documents are never edited."""
    idx = _idx()
    if not idx.id_re.fullmatch(id):
        raise SpecError("validation", f"{id!r} does not match requirements.id_pattern {idx.id_re.pattern!r}")
    target = idx.resolve(file or idx.cfg["requirements"]["write_file"])
    if target.suffix != ".json":
        raise SpecError("validation", f"req_upsert writes JSON collections only; {rel(target, idx.root)} is not a .json file")
    target_rel = rel(target, idx.root)
    existing = idx.requirements.get(id)
    if existing and existing["file"] != target_rel:
        raise SpecError("validation", f"{id} is already defined in {existing['file']}:{existing['line']}; edit it there", file=existing["file"])
    item: dict[str, Any] = {"id": id, "text": text, "status": status, "domain": domain or target.stem, "title": title, "tags": tags, "acceptance": acceptance}
    if existing is None:
        item["links"] = {"flows": [], "docs": [], "tests": []}
    action = upsert_item(target, item, "requirements")
    idx.invalidate()
    idx.ensure(force=True)
    return {"action": action, "id": id, "file": target_rel, "format": "json"}


@_tool
def req_link(id: str, target_type: str, target: str) -> dict[str, Any]:
    """Link a requirement to a flow id, a doc path or a test path. Stored in the links
    sidecar (links_file), never inside the prose. Flow and doc targets must exist."""
    idx = _idx()
    if target_type not in ("flow", "doc", "test"):
        raise SpecError("validation", "target_type must be one of: flow, doc, test")
    if id not in idx.requirements:
        raise SpecError("not_found", f"requirement {id} not found", scanned=len(idx.requirements))
    if target_type == "flow" and target not in idx.flows:
        raise SpecError("not_found", f"flow {target!r} not found", available=sorted(idx.flows)[:50])
    if target_type == "doc" and not idx.resolve(target).is_file():
        raise SpecError("not_found", f"doc {target!r} not found")
    links = idx.add_link(id, target_type, target)
    return {"id": id, "links": links, "links_file": rel(idx.links_path, idx.root)}


@_tool
def req_coverage(domain: str | None = None) -> dict[str, Any]:
    """Coverage of requirements: how many have a status, a flow, a test, a doc — from inline
    evidence plus sidecar links — overall and per domain, with the uncovered IDs and the
    denominator (files and items scanned)."""
    idx = _idx()
    links = idx.load_links()
    rows = [_req_view(idx, r, links) for r in idx.requirements.values()]
    if domain:
        rows = [r for r in rows if r["domain"] == domain]

    def summarize(rs: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "total": len(rs),
            "with_status": sum(1 for r in rs if r["status"]),
            "with_flow": sum(1 for r in rs if r["flows"]),
            "with_test": sum(1 for r in rs if r["tests"]),
            "with_doc": sum(1 for r in rs if r["docs"]),
        }

    by_domain: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_domain.setdefault(r["domain"], []).append(r)
    uncovered = [r["id"] for r in rows if not (r["flows"] or r["tests"] or r["docs"])]
    out = summarize(rows)
    out["by_domain"] = {d: summarize(rs) for d, rs in sorted(by_domain.items())}
    out["uncovered"] = uncovered[:100]
    out["uncovered_total"] = len(uncovered)
    out["denominator"] = {
        "requirement_files": len(idx.req_files),
        "flow_files": len(idx.flow_files),
        "requirements": len(idx.requirements),
        "flows": len(idx.flows),
    }
    return out


# ---------------------------------------------------------------- flows


@_tool
def flow_list(limit: int = 200) -> dict[str, Any]:
    """List user flows: id, title, file, line, number of steps, linked requirement IDs."""
    idx = _idx()
    items = [
        {
            "id": f["id"],
            "title": f["title"],
            "file": f["file"],
            "line": f["line"],
            "steps": len(f["steps"]),
            "requirements": f["requirements"],
            "roles": f.get("roles", []),
        }
        for f in idx.flows.values()
    ]
    return {"items": items[: max(1, limit)], "total": len(items), "scanned_files": len(idx.flow_files)}


@_tool
def flow_get(id: str) -> dict[str, Any]:
    """One flow with its parsed steps (n, role, screen, action, evidence) and requirements."""
    idx = _idx()
    f = idx.flows.get(id)
    if f is None:
        raise SpecError("not_found", f"flow {id!r} not found", available=sorted(idx.flows)[:50])
    return {"flow": f}


@_tool
def flow_upsert(
    id: str,
    title: str,
    steps: list[dict[str, str]],
    requirements: list[str] | None = None,
    roles: list[str] | None = None,
    file: str | None = None,
) -> dict[str, Any]:
    """Create or replace a flow in a JSON collection file (default: flows.write_file).
    Steps are {n, role, screen, action, evidence}; `n` is filled in when missing.
    Refuses when the id is defined in another file (e.g. a hand-written markdown flow)."""
    idx = _idx()
    slug = id.strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9\-]*", slug):
        raise SpecError("validation", "id must be a kebab-case slug (a-z, 0-9, '-')")
    target = idx.resolve(file or idx.cfg["flows"]["write_file"])
    if target.suffix != ".json":
        raise SpecError("validation", f"flow_upsert writes JSON collections only; {rel(target, idx.root)} is not a .json file")
    target_rel = rel(target, idx.root)
    existing = idx.flows.get(slug)
    if existing and existing["file"] != target_rel:
        raise SpecError("validation", f"flow {slug} is defined in {existing['file']}:{existing['line']}; edit it there", file=existing["file"])
    item = {"id": slug, "title": title, "roles": list(roles or []), "requirements": list(requirements or []), "steps": list(steps or [])}
    action = upsert_item(target, item, "flows")
    idx.invalidate()
    idx.ensure(force=True)
    return {"action": action, "id": slug, "file": target_rel, "steps": len(steps or []), "format": "json"}


@_tool
def spec_import_markdown(collection: str, glob: str | None = None, target_file: str | None = None, dry_run: bool = True) -> dict[str, Any]:
    """Convert markdown-parsed requirements or flows into the JSON schema (keeping
    source.file/line). `collection` is "requirements" or "flows"; `glob` limits the source
    files. dry_run=True only reports counts and a sample; dry_run=False merges into
    `target_file` (default: the collection's write_file) without duplicating ids —
    existing JSON items are never overwritten."""
    idx = _idx()
    if collection not in ("requirements", "flows"):
        raise SpecError("validation", 'collection must be "requirements" or "flows"')
    sidecar = idx.load_links()
    if collection == "requirements":
        src = [r for r in idx.req_all if r.get("format") == "md"]
        if glob:
            src = [r for r in src if fnmatch.fnmatch(r["file"], glob)]
        items = [md_requirement_to_item(r, sidecar.get(r["id"])) for r in src]
    else:
        src = [f for f in idx.flows.values() if f.get("format") == "md"]
        if glob:
            src = [f for f in src if fnmatch.fnmatch(f["file"], glob)]
        items = [md_flow_to_item(f) for f in src]
    seen: set[str] = set()
    unique = []
    for it in items:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        unique.append(it)
    target = idx.resolve(target_file or idx.cfg[collection]["write_file"])
    if target.suffix != ".json":
        raise SpecError("validation", f"target_file must be a .json file, got {rel(target, idx.root)}")
    out: dict[str, Any] = {
        "collection": collection,
        "dry_run": dry_run,
        "source_files": sorted({it["source"]["file"] for it in unique}),
        "parsed": len(items),
        "unique": len(unique),
        "target": rel(target, idx.root),
        "sample": unique[:3],
    }
    if not dry_run:
        out["result"] = merge_items(target, unique, collection)
        idx.invalidate()
        idx.ensure(force=True)
    return out


# ---------------------------------------------------------------- validation / index


@_tool
def spec_validate() -> dict[str, Any]:
    """Find duplicate requirement IDs, dangling IDs (referenced but never defined, including
    from flows), and sidecar links pointing at missing flows/docs/requirements."""
    idx = _idx()
    findings: list[dict[str, Any]] = []
    by_id: dict[str, list[dict[str, Any]]] = {}
    for r in idx.req_all:
        by_id.setdefault(r["id"], []).append(r)
    for rid, defs in by_id.items():
        if len(defs) > 1:
            findings.append({"kind": "duplicate_id", "id": rid, "files": [f"{d['file']}:{d['line']}" for d in defs]})
    defined = set(idx.requirements)
    for r in idx.req_all:
        for ref in r["refs"]:
            if ref not in defined:
                findings.append({"kind": "dangling_id", "id": ref, "from": f"{r['file']}:{r['line']}", "in": "requirement " + r["id"]})
    for f in idx.flows.values():
        for ref in f["requirements"]:
            if ref not in defined:
                findings.append({"kind": "dangling_id", "id": ref, "from": f"{f['file']}:{f['line']}", "in": "flow " + f["id"]})
    links = idx.load_links()
    for rid, entry in links.items():
        if rid not in defined:
            findings.append({"kind": "link_to_missing_requirement", "id": rid, "from": rel(idx.links_path, idx.root)})
        for fl in entry.get("flows", []):
            if fl not in idx.flows:
                findings.append({"kind": "link_to_missing_flow", "id": rid, "target": fl})
        for d in entry.get("docs", []):
            if not idx.resolve(d).is_file():
                findings.append({"kind": "link_to_missing_doc", "id": rid, "target": d})
        for t in entry.get("tests", []):
            if not idx.resolve(t).is_file():
                findings.append({"kind": "link_to_missing_test", "id": rid, "target": t})
    return {
        "findings": findings,
        "total": len(findings),
        "checked": {
            "requirements": len(idx.requirements),
            "requirement_definitions": len(idx.req_all),
            "flows": len(idx.flows),
            "links": sum(len(v.get("flows", [])) + len(v.get("docs", [])) + len(v.get("tests", [])) for v in links.values()),
            "files": len(idx.req_files) + len(idx.flow_files),
        },
    }


@_tool
def spec_index() -> dict[str, Any]:
    """Force a rebuild of the index and regenerate INDEX.md in every requirements and flows
    directory and in the data-model directory (marked as generated). Returns counts and paths."""
    idx = _idx(force=True)
    written: list[str] = []
    req_dirs: dict[Path, list[dict[str, Any]]] = {}
    for r in idx.req_all:
        req_dirs.setdefault(idx.resolve(r["file"]).parent, []).append(r)
    for d, rs in req_dirs.items():
        rs = sorted(rs, key=lambda x: (x["file"], x.get("line") or 0, x["id"]))
        lines = [f"<!-- {GENERATED_MARKER}; do not edit by hand -->", "# Requirements index", "", f"_{len(rs)} requirements in {len({r['file'] for r in rs})} files._", "", "| ID | Status | Text | Source |", "|---|---|---|---|"]
        for r in rs:
            src = Path(r["file"]).name + (f":{r['line']}" if r.get("line") else "")
            lines.append(f"| {r['id']} | {r['status'] or ''} | {r['text'][:120].replace('|', '\\|')} | `{src}` |")
        p = d / "INDEX.md"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        written.append(rel(p, idx.root))
    flow_dirs: dict[Path, list[dict[str, Any]]] = {}
    for f in idx.flows.values():
        flow_dirs.setdefault(idx.resolve(f["file"]).parent, []).append(f)
    for d, fs in flow_dirs.items():
        fs = sorted(fs, key=lambda x: (x["file"], x.get("line") or 0, x["id"]))
        lines = [f"<!-- {GENERATED_MARKER}; do not edit by hand -->", "# Flows index", "", f"_{len(fs)} flows._", "", "| ID | Title | Steps | Requirements | Source |", "|---|---|---|---|---|"]
        for f in fs:
            lines.append(f"| {f['id']} | {f['title'].replace('|', '\\|')} | {len(f['steps'])} | {', '.join(f['requirements'])} | `{Path(f['file']).name}{(':' + str(f['line'])) if f.get('line') else ''}` |")
        p = d / "INDEX.md"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        written.append(rel(p, idx.root))
    if idx.designs:
        d_dir = idx.resolve(idx.design_files[0]).parent if idx.design_files else idx.resolve(idx.cfg["designs"]["write_dir"])
        p = d_dir / "INDEX.md"
        p.write_text(fe.render_design_index(list(idx.designs.values()), GENERATED_MARKER), encoding="utf-8")
        written.append(rel(p, idx.root))
    if idx.tables:
        dm_dir = idx.resolve(idx.cfg["datamodel"]["write_file"]).parent
        if idx.dm_files:
            dm_dir = idx.resolve(idx.dm_files[0]).parent
        p = dm_dir / "INDEX.md"
        p.write_text(dm.render_index(list(idx.tables.values()), dm.all_relations(idx.tables), GENERATED_MARKER), encoding="utf-8")
        written.append(rel(p, idx.root))
    idx.invalidate()
    idx.ensure(force=True)
    return {"requirements": len(idx.requirements), "flows": len(idx.flows), "docs": idx.doc_files, "datamodel": len(idx.tables), "components": len(idx.components), "designs": len(idx.designs), "written": written}


# ---------------------------------------------------------------- data model


def _dm_denominator(idx: SpecIndex) -> dict[str, int]:
    return {
        "files": len(idx.dm_files),
        "tables": len(idx.tables),
        "access_patterns": sum(len(t["access_patterns"]) for t in idx.tables.values()),
        "relations": sum(len(t["relations"]) for t in idx.tables.values()),
        "attributes": sum(len(t["attributes"]) for t in idx.tables.values()),
    }


@_tool
def dm_tables() -> dict[str, Any]:
    """List data-model tables: name, pk/sk names, number of GSIs, entities, attributes,
    relations and access patterns, and the JSON file that owns each table. Use this
    instead of grepping Terraform or repositories to learn the table layout."""
    idx = _idx()
    items = [
        {
            "name": t["name"],
            "pk": t["pk"]["name"] if t["pk"] else None,
            "sk": t["sk"]["name"] if t["sk"] else None,
            "gsis": len(t["gsis"]),
            "entities": len(t["entities"]),
            "access_patterns": len(t["access_patterns"]),
            "relations": len(t["relations"]),
            "attributes": len(t["attributes"]),
            "file": t["file"],
        }
        for t in sorted(idx.tables.values(), key=lambda x: x["name"])
    ]
    return {"items": items, "total": len(items), "scanned_files": len(idx.dm_files)}


@_tool
def dm_table(name: str) -> dict[str, Any]:
    """Full record of one table (keys, GSIs, TTL, stream, entities, attributes, relations,
    access patterns) plus inbound relations (other tables pointing here) and outbound
    relations (this table pointing elsewhere)."""
    idx = _idx()
    t = idx.tables.get(name)
    if t is None:
        raise SpecError("not_found", f"table {name!r} not found", available=sorted(idx.tables)[:50])
    rels = dm.all_relations(idx.tables)
    return {
        "table": t,
        "outbound": [r for r in rels if r["from_table"] == name],
        "inbound": [r for r in rels if r["to_table"] == name],
    }


@_tool
def dm_relations(table: str | None = None, entity: str | None = None, format: str = "json") -> dict[str, Any]:
    """Pseudo-relations between tables (DynamoDB has no foreign keys; these are the links
    the code relies on). Filter by table (either end) or by entity. format="mermaid"
    returns an erDiagram you can paste into docs."""
    idx = _idx()
    if format not in ("json", "mermaid"):
        raise SpecError("validation", 'format must be "json" or "mermaid"')
    rels = dm.all_relations(idx.tables)
    if table:
        rels = [r for r in rels if table in (r["from_table"], r["to_table"])]
    if entity:
        owners = {t["name"] for t in idx.tables.values() if entity in t["entities"]}
        rels = [r for r in rels if r["from_table"] in owners or r["to_table"] in owners]
    out: dict[str, Any] = {"total": len(rels), "denominator": _dm_denominator(idx)}
    if format == "mermaid":
        out["mermaid"] = dm.mermaid(rels)
    else:
        out["items"] = rels
    return out


@_tool
def dm_access_patterns(
    table: str | None = None, index: str | None = None, operation: str | None = None, repository: str | None = None
) -> dict[str, Any]:
    """Access patterns across tables with the repository method that implements each.
    Filters: table, index (GSI name), operation (case-insensitive), repository (substring)."""
    idx = _idx()
    pats = dm.all_patterns(idx.tables)
    if table:
        pats = [p for p in pats if p["table"] == table]
    if index:
        pats = [p for p in pats if p["index"] == index]
    if operation:
        pats = [p for p in pats if p["operation"].lower() == operation.lower()]
    if repository:
        pats = [p for p in pats if repository in p["repository"]]
    return {"items": pats, "total": len(pats), "denominator": _dm_denominator(idx)}


_DM_WHERE = ("table", "entity", "attribute", "index", "operation", "kind")


@_tool
def dm_query(where: dict[str, str]) -> dict[str, Any]:
    """Generic filter over the data model. `where` keys: table, entity, attribute, index,
    operation, kind. Returns the matching tables, attributes, access patterns and
    relations together with counts and the denominator."""
    idx = _idx()
    bad = [k for k in where if k not in _DM_WHERE]
    if bad:
        raise SpecError("validation", f"unknown where keys: {bad}; allowed: {list(_DM_WHERE)}")
    tables = list(idx.tables.values())
    if where.get("table"):
        tables = [t for t in tables if t["name"] == where["table"]]
    if where.get("entity"):
        tables = [t for t in tables if where["entity"] in t["entities"]]
    if where.get("attribute"):
        tables = [t for t in tables if where["attribute"] in dm.table_attr_names(t)]
    if where.get("index"):
        tables = [t for t in tables if any(g["name"] == where["index"] for g in t["gsis"])]
    names = {t["name"] for t in tables}
    attrs = [{**a, "table": t["name"]} for t in tables for a in t["attributes"]]
    if where.get("attribute"):
        attrs = [a for a in attrs if a["name"] == where["attribute"]]
    pats = [p for p in dm.all_patterns(idx.tables) if p["table"] in names]
    if where.get("index"):
        pats = [p for p in pats if p["index"] == where["index"]]
    if where.get("operation"):
        pats = [p for p in pats if p["operation"].lower() == where["operation"].lower()]
    rels = dm.all_relations(idx.tables)
    if where.get("table"):
        rels = [r for r in rels if where["table"] in (r["from_table"], r["to_table"])]
    elif where.get("entity") or where.get("attribute") or where.get("index"):
        rels = [r for r in rels if r["from_table"] in names or r["to_table"] in names]
    if where.get("kind"):
        rels = [r for r in rels if r["kind"] == where["kind"]]
    if where.get("attribute"):
        rels = [r for r in rels if r["from"].endswith("." + where["attribute"]) or r["to"].endswith("." + where["attribute"])]
    return {
        "where": where,
        "tables": [{"name": t["name"], "pk": t["pk"], "sk": t["sk"], "entities": t["entities"], "gsis": [g["name"] for g in t["gsis"]]} for t in tables],
        "attributes": attrs,
        "access_patterns": pats,
        "relations": rels,
        "counts": {"tables": len(tables), "attributes": len(attrs), "access_patterns": len(pats), "relations": len(rels)},
        "denominator": _dm_denominator(idx),
    }


_DM_BY = ("table", "index", "entity", "operation", "repository", "kind")
_DM_TABLE_METRICS = ("count", "gsis", "attributes", "relations", "patterns")


@_tool
def dm_aggregate(by: str, metric: str = "count") -> dict[str, Any]:
    """Counts grouped by table / index / entity / operation / repository / kind.
    metric="count" counts access patterns (by table, index, operation, repository),
    tables (by entity) or relations (by kind). With by="table" the metric may also be
    "gsis", "attributes", "relations" or "patterns"."""
    idx = _idx()
    if by not in _DM_BY:
        raise SpecError("validation", f"by must be one of {list(_DM_BY)}")
    if metric != "count" and (by != "table" or metric not in _DM_TABLE_METRICS):
        raise SpecError("validation", f'metric {metric!r} is only valid with by="table" ({list(_DM_TABLE_METRICS)})')
    buckets: dict[str, int] = {}
    pats = dm.all_patterns(idx.tables)
    rels = dm.all_relations(idx.tables)
    if by == "table":
        key = {"count": "access_patterns", "patterns": "access_patterns", "gsis": "gsis", "attributes": "attributes", "relations": "relations"}[metric]
        buckets = {t["name"]: len(t[key]) for t in idx.tables.values()}
        counted = "access_patterns" if key == "access_patterns" else key
    elif by == "index":
        for p in pats:
            k = p["index"] or "(table)"
            buckets[k] = buckets.get(k, 0) + 1
        counted = "access_patterns"
    elif by == "operation":
        for p in pats:
            buckets[p["operation"]] = buckets.get(p["operation"], 0) + 1
        counted = "access_patterns"
    elif by == "repository":
        for p in pats:
            k = p["repository_file"] or "(none)"
            buckets[k] = buckets.get(k, 0) + 1
        counted = "access_patterns"
    elif by == "entity":
        for t in idx.tables.values():
            for e in t["entities"]:
                buckets[e] = buckets.get(e, 0) + 1
        counted = "tables"
    else:
        for r in rels:
            buckets[r["kind"]] = buckets.get(r["kind"], 0) + 1
        counted = "relations"
    return {"by": by, "metric": metric, "counted": counted, "buckets": dict(sorted(buckets.items())), "denominator": _dm_denominator(idx)}


@_tool
def dm_validate() -> dict[str, Any]:
    """Data-model lint: tables with more than datamodel.max_gsis_per_table GSIs, Scan access
    patterns, patterns naming an index the table lacks, relations to unknown tables or
    attributes, GSI keys missing from `attributes`, repository files that do not exist."""
    idx = _idx()
    findings = dm.validate(idx.tables, idx.tables_raw_count, int(idx.cfg["datamodel"]["max_gsis_per_table"]), idx.root)
    return {"findings": findings, "total": len(findings), "checked": _dm_denominator(idx)}


def _dm_owner_file(idx: SpecIndex, name: str) -> Path:
    t = idx.tables.get(name)
    return idx.resolve(t["file"]) if t else idx.resolve(idx.cfg["datamodel"]["write_file"])


@_tool
def dm_upsert_table(table: dict[str, Any]) -> dict[str, Any]:
    """Create or update a table record. `table` needs at least `name`; other fields are
    merged over the existing record (lists replace, scalars overwrite). Written to the
    JSON file that owns the table, or datamodel.write_file for a new table."""
    idx = _idx()
    if not isinstance(table, dict) or not table.get("name"):
        raise SpecError("validation", "table.name is required")
    name = str(table["name"])
    path = _dm_owner_file(idx, name)
    raw = dm.read_raw_tables(path)
    action = "created"
    for i, existing in enumerate(raw):
        if existing.get("name") == name:
            raw[i] = {**existing, **table}
            action = "updated"
            break
    else:
        raw.append(table)
    dm.write_tables(path, raw)
    idx.invalidate()
    idx.ensure(force=True)
    return {"action": action, "table": name, "file": rel(path, idx.root)}


@_tool
def dm_upsert_pattern(table: str, pattern: dict[str, Any]) -> dict[str, Any]:
    """Add or replace one access pattern (matched by `pattern.name`) on an existing table,
    written back to the table's JSON file. `pattern.operation` must be one of Query,
    GetItem, BatchGet, Put, Update, Delete, Transact, Scan."""
    idx = _idx()
    if table not in idx.tables:
        raise SpecError("not_found", f"table {table!r} not found", available=sorted(idx.tables)[:50])
    if not isinstance(pattern, dict) or not pattern.get("name"):
        raise SpecError("validation", "pattern.name is required")
    op = str(pattern.get("operation") or "")
    if op not in dm.OPERATIONS:
        raise SpecError("validation", f"pattern.operation must be one of {list(dm.OPERATIONS)}")
    path = _dm_owner_file(idx, table)
    raw = dm.read_raw_tables(path)
    rec = next(t for t in raw if t.get("name") == table)
    pats = list(rec.get("access_patterns") or [])
    action = "created"
    for i, p in enumerate(pats):
        if isinstance(p, dict) and p.get("name") == pattern["name"]:
            pats[i] = {**p, **pattern}
            action = "updated"
            break
    else:
        pats.append(pattern)
    rec["access_patterns"] = pats
    dm.write_tables(path, raw)
    idx.invalidate()
    idx.ensure(force=True)
    return {"action": action, "table": table, "pattern": pattern["name"], "file": rel(path, idx.root)}


# ---------------------------------------------------------------- UI components


def _ui_patterns(idx: SpecIndex) -> list[str]:
    return list(idx.cfg["components"].get("expected_files") or [])


@_tool
def ui_components(category: str | None = None, limit: int = 200) -> dict[str, Any]:
    """List registered UI components: name, category, import line, number of props and
    how many of the expected companion files (tsx, test, e2e, stories, md, registry,
    index) exist on disk. Ask this before writing a new component."""
    idx = _idx()
    pats = _ui_patterns(idx)
    items = []
    for c in sorted(idx.components.values(), key=lambda x: x["name"]):
        if category and c["category"] != category:
            continue
        pres = fe.file_presence(c, pats, idx.root)
        items.append({
            "name": c["name"], "category": c["category"], "import": c["import"], "props": len(c["props"]),
            "files": {"present": sum(pres.values()), "expected": len(pres)}, "file": c["file"],
        })
    return {"items": items[: max(1, limit)], "total": len(items), "scanned_files": len(idx.component_files)}


@_tool
def ui_component(name: str) -> dict[str, Any]:
    """Full registry record of one component (props, variants, tokensUsed, a11y, examples,
    dependsOn) plus which expected files exist on disk, the missing ones, and the path of
    its markdown doc when present."""
    idx = _idx()
    c = idx.components.get(name)
    if c is None:
        raise SpecError("not_found", f"component {name!r} not found", available=sorted(idx.components)[:80])
    pres = fe.file_presence(c, _ui_patterns(idx), idx.root)
    md = f"{c['dir']}/{c['name']}.md"
    return {
        "component": c,
        "files": pres,
        "missing": [f for f, ok in pres.items() if not ok],
        "md": md if (idx.root / md).is_file() else None,
    }


@_tool
def ui_find(query: str, limit: int = 10) -> dict[str, Any]:
    """"Is there already a component for X?" — BM25 over component names, descriptions,
    categories, prop names/descriptions and example titles. Returns ranked hits with the
    import line, and how many components were searched."""
    idx = _idx()
    from .bm25 import BM25

    comps = sorted(idx.components.values(), key=lambda x: x["name"])
    engine = BM25([tokenize(fe.component_text(c)) for c in comps])
    hits = []
    for i, score in engine.search(tokenize(query), limit=max(1, min(limit, 50))):
        c = comps[i]
        hits.append({"name": c["name"], "category": c["category"], "import": c["import"], "score": round(score, 3), "description": c["description"][:200]})
    return {"query": query, "hits": hits, "searched": len(comps)}


@_tool
def ui_validate() -> dict[str, Any]:
    """Registry lint: expected companion files missing on disk, tokensUsed not defined as
    --token in tokens_css, components on disk missing from registry/index.json and index
    entries with no registry file, dependsOn naming unknown components."""
    idx = _idx()
    pats = _ui_patterns(idx)
    findings = fe.validate_components(idx.components, idx.index_names, idx.tokens, pats, idx.root)
    return {
        "findings": findings,
        "total": len(findings),
        "checked": {"components": len(idx.components), "index_entries": len(idx.index_names), "tokens": len(idx.tokens), "expected_files": len(idx.components) * len(pats)},
    }


_UI_BY = ("category", "token", "dependsOn")


@_tool
def ui_aggregate(by: str) -> dict[str, Any]:
    """Count components by category, by design token used, or by dependency."""
    idx = _idx()
    if by not in _UI_BY:
        raise SpecError("validation", f"by must be one of {list(_UI_BY)}")
    buckets: dict[str, int] = {}
    for c in idx.components.values():
        keys = [c["category"] or "(none)"] if by == "category" else (c["tokensUsed"] if by == "token" else c["dependsOn"])
        for k in keys:
            buckets[k] = buckets.get(k, 0) + 1
    return {"by": by, "buckets": dict(sorted(buckets.items())), "denominator": {"components": len(idx.components)}}


# ---------------------------------------------------------------- designs


@_tool
def design_list(status: str | None = None) -> dict[str, Any]:
    """List design records (one per view): id, title, route, status, roles, counts of
    components/artboards/requirements, artifact_url."""
    idx = _idx()
    if status and status not in fe.STATUSES:
        raise SpecError("validation", f"status must be one of {list(fe.STATUSES)}")
    items = []
    for d in sorted(idx.designs.values(), key=lambda x: x["id"]):
        if status and d["status"] != status:
            continue
        items.append({
            "id": d["id"], "title": d["title"], "route": d["route"], "status": d["status"], "roles": d["roles"],
            "components": len(d["components"]), "artboards": len(d["artboards"]), "requirements": len(d["requirements"]),
            "artifact_url": d["artifact_url"], "file": d["file"],
        })
    return {"items": items, "total": len(items), "scanned_files": len(idx.design_files)}


@_tool
def design_get(id: str) -> dict[str, Any]:
    """Full design record of one view."""
    idx = _idx()
    d = idx.designs.get(id)
    if d is None:
        raise SpecError("not_found", f"design {id!r} not found", available=sorted(idx.designs)[:80])
    return {"design": d, "file": d["file"]}


@_tool
def design_upsert(record: dict[str, Any]) -> dict[str, Any]:
    """Create or update a design record, merged by id over the existing file
    (designs.write_dir/<id>.json), written as pretty JSON with a stable key order.
    `status` must be brief | designed | specified | implemented | verified."""
    idx = _idx()
    if not isinstance(record, dict) or not record.get("id"):
        raise SpecError("validation", "record.id is required")
    did = str(record["id"])
    if not re.fullmatch(r"[a-z0-9][a-z0-9\-]*", did):
        raise SpecError("validation", "id must be a kebab-case slug (a-z, 0-9, '-')")
    if "status" in record and record["status"] not in fe.STATUSES:
        raise SpecError("validation", f"status must be one of {list(fe.STATUSES)}")
    existing = idx.designs.get(did)
    path = idx.resolve(existing["file"]) if existing else idx.resolve(idx.cfg["designs"]["write_dir"]) / f"{did}.json"
    base = {k: v for k, v in (existing or {}).items() if k != "file"}
    merged = {**base, **record}
    fe.write_design(path, merged)
    idx.invalidate()
    idx.ensure(force=True)
    return {"action": "updated" if existing else "created", "id": did, "file": rel(path, idx.root), "status": merged.get("status", "brief")}


@_tool
def design_validate(id: str | None = None) -> dict[str, Any]:
    """Design lint: duotio-ui components missing from the registry; feature/new-duotio-ui
    components without a path; unknown requirement ids; missing artifact_url and missing
    390 light+dark artboards once status >= designed; empty acceptance once >= specified;
    i18n_keys absent from messages_file once >= implemented."""
    idx = _idx()
    designs = list(idx.designs.values())
    if id is not None:
        if id not in idx.designs:
            raise SpecError("not_found", f"design {id!r} not found", available=sorted(idx.designs)[:80])
        designs = [idx.designs[id]]
    messages = fe.load_messages(idx.resolve(idx.cfg["designs"]["messages_file"]))
    findings = fe.validate_designs(designs, idx.components, set(idx.requirements), messages)
    return {
        "findings": findings,
        "total": len(findings),
        "checked": {"designs": len(designs), "components": len(idx.components), "requirements": len(idx.requirements), "messages_loaded": messages is not None},
    }


@_tool
def design_coverage() -> dict[str, Any]:
    """Counts of design records per status and of design components by source
    (duotio-ui / feature / new-duotio-ui), with denominators."""
    idx = _idx()
    by_status = {s: 0 for s in fe.STATUSES}
    by_source: dict[str, int] = {}
    n_comp = 0
    for d in idx.designs.values():
        by_status[d["status"]] = by_status.get(d["status"], 0) + 1
        for c in d["components"]:
            n_comp += 1
            by_source[c["source"]] = by_source.get(c["source"], 0) + 1
    return {
        "by_status": by_status,
        "components_by_source": dict(sorted(by_source.items())),
        "denominator": {"designs": len(idx.designs), "design_components": n_comp, "registry_components": len(idx.components)},
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
