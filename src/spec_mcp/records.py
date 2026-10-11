"""Generic record collections: `{"records": [...]}` JSON files validated by a JSON Schema
(2020-12) per record, configured under `records.<name>` in the config file.

No caching: collections are small and read fresh on every call so a write from another
process is visible immediately. Writers keep file order, an optional top-level `meta`,
2-space indent, UTF-8 without escaping and a trailing newline."""

from __future__ import annotations

import datetime as _dt
import glob
import json
import os
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator, FormatChecker

from .errors import SpecError

_FORMATS = FormatChecker()


@_FORMATS.checks("date", raises=ValueError)
def _check_date(value: object) -> bool:
    if isinstance(value, str):
        _dt.date.fromisoformat(value)
    return True


@_FORMATS.checks("date-time", raises=ValueError)
def _check_datetime(value: object) -> bool:
    if isinstance(value, str):
        _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return True


def _rel(p: Path, root: Path) -> str:
    try:
        return str(p.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(p)


class Collection:
    def __init__(self, name: str, cfg: dict[str, Any], root: Path):
        if not isinstance(cfg, dict):
            raise SpecError("config", f"records.{name} must be an object")
        self.name = name
        self.root = root
        self.globs: list[str] = list(cfg.get("globs") or [])
        self.exclude: list[str] = list(cfg.get("exclude") or [])
        self.schema_path: str | None = cfg.get("schema")
        self.id_field: str = cfg.get("id_field") or "id"
        self.write_file: str | None = cfg.get("write_file")
        if not self.globs and not self.write_file:
            raise SpecError(
                "config", f"records.{name}: `globs` or `write_file` is required"
            )
        self._schema: dict[str, Any] | None = None
        self._validator: Draft202012Validator | None = None

    # ------------------------------------------------------------ paths

    def resolve(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else self.root / path

    def files(self) -> list[Path]:
        seen: dict[str, Path] = {}
        import fnmatch

        for g in self.globs:
            pattern = g if os.path.isabs(g) else str(self.root / g)
            for hit in glob.glob(pattern, recursive=True):
                p = Path(hit)
                if not p.is_file():
                    continue
                r = _rel(p, self.root)
                if any(fnmatch.fnmatch(r, ex) for ex in self.exclude):
                    continue
                seen.setdefault(str(p.resolve()), p)
        if self.write_file:
            wf = self.resolve(self.write_file)
            if wf.is_file():
                seen.setdefault(str(wf.resolve()), wf)
        return sorted(seen.values(), key=lambda p: str(p))

    def target_file(self, file: str | None = None) -> Path:
        if file:
            return self.resolve(file)
        if self.write_file:
            return self.resolve(self.write_file)
        files = self.files()
        if len(files) == 1:
            return files[0]
        if (
            not files
            and len(self.globs) == 1
            and not any(ch in self.globs[0] for ch in "*?[")
        ):
            return self.resolve(self.globs[0])
        raise SpecError(
            "validation",
            f"records.{self.name}: no `write_file` configured and {len(files)} files match — pass `file`",
            files=[_rel(f, self.root) for f in files],
        )

    # ------------------------------------------------------------ schema

    def schema(self) -> dict[str, Any] | None:
        if self._schema is not None or not self.schema_path:
            return self._schema
        p = self.resolve(self.schema_path)
        if not p.is_file():
            raise SpecError(
                "config",
                f"records.{self.name}: schema file not found: {_rel(p, self.root)}",
            )
        try:
            self._schema = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise SpecError(
                "config", f"records.{self.name}: schema is not valid JSON ({e})"
            ) from e
        try:
            Draft202012Validator.check_schema(self._schema)
        except Exception as e:  # jsonschema.SchemaError
            raise SpecError(
                "config",
                f"records.{self.name}: invalid JSON Schema: {getattr(e, 'message', e)}",
            ) from e
        return self._schema

    def validator(self) -> Draft202012Validator | None:
        if self._validator is None:
            s = self.schema()
            if s is None:
                return None
            self._validator = Draft202012Validator(s, format_checker=_FORMATS)
        return self._validator

    def record_errors(self, record: Any) -> list[dict[str, Any]]:
        """Schema + id-field errors for one record (no file context)."""
        errs: list[dict[str, Any]] = []
        if not isinstance(record, dict):
            return [{"json_path": "$", "message": "record must be an object"}]
        if not record.get(self.id_field):
            errs.append(
                {
                    "json_path": f"$.{self.id_field}",
                    "message": f"missing or empty `{self.id_field}`",
                }
            )
        v = self.validator()
        if v is not None:
            for e in sorted(v.iter_errors(record), key=lambda e: list(e.absolute_path)):
                path = "$" + "".join(
                    f"[{p}]" if isinstance(p, int) else f".{p}" for p in e.absolute_path
                )
                errs.append(
                    {"json_path": path, "message": e.message, "keyword": e.validator}
                )
        return errs

    # ------------------------------------------------------------ io

    def read_file(self, path: Path) -> dict[str, Any]:
        """Return the raw document ({"records": [...], ...}). Raises on bad shape."""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            raise SpecError(
                "io", f"cannot read {_rel(path, self.root)}: {e}", retryable=True
            ) from e
        if not text.strip():
            raise SpecError("validation", f"{_rel(path, self.root)}: file is empty")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise SpecError(
                "validation", f"{_rel(path, self.root)}: invalid JSON ({e})"
            ) from e
        if isinstance(data, list):
            data = {"records": data}
        if not isinstance(data, dict) or not isinstance(data.get("records"), list):
            raise SpecError(
                "validation", f'{_rel(path, self.root)}: expected {{"records": [...]}}'
            )
        return data

    def load(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """All records (each with `_file`) and file-level problems."""
        rows: list[dict[str, Any]] = []
        problems: list[dict[str, Any]] = []
        for f in self.files():
            try:
                doc = self.read_file(f)
            except SpecError as e:
                problems.append({"file": _rel(f, self.root), "message": e.message})
                continue
            for i, r in enumerate(doc["records"]):
                row = dict(r) if isinstance(r, dict) else {"_raw": r}
                row["_file"] = _rel(f, self.root)
                row["_index"] = i
                rows.append(row)
        return rows, problems

    def write_file_doc(self, path: Path, doc: dict[str, Any]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        except OSError as e:
            raise SpecError(
                "io", f"cannot write {_rel(path, self.root)}: {e}", retryable=True
            ) from e

    # ------------------------------------------------------------ ops

    def validate(self) -> dict[str, Any]:
        rows, problems = self.load()
        errors: list[dict[str, Any]] = [{"kind": "file", **p} for p in problems]
        try:
            self.schema()
        except SpecError as e:
            errors.append({"kind": "config", "message": e.message})
            return {
                "collection": self.name,
                "checked": len(rows),
                "files": len(self.files()),
                "errors": errors,
            }
        seen: dict[str, str] = {}
        for row in rows:
            rec = {k: v for k, v in row.items() if k not in ("_file", "_index")}
            rid = rec.get(self.id_field)
            loc = {"file": row["_file"], "index": row["_index"], "id": rid}
            if "_raw" in row:
                errors.append(
                    {
                        "kind": "schema",
                        **loc,
                        "json_path": "$",
                        "message": "record must be an object",
                    }
                )
                continue
            for e in self.record_errors(rec):
                errors.append({"kind": "schema", **loc, **e})
            if rid:
                key = str(rid)
                if key in seen:
                    errors.append(
                        {
                            "kind": "duplicate_id",
                            **loc,
                            "message": f"id {key!r} already defined in {seen[key]}",
                        }
                    )
                else:
                    seen[key] = f"{row['_file']}[{row['_index']}]"
        return {
            "collection": self.name,
            "checked": len(rows),
            "files": len(self.files()),
            "errors": errors,
        }

    def upsert(self, record: dict[str, Any], file: str | None = None) -> dict[str, Any]:
        errs = self.record_errors(record)
        if errs:
            raise SpecError(
                "validation",
                f"records.{self.name}: record rejected by schema",
                errors=errs,
            )
        rid = str(record[self.id_field])
        rows, _ = self.load()
        existing = next((r for r in rows if str(r.get(self.id_field)) == rid), None)
        if existing is not None:
            path = self.resolve(existing["_file"])
            doc = self.read_file(path)
            doc["records"][existing["_index"]] = record
            self.write_file_doc(path, doc)
            return {"action": "updated", "id": rid, "file": existing["_file"]}
        path = self.target_file(file)
        doc = self.read_file(path) if path.is_file() else {"records": []}
        doc["records"].append(record)
        self.write_file_doc(path, doc)
        return {"action": "created", "id": rid, "file": _rel(path, self.root)}


# ---------------------------------------------------------------- helpers


def collections(cfg: dict[str, Any]) -> dict[str, Collection]:
    raw = cfg.get("records") or {}
    if not isinstance(raw, dict):
        raise SpecError("config", "`records` must be an object of collections")
    root = Path(cfg["_root"])
    return {name: Collection(name, c, root) for name, c in raw.items()}


def get_collection(cfg: dict[str, Any], name: str) -> Collection:
    cols = collections(cfg)
    if name not in cols:
        raise SpecError(
            "not_found", f"collection {name!r} not configured", available=sorted(cols)
        )
    return cols[name]


def strip_meta(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in ("_file", "_index")}


def matches(row: dict[str, Any], where: dict[str, Any] | None) -> bool:
    for k, want in (where or {}).items():
        have = row.get(k)
        if isinstance(have, list):
            if want not in have and str(want) not in [str(x) for x in have]:
                return False
        elif have != want and str(have) != str(want):
            return False
    return True


def aggregate(
    rows: Iterable[dict[str, Any]], by: str, metric: str = "count"
) -> dict[str, Any]:
    kind, _, field = metric.partition(":")
    if kind not in ("count", "sum", "avg") or (kind != "count" and not field):
        raise SpecError(
            "validation", "metric must be count | sum:<field> | avg:<field>"
        )
    groups: dict[str, list[float]] = {}
    n = 0
    for r in rows:
        n += 1
        keys = r.get(by)
        keys = keys if isinstance(keys, list) else [keys]
        for key in keys or ["(none)"]:
            label = "(none)" if key is None else str(key)
            val = 1.0
            if kind != "count":
                v = r.get(field)
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    continue
                val = float(v)
            groups.setdefault(label, []).append(val)
    if kind == "count":
        out = {k: len(v) for k, v in groups.items()}
    elif kind == "sum":
        out = {k: sum(v) for k, v in groups.items()}
    else:
        out = {k: (sum(v) / len(v)) for k, v in groups.items()}
    return {
        "by": by,
        "metric": metric,
        "groups": dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0]))),
        "denominator": n,
    }


def render_index_section(coll: Collection, rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        f"## {coll.name}",
        "",
        f"_{len(rows)} records in {len({r['_file'] for r in rows})} files._",
        "",
    ]
    if any("status" in r for r in rows):
        dist = aggregate(rows, "status")["groups"]
        lines += ["| Status | Count |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in dist.items()]
        lines.append("")
    lines += ["| ID | Title | Status | File |", "|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: str(r.get(coll.id_field) or "")):
        title = str(r.get("title") or r.get("name") or r.get("text") or "")[
            :100
        ].replace("|", "\\|")
        lines.append(
            f"| {r.get(coll.id_field, '')} | {title} | {r.get('status', '')} | `{Path(r['_file']).name}` |"
        )
    lines.append("")
    return lines
