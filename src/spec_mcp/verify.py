"""Evidence verification: do `file:line` references and symbol anchors in flows and
requirements still point at the code they claim to?

An evidence entry is `{file, line?, line_end?, symbol?}`. Statuses:

- ok                 file found; line in range; symbol (if any) within +-5 lines of `line`
- moved              symbol found, but elsewhere (`new_line` = nearest occurrence)
- symbol_missing     file found, symbol not in it
- file_missing       no candidate file under the evidence roots
- line_out_of_range  file shorter than the cited line
- ambiguous          more than one candidate file (`candidates`)
- wrong_root         the path exists, but outside every evidence root
- invalid_anchor     anchor without a usable `file`
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterator

from .errors import SpecError
from .parse import _CODE_REF, evidence_in

STATUSES = (
    "ok",
    "moved",
    "symbol_missing",
    "file_missing",
    "line_out_of_range",
    "ambiguous",
    "wrong_root",
    "invalid_anchor",
)
SKIP_DIRS = {
    "node_modules",
    ".git",
    "dist",
    ".next",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    ".turbo",
}
SYMBOL_WINDOW = 5
_REF_LINE = re.compile(r"^(?P<file>.+?)(?::(?P<a>\d+)(?:-(?P<b>\d+))?)?$")


def split_ref(ref: str) -> tuple[str, int | None, int | None]:
    m = _REF_LINE.match(ref.strip())
    if not m:
        return ref, None, None
    a = int(m.group("a")) if m.group("a") else None
    b = int(m.group("b")) if m.group("b") else a
    return m.group("file"), a, b


class Resolver:
    """Resolves evidence paths against the evidence roots; caches file lines and a basename index."""

    def __init__(self, root: Path, evidence_roots: list[str]):
        self.root = root
        self.roots = [(root / r).resolve() for r in evidence_roots] or [root.resolve()]
        self._lines: dict[Path, list[str] | None] = {}
        self._names: dict[str, list[Path]] | None = None

    def _under_roots(self, p: Path) -> bool:
        return any(p == r or r in p.parents for r in self.roots)

    def _name_index(self) -> dict[str, list[Path]]:
        if self._names is None:
            names: dict[str, list[Path]] = {}
            for r in self.roots:
                for dirpath, dirs, files in os.walk(r):
                    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                    for f in files:
                        names.setdefault(f, []).append(Path(dirpath) / f)
            self._names = names
        return self._names

    def resolve(self, file: str) -> tuple[str, list[Path]]:
        """Return (state, candidates): state in found | ambiguous | wrong_root | missing."""
        fp = Path(file)
        if fp.is_absolute():
            return (
                ("found", [fp])
                if fp.is_file() and self._under_roots(fp.resolve())
                else (("wrong_root", [fp]) if fp.is_file() else ("missing", []))
            )
        direct: list[Path] = []
        for base in self.roots:
            c = (base / fp).resolve()
            if c.is_file() and c not in direct:
                direct.append(c)
        if len(direct) == 1:
            return "found", direct
        if len(direct) > 1:
            return "ambiguous", direct
        outside = (self.root / fp).resolve()
        if outside.is_file() and not self._under_roots(outside):
            return "wrong_root", [outside]
        suffix = "/" + fp.as_posix().lstrip("/")
        hits = [
            p
            for p in self._name_index().get(fp.name, [])
            if p.as_posix().endswith(suffix) or fp.name == fp.as_posix()
        ]
        if len(hits) == 1:
            return "found", hits
        if len(hits) > 1:
            return "ambiguous", hits
        return "missing", []

    def lines(self, p: Path) -> list[str]:
        if p not in self._lines:
            try:
                self._lines[p] = p.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
            except OSError:
                self._lines[p] = None
        return self._lines[p] or []

    def rel(self, p: Path) -> str:
        try:
            return p.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return p.as_posix()


def symbol_lines(lines: list[str], symbol: str) -> list[int]:
    pat = re.compile(r"(?<![\w$])" + re.escape(symbol) + r"(?![\w$])")
    return [i for i, ln in enumerate(lines, start=1) if pat.search(ln)]


def check_entry(res: Resolver, e: dict[str, Any]) -> dict[str, Any]:
    """Classify one evidence entry. Adds status, resolved path, new_line/candidates/unanchored."""
    out = dict(e)
    file = e.get("file")
    if not file or not isinstance(file, str):
        out["status"] = "invalid_anchor"
        return out
    state, cands = res.resolve(file)
    if state == "missing":
        out["status"] = "file_missing"
        return out
    if state == "wrong_root":
        out["status"] = "wrong_root"
        out["candidates"] = [res.rel(c) for c in cands]
        return out
    if state == "ambiguous":
        out["status"] = "ambiguous"
        out["candidates"] = [res.rel(c) for c in cands]
        return out
    path = cands[0]
    out["resolved"] = res.rel(path)
    lines = res.lines(path)
    line, line_end, symbol = (
        e.get("line"),
        e.get("line_end") or e.get("line"),
        e.get("symbol"),
    )
    if line is not None and (line > len(lines) or (line_end or line) > len(lines)):
        out["status"] = "line_out_of_range"
        out["file_lines"] = len(lines)
        return out
    if not symbol:
        out["status"] = "ok"
        out["unanchored"] = True
        return out
    found = symbol_lines(lines, symbol)
    if not found:
        out["status"] = "symbol_missing"
        return out
    if line is None:
        out["status"] = "ok"
        out["found_line"] = found[0]
        return out
    if any(abs(f - line) <= SYMBOL_WINDOW for f in found):
        out["status"] = "ok"
        return out
    out["status"] = "moved"
    out["new_line"] = min(found, key=lambda f: (abs(f - line), f))
    return out


# ---------------------------------------------------------------- collecting entries


def _ref_entries(text: str) -> list[dict[str, Any]]:
    out = []
    for ref in evidence_in(text):
        file, a, b = split_ref(ref)
        out.append({"ref": ref, "file": file, "line": a, "line_end": b})
    return out


def _bind(
    refs: list[dict[str, Any]], anchors: list[dict[str, Any]], res: Resolver
) -> list[dict[str, Any]]:
    """Attach anchor symbols to the matching evidence refs (same resolved file); anchors that
    match no ref become entries of their own."""
    entries = [dict(r, source="evidence") for r in refs]
    for a in anchors:
        af = a.get("file")
        match = None
        if isinstance(af, str):
            astate, acands = res.resolve(af)
            for ent in entries:
                if ent.get("symbol"):
                    continue
                if ent["file"] == af:
                    match = ent
                    break
                estate, ecands = res.resolve(ent["file"])
                if estate == "found" and astate == "found" and ecands[0] == acands[0]:
                    match = ent
                    break
        if match is not None and a.get("symbol"):
            match["symbol"] = a["symbol"]
            match["anchor"] = a
            if match.get("line") is None:
                match["line"] = a.get("line")
                match["line_end"] = a.get("line")
        elif match is not None:
            match["anchor"] = a
        else:
            entries.append(
                {
                    "ref": None,
                    "file": a.get("file"),
                    "line": a.get("line"),
                    "line_end": a.get("line"),
                    "symbol": a.get("symbol"),
                    "source": "anchor",
                    "anchor": a,
                }
            )
    return entries


def flow_entries(item: dict[str, Any], res: Resolver) -> Iterator[dict[str, Any]]:
    """Evidence entries of one raw JSON flow (steps + flow-level async), tagged with `where`."""
    for si, step in enumerate(item.get("steps") or []):
        if not isinstance(step, dict):
            continue
        n = step.get("n") or si + 1
        for ent in _bind(
            _ref_entries(str(step.get("evidence") or "")),
            step.get("anchors") or [],
            res,
        ):
            ent.update(where=f"step {n}", container=("steps", si))
            yield ent
    for ai, a in enumerate(item.get("async") or []):
        if not isinstance(a, dict):
            continue
        for ent in _bind(_ref_entries(str(a.get("evidence") or "")), [], res):
            ent.update(where=f"async {a.get('n') or ai + 1}", container=("async", ai))
            yield ent


def requirement_entries(
    item: dict[str, Any], res: Resolver
) -> Iterator[dict[str, Any]]:
    blob = " ".join(
        [str(item.get("text") or ""), *[str(a) for a in (item.get("acceptance") or [])]]
    )
    refs = _ref_entries(blob)
    for extra in item.get("evidence") or []:
        if isinstance(extra, str):
            refs.extend(_ref_entries(extra))
        elif isinstance(extra, dict) and extra.get("file"):
            refs.append(
                {
                    "ref": None,
                    "file": extra["file"],
                    "line": extra.get("line"),
                    "line_end": extra.get("line_end") or extra.get("line"),
                    "symbol": extra.get("symbol"),
                }
            )
    seen: set[tuple[Any, ...]] = set()
    uniq = []
    for r in refs:
        key = (r["file"], r.get("line"), r.get("line_end"))
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    for ent in _bind(uniq, item.get("anchors") or [], res):
        ent["where"] = "text"
        yield ent


# ---------------------------------------------------------------- applying fixes


def _shift(ref: str, delta: int) -> str:
    file, a, b = split_ref(ref)
    if a is None:
        return ref
    if b is not None and b != a:
        return f"{file}:{a + delta}-{b + delta}"
    return f"{file}:{a + delta}"


def rewrite_ref(text: str, old_ref: str, new_ref: str) -> str:
    def cb(m: re.Match[str]) -> str:
        ref = m.group(1) + (m.group(2) or "")
        return new_ref if ref == old_ref else m.group(0)

    return _CODE_REF.sub(cb, text)


def apply_fix(item: dict[str, Any], ent: dict[str, Any], kind: str) -> bool:
    """Apply `moved` (or a missing anchor line) to the raw item. Returns True if changed."""
    new_line = ent.get("new_line") or ent.get("found_line")
    if new_line is None:
        return False
    changed = False
    old_line = ent.get("line")
    delta = new_line - old_line if old_line is not None else 0
    anchor = ent.get("anchor")
    if anchor is not None and anchor.get("line") != new_line:
        anchor["line"] = new_line
        changed = True
    if ent.get("ref") and old_line is not None and ent.get("status") == "moved":
        new_ref = _shift(ent["ref"], delta)
        if new_ref != ent["ref"]:
            if kind == "flow":
                kind_key, idx = ent["container"]
                host = item[kind_key][idx]
                host["evidence"] = rewrite_ref(
                    str(host.get("evidence") or ""), ent["ref"], new_ref
                )
            else:
                for key in ("text",):
                    if isinstance(item.get(key), str):
                        item[key] = rewrite_ref(item[key], ent["ref"], new_ref)
                if isinstance(item.get("acceptance"), list):
                    item["acceptance"] = [
                        rewrite_ref(a, ent["ref"], new_ref) if isinstance(a, str) else a
                        for a in item["acceptance"]
                    ]
                if isinstance(item.get("evidence"), list):
                    item["evidence"] = [
                        rewrite_ref(a, ent["ref"], new_ref) if isinstance(a, str) else a
                        for a in item["evidence"]
                    ]
            changed = True
    return changed


def load_raw(path: Path) -> tuple[Any, list[dict[str, Any]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise SpecError(
            "validation", f'{path}: expected {{"items": [...]}} or an array'
        )
    return data, items


def write_raw(path: Path, data: Any) -> None:
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise SpecError("io", f"cannot write {path}: {e}", retryable=True) from e


def verify_items(
    kind: str,
    files: list[Path],
    root: Path,
    res: Resolver,
    select: Any,
    fix: bool,
    include_ok: bool,
    limit: int,
) -> dict[str, Any]:
    """Verify every raw JSON item of `files` (kind 'flow' | 'requirement') that `select(item)` accepts."""
    counts = {s: 0 for s in STATUSES}
    items_out: list[dict[str, Any]] = []
    fixed: list[dict[str, Any]] = []
    checked_items = 0
    entries_total = 0
    unanchored = 0
    skipped_files: list[str] = []
    for path in files:
        if path.suffix != ".json":
            skipped_files.append(res.rel(path))
            continue
        try:
            data, raw = load_raw(path)
        except (OSError, json.JSONDecodeError, SpecError):
            skipped_files.append(res.rel(path))
            continue
        dirty = False
        for item in raw:
            if not isinstance(item, dict) or not item.get("id") or not select(item, path):
                continue
            checked_items += 1
            gen = (
                flow_entries(item, res)
                if kind == "flow"
                else requirement_entries(item, res)
            )
            for ent in gen:
                entries_total += 1
                chk = check_entry(res, ent)
                counts[chk["status"]] += 1
                if chk.get("unanchored"):
                    unanchored += 1
                row = {
                    "id": item["id"],
                    "where": chk.get("where"),
                    "file": chk.get("file"),
                    "line": chk.get("line"),
                    "symbol": chk.get("symbol"),
                    "status": chk["status"],
                    "source": chk.get("source"),
                    "json_file": res.rel(path),
                }
                for k in (
                    "new_line",
                    "found_line",
                    "candidates",
                    "resolved",
                    "file_lines",
                ):
                    if chk.get(k) is not None:
                        row[k] = chk[k]
                if (
                    fix
                    and chk["status"] in ("moved",)
                    and apply_fix(item, {**ent, **chk}, kind)
                ):
                    dirty = True
                    row["fixed"] = True
                    fixed.append(
                        {
                            k: row[k]
                            for k in (
                                "id",
                                "where",
                                "file",
                                "line",
                                "new_line",
                                "json_file",
                            )
                            if k in row
                        }
                    )
                if include_ok or chk["status"] != "ok":
                    items_out.append({k: v for k, v in row.items() if v is not None})
        if dirty:
            write_raw(path, data)
    return {
        "checked_items": checked_items,
        "evidence_entries": entries_total,
        "counts": counts,
        "unanchored_ok": unanchored,
        "items": items_out[: max(1, limit)],
        "items_total": len(items_out),
        "truncated": len(items_out) > max(1, limit),
        "fixed": fixed,
        "fixed_total": len(fixed),
        "skipped_files": skipped_files,
        "evidence_roots": [res.rel(r) for r in res.roots],
    }
