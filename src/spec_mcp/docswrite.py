"""Writers for documentation status: front matter upsert and supersede-with-archive."""

from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .docscheck import glob_match
from .errors import SpecError
from .parse import _FRONTMATTER, parse_frontmatter

FM_ORDER = (
    "title",
    "status",
    "topic",
    "owner",
    "supersedes",
    "superseded_by",
    "verified_at",
)
LIST_KEYS = {"supersedes"}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_BANNER = re.compile(r"^> ZASTĄPIONY przez ", re.MULTILINE)


def _scalar(v: Any) -> str:
    s = str(v)
    if (
        s == ""
        or re.search(r"[:#\[\]{},&*!|>'\"%@`]", s)
        or s != s.strip()
        or s.lower() in ("null", "true", "false", "yes", "no")
    ):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def dump_frontmatter(fm: dict[str, Any]) -> str:
    keys = [k for k in FM_ORDER if k in fm] + [k for k in fm if k not in FM_ORDER]
    lines = ["---"]
    for k in keys:
        v = fm[k]
        if v is None or v == [] or v == "":
            continue
        if isinstance(v, list):
            lines.append(f"{k}:")
            lines.extend(f"  - {_scalar(x)}" for x in v)
        else:
            lines.append(f"{k}: {_scalar(v)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def validate_frontmatter(fm: dict[str, Any], statuses: list[str]) -> None:
    st = fm.get("status")
    if st is not None and st not in statuses:
        raise SpecError(
            "validation", f"status {st!r} not in {statuses}", allowed=statuses
        )
    va = fm.get("verified_at")
    if va is not None and (
        not isinstance(va, str) or not _DATE.match(va) or _bad_date(va)
    ):
        raise SpecError(
            "validation", f"verified_at must be an ISO date YYYY-MM-DD, got {va!r}"
        )
    for k in LIST_KEYS:
        if k in fm and fm[k] is not None and not isinstance(fm[k], list):
            raise SpecError("validation", f"{k} must be a list of paths")


def _bad_date(s: str) -> bool:
    try:
        dt.date.fromisoformat(s)
        return False
    except ValueError:
        return True


def in_scope(cfg_docs: dict[str, Any], file: str) -> bool:
    inc = any(glob_match(file, g) for g in cfg_docs["globs"])
    exc = any(glob_match(file, g) for g in (cfg_docs.get("exclude") or []))
    return inc and not exc


def split_file(text: str) -> tuple[dict[str, Any], str]:
    fm, body = parse_frontmatter(text)
    for k in LIST_KEYS:
        if k in fm and not isinstance(fm[k], list):
            fm[k] = [fm[k]] if fm[k] else []
    for k, v in list(fm.items()):
        if v == [] and k not in LIST_KEYS:
            fm[k] = None
    return fm, body


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        raise SpecError("io", f"cannot write {path}: {e}", retryable=True) from e


def upsert(
    idx: Any, path: str, front_matter: dict[str, Any] | None, body: str | None
) -> dict[str, Any]:
    cfg = idx.cfg["docs"]
    target = idx.resolve(path)
    file = (
        target.resolve().relative_to(idx.root.resolve()).as_posix()
        if target.resolve().is_relative_to(idx.root.resolve())
        else path
    )
    if not file.endswith(".md"):
        raise SpecError("validation", "docs_upsert writes markdown files only")
    if not in_scope(cfg, file):
        raise SpecError(
            "validation",
            f"{file} is outside docs.globs / inside docs.exclude",
            globs=cfg["globs"],
        )
    existed = target.is_file()
    if existed:
        fm, old_body = split_file(target.read_text(encoding="utf-8", errors="replace"))
    else:
        fm, old_body = {}, None
        if body is None:
            raise SpecError(
                "validation", f"{file} does not exist: pass `body` to create it"
            )
    for k, v in (front_matter or {}).items():
        if v is None:
            fm.pop(k, None)
        else:
            fm[k] = v
    validate_frontmatter(fm, cfg["statuses"])
    new_body = body if body is not None else old_body or ""
    if front_matter is None and body is None:
        raise SpecError(
            "validation", "nothing to write: pass `front_matter` and/or `body`"
        )
    new_body = new_body.lstrip("\n") if fm else new_body
    text = (dump_frontmatter(fm) + "\n" if fm else "") + new_body
    if not text.endswith("\n"):
        text += "\n"
    _atomic_write(target, text)
    idx.invalidate()
    idx.ensure(force=True)
    return {
        "file": file,
        "created": not existed,
        "front_matter": fm,
        "bytes": len(text.encode("utf-8")),
    }


def archive_destination(cfg_docs: dict[str, Any], file: str, topic: str | None) -> str:
    archive = (cfg_docs.get("archive_dir") or "docs/archive").strip("/")
    first_glob = cfg_docs["globs"][0] if cfg_docs.get("globs") else "docs/**/*.md"
    base = []
    for part in first_glob.split("/"):
        if any(c in part for c in "*?[]"):
            break
        base.append(part)
    base_s = "/".join(base).strip("/")
    relp = (
        file[len(base_s) + 1 :]
        if base_s and file.startswith(base_s + "/")
        else Path(file).name
    )
    parts = relp.split("/")
    first = parts[0] if len(parts) > 1 else None
    group = topic or first or "misc"
    remainder = "/".join(parts[1:]) if (first and group == first) else relp
    return f"{archive}/{group}/{remainder}"


def _is_git_tracked(root: Path, file: str) -> bool:
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", file],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def supersede(
    idx: Any, path: str, by: str, reason: str, move_to_archive: bool, dry_run: bool
) -> dict[str, Any]:
    from .docscheck import inbound_links

    cfg = idx.cfg["docs"]
    src = idx.resolve(path)
    if not src.is_file():
        raise SpecError("not_found", f"no such file: {path}")
    file = src.resolve().relative_to(idx.root.resolve()).as_posix()
    if not in_scope(cfg, file):
        raise SpecError(
            "validation", f"{file} is outside docs.globs / inside docs.exclude"
        )
    repl = idx.resolve(by)
    if not repl.exists():
        raise SpecError(
            "not_found", f"replacement {by!r} does not exist; nothing changed"
        )
    by_rel = repl.resolve().relative_to(idx.root.resolve()).as_posix()
    if by_rel == file:
        raise SpecError("validation", "a document cannot supersede itself")
    if not reason.strip():
        raise SpecError("validation", "`reason` is required")
    today = dt.date.today().isoformat()
    fm, body = split_file(src.read_text(encoding="utf-8", errors="replace"))
    fm["status"] = "superseded"
    fm["superseded_by"] = by_rel
    validate_frontmatter(fm, cfg["statuses"])
    banner = f"> ZASTĄPIONY przez `{by_rel}` — {reason.strip()} ({today})\n"
    if _BANNER.search(body):
        body = _BANNER.sub("> ZASTĄPIONY przez ", body, count=1)
        body = re.sub(
            r"^> ZASTĄPIONY przez .*$",
            banner.rstrip("\n"),
            body,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        body = banner + "\n" + body.lstrip("\n")
    new_text = dump_frontmatter(fm) + "\n" + body.lstrip("\n")
    if not new_text.endswith("\n"):
        new_text += "\n"
    dest_rel = (
        archive_destination(cfg, file, fm.get("topic")) if move_to_archive else file
    )
    result: dict[str, Any] = {
        "old_path": file,
        "new_path": dest_rel,
        "status": "superseded",
        "superseded_by": by_rel,
        "moved": dest_rel != file,
        "dry_run": dry_run,
        "inbound_references": inbound_links(idx, file)[:50],
    }
    if dest_rel != file and (idx.root / dest_rel).exists():
        raise SpecError(
            "validation", f"destination {dest_rel} already exists; nothing changed"
        )
    if dry_run:
        result["method"] = "none"
        return result
    _atomic_write(src, new_text)
    method = "none"
    if dest_rel != file:
        dest = idx.root / dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if _is_git_tracked(idx.root, file):
            r = subprocess.run(
                ["git", "-C", str(idx.root), "mv", "--", file, dest_rel],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if r.returncode != 0:
                raise SpecError(
                    "io", f"git mv failed: {r.stderr.strip()}", file_written=file
                )
            method = "git mv"
        else:
            os.rename(src, dest)
            method = "rename"
    result["method"] = method
    idx.invalidate()
    idx.ensure(force=True)
    return result
