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
        "counts": {"docs": idx.doc_files, "requirements": len(idx.requirements), "flows": len(idx.flows)},
        "files": {"requirements": idx.req_files, "flows": idx.flow_files},
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
    directory (one table row per item, marked as generated). Returns counts and paths."""
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
    idx.invalidate()
    idx.ensure(force=True)
    return {"requirements": len(idx.requirements), "flows": len(idx.flows), "docs": idx.doc_files, "written": written}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
