"""Parsers for the three collections. Everything format-related is driven by regexes
passed in from config; nothing here knows about any particular project."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_CODE_REF = re.compile(
    r"(?<![\w/])([\w\-./]+\.(?:tsx?|jsx?|mjs|cjs|py|go|java|kt|rb|rs|tf|ya?ml|json|sql|md|cs))"
    r"(:\d+(?:-\d+)?)?(?![\w])"
)
_TEST_HINT = re.compile(r"(\.spec\.|\.test\.|/tests?/|__tests__|e2e|\.e2e\.)", re.IGNORECASE)
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_NUMBERED = re.compile(r"^\s*(\d+(?:\.\d+)*)[.)]\s+(.*)$")


def rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower())
    return s.strip("-") or "untitled"


def _is_table_row(line: str) -> bool:
    return line.lstrip().startswith("|")


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells)


def evidence_in(text: str) -> list[str]:
    out: list[str] = []
    for m in _CODE_REF.finditer(text):
        ref = m.group(1) + (m.group(2) or "")
        if ref not in out:
            out.append(ref)
    return out


def is_test_evidence(ref: str) -> bool:
    return bool(_TEST_HINT.search(ref))


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Tiny YAML-subset frontmatter parser: scalars and inline/bulleted lists."""
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text
    data: dict[str, Any] = {}
    key: str | None = None
    for raw in m.group(1).splitlines():
        if re.match(r"^\s*-\s+", raw) and key is not None:
            data.setdefault(key, [])
            if not isinstance(data[key], list):
                data[key] = []
            data[key].append(re.sub(r"^\s*-\s+", "", raw).strip().strip("\"'"))
            continue
        kv = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", raw)
        if not kv:
            continue
        key, val = kv.group(1), kv.group(2).strip()
        if val.startswith("[") and val.endswith("]"):
            data[key] = [v.strip().strip("\"'") for v in val[1:-1].split(",") if v.strip()]
        elif val == "":
            data[key] = []
        else:
            data[key] = val.strip("\"'")
    return data, text[m.end():]


# ---------------------------------------------------------------- requirements


def parse_requirements(path: Path, id_re: re.Pattern[str], status_re: re.Pattern[str], root: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    file = rel(path, root)
    domain = path.stem
    out: list[dict[str, Any]] = []
    for n, line in enumerate(text.splitlines(), start=1):
        ids = [m.group(0) for m in id_re.finditer(line)]
        if not ids:
            continue
        if _is_table_row(line):
            cells = _cells(line)
            if _is_separator(cells):
                continue
            body_cells = [c for c in cells if c and c != ids[0]]
            body = " · ".join(body_cells)
        else:
            body = _BULLET.sub("", line)
            body = _HEADING.sub(lambda m: m.group(2), body)
        body = body.replace(ids[0], "", 1)
        body = re.sub(r"^[\s·:|\-–—]+", "", body).strip()
        status_m = status_re.search(line)
        refs: list[str] = []
        for i in ids[1:]:
            if i != ids[0] and i not in refs:
                refs.append(i)
        out.append(
            {
                "id": ids[0],
                "file": file,
                "line": n,
                "text": body,
                "status": status_m.group(1) if status_m and status_m.groups() else (status_m.group(0) if status_m else None),
                "domain": domain,
                "refs": refs,
                "evidence": evidence_in(line),
                "format": "md",
                "title": body[:80],
                "tags": [],
                "acceptance": [],
                "links": {"flows": [], "docs": [], "tests": []},
                "source": {"file": file, "line": n},
            }
        )
    return out


# ---------------------------------------------------------------- flows


_STEP_COLS = {
    "n": ("#", "n", "no", "no.", "nr", "lp", "step #", "id"),
    "role": ("role", "rola", "actor", "aktor", "who", "kto"),
    "screen": ("screen", "ekran", "view", "widok", "page", "strona", "where", "gdzie"),
    "action": ("action", "akcja", "step", "krok", "description", "opis", "what", "co"),
    "evidence": ("evidence", "dowód", "dowod", "proof", "source", "źródło", "zrodlo", "ref"),
}


def _col_map(header: list[str]) -> dict[str, int]:
    low = [h.strip().lower() for h in header]
    m: dict[str, int] = {}
    for field, names in _STEP_COLS.items():
        for i, h in enumerate(low):
            if h in names or any(h.startswith(x) for x in names if len(x) > 2):
                if i not in m.values():
                    m[field] = i
                    break
    if "n" not in m and low and low[0] not in [low[i] for i in m.values()]:
        m["n"] = 0
    if "action" not in m:
        free = [i for i in range(len(low)) if i not in m.values()]
        if free:
            m["action"] = free[0]
    if "evidence" not in m and len(low) > 2 and (len(low) - 1) not in m.values():
        m["evidence"] = len(low) - 1
    return m


def _steps_from_lines(lines: list[str]) -> list[dict[str, str]]:
    # first markdown table
    i = 0
    while i < len(lines):
        if _is_table_row(lines[i]):
            header = _cells(lines[i])
            j = i + 1
            if j < len(lines) and _is_table_row(lines[j]) and _is_separator(_cells(lines[j])):
                cm = _col_map(header)
                steps: list[dict[str, str]] = []
                k = j + 1
                while k < len(lines) and _is_table_row(lines[k]):
                    cells = _cells(lines[k])
                    step = {f: (cells[idx] if idx < len(cells) else "") for f, idx in cm.items()}
                    for f in ("n", "role", "screen", "action", "evidence"):
                        step.setdefault(f, "")
                    if not step["n"]:
                        step["n"] = str(len(steps) + 1)
                    steps.append(step)
                    k += 1
                return steps
            i = j
            continue
        i += 1
    # fallback: numbered list
    steps = []
    for ln in lines:
        m = _NUMBERED.match(ln)
        if m:
            steps.append({"n": m.group(1), "role": "", "screen": "", "action": m.group(2).strip(), "evidence": ""})
    return steps


def parse_flows(path: Path, heading_re: re.Pattern[str], id_re: re.Pattern[str], root: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    file = rel(path, root)
    fm, body = parse_frontmatter(text)
    if fm.get("id"):
        # whole file is one flow (the format flow_upsert writes)
        lines = body.splitlines()
        offset = text[: len(text) - len(body)].count("\n")
        ids = [m.group(0) for m in id_re.finditer(body)]
        reqs = list(fm.get("requirements") or []) if isinstance(fm.get("requirements"), list) else []
        for i in ids:
            if i not in reqs:
                reqs.append(i)
        return [
            {
                "id": str(fm["id"]),
                "title": str(fm.get("title") or fm["id"]),
                "file": file,
                "line": 1,
                "roles": list(fm.get("roles") or []) if isinstance(fm.get("roles"), list) else [],
                "steps": _steps_from_lines(lines),
                "requirements": reqs,
                "line_end": offset + len(lines),
                "format": "md",
            }
        ]
    lines = text.splitlines()
    sections: list[tuple[int, str]] = []
    for n, line in enumerate(lines, start=1):
        m = heading_re.match(line)
        if m:
            title = (m.group(1) if m.groups() else _HEADING.sub(lambda h: h.group(2), line)).strip()
            sections.append((n, title))
    flows: list[dict[str, Any]] = []
    for idx, (start, title) in enumerate(sections):
        end = sections[idx + 1][0] - 1 if idx + 1 < len(sections) else len(lines)
        sec_lines = lines[start:end]  # body after heading
        sec_text = "\n".join(sec_lines)
        reqs: list[str] = []
        for m in id_re.finditer(sec_text):
            if m.group(0) not in reqs:
                reqs.append(m.group(0))
        flows.append(
            {
                "id": slugify(title),
                "title": title,
                "file": file,
                "line": start,
                "line_end": end,
                "roles": [],
                "steps": _steps_from_lines(sec_lines),
                "requirements": reqs,
                "format": "md",
            }
        )
    return flows


# ---------------------------------------------------------------- docs


def chunk_doc(path: Path, root: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    file = rel(path, root)
    lines = text.splitlines()
    heads: list[tuple[int, int, str]] = []
    in_fence = False
    for n, line in enumerate(lines, start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING.match(line)
        if m:
            heads.append((n, len(m.group(1)), m.group(2).strip()))
    chunks: list[dict[str, Any]] = []
    first = heads[0][0] if heads else len(lines) + 1
    pre = "\n".join(lines[: first - 1]).strip()
    if pre:
        chunks.append({"file": file, "heading": "", "level": 0, "line": 1, "line_end": first - 1, "text": pre})
    for i, (n, level, heading) in enumerate(heads):
        end = heads[i + 1][0] - 1 if i + 1 < len(heads) else len(lines)
        body = "\n".join(lines[n:end]).strip()
        chunks.append({"file": file, "heading": heading, "level": level, "line": n, "line_end": end, "text": body})
    return chunks


def doc_title(path: Path) -> str:
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for _ in range(40):
                line = fh.readline()
                if not line:
                    break
                m = _HEADING.match(line)
                if m and len(m.group(1)) == 1:
                    return m.group(2).strip()
    except OSError:
        pass
    return path.stem


# ---------------------------------------------------------------- JSON collections

REQ_KEYS = ("id", "title", "text", "status", "domain", "tags", "acceptance", "links", "source")
FLOW_KEYS = ("id", "title", "roles", "requirements", "steps", "source")
STEP_KEYS = ("n", "role", "screen", "action", "evidence")


def _as_list(v: Any) -> list[Any]:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def empty_links() -> dict[str, list[str]]:
    return {"flows": [], "docs": [], "tests": []}


def norm_links(v: Any) -> dict[str, list[str]]:
    out = empty_links()
    if isinstance(v, dict):
        for k in out:
            out[k] = [str(x) for x in _as_list(v.get(k))]
    return out


def load_json_items(path: Path) -> list[dict[str, Any]]:
    """A JSON collection file is {"items": [...]} or a bare array. Non-dict items are skipped."""
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("items", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict) and i.get("id")]


def norm_requirement(item: dict[str, Any], *, file: str, domain_default: str, id_re: re.Pattern[str]) -> dict[str, Any]:
    """Normalize a JSON requirement item to the canonical schema (also the index record)."""
    text = str(item.get("text") or item.get("title") or "")
    acceptance = [str(a) for a in _as_list(item.get("acceptance"))]
    links = norm_links(item.get("links"))
    blob = " ".join([text, *acceptance, *links["tests"], *links["docs"]])
    refs = [m.group(0) for m in id_re.finditer(" ".join([text, *acceptance]))]
    refs = [r for i, r in enumerate(refs) if r != item["id"] and r not in refs[:i]]
    src = item.get("source") if isinstance(item.get("source"), dict) else {}
    return {
        "id": str(item["id"]),
        "title": str(item.get("title") or text[:80]),
        "text": text,
        "status": (str(item["status"]) if item.get("status") else None),
        "domain": str(item.get("domain") or domain_default),
        "tags": [str(t) for t in _as_list(item.get("tags"))],
        "acceptance": acceptance,
        "links": links,
        "source": {"file": str(src.get("file") or file), "line": src.get("line")},
        "file": file,
        "line": src.get("line"),
        "format": "json",
        "refs": refs,
        "evidence": sorted(set(evidence_in(blob)) | set(links["tests"])),
    }


def norm_step(s: Any, n: int) -> dict[str, str]:
    if not isinstance(s, dict):
        return {"n": str(n), "role": "", "screen": "", "action": str(s), "evidence": ""}
    out = {k: str(s.get(k, "") or "") for k in STEP_KEYS}
    out["n"] = out["n"] or str(n)
    return out


def norm_flow(item: dict[str, Any], *, file: str, id_re: re.Pattern[str]) -> dict[str, Any]:
    steps = [norm_step(s, i) for i, s in enumerate(_as_list(item.get("steps")), start=1)]
    reqs = [str(r) for r in _as_list(item.get("requirements"))]
    for s in steps:
        for m in id_re.finditer(s["action"] + " " + s["evidence"]):
            if m.group(0) not in reqs:
                reqs.append(m.group(0))
    src = item.get("source") if isinstance(item.get("source"), dict) else {}
    return {
        "id": str(item["id"]),
        "title": str(item.get("title") or item["id"]),
        "file": file,
        "line": src.get("line"),
        "line_end": None,
        "roles": [str(r) for r in _as_list(item.get("roles"))],
        "steps": steps,
        "requirements": reqs,
        "source": {"file": str(src.get("file") or file), "line": src.get("line")},
        "format": "json",
    }


def parse_requirements_json(path: Path, id_re: re.Pattern[str], root: Path) -> list[dict[str, Any]]:
    file = rel(path, root)
    return [norm_requirement(i, file=file, domain_default=path.stem, id_re=id_re) for i in load_json_items(path)]


def parse_flows_json(path: Path, id_re: re.Pattern[str], root: Path) -> list[dict[str, Any]]:
    file = rel(path, root)
    return [norm_flow(i, file=file, id_re=id_re) for i in load_json_items(path)]


def md_requirement_to_item(r: dict[str, Any], links: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Markdown-parsed requirement -> JSON item schema (for spec_import_markdown)."""
    return {
        "id": r["id"],
        "title": r["text"][:80],
        "text": r["text"],
        "status": r.get("status"),
        "domain": r["domain"],
        "tags": [],
        "acceptance": [],
        "links": norm_links(links) if links else empty_links(),
        "source": {"file": r["file"], "line": r["line"]},
    }


def md_flow_to_item(f: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f["id"],
        "title": f["title"],
        "roles": list(f.get("roles") or []),
        "requirements": list(f["requirements"]),
        "steps": [{k: s.get(k, "") for k in STEP_KEYS} for s in f["steps"]],
        "source": {"file": f["file"], "line": f["line"]},
    }
