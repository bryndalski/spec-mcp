"""Configuration: SPEC_CONFIG env -> JSON file, merged over DEFAULTS.

Nothing project-specific lives in code; every format decision is a config key.
"""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any

from .errors import SpecError

DEFAULTS: dict[str, Any] = {
    "docs": {
        "globs": ["docs/**/*.md"],
        "exclude": ["**/node_modules/**"],
        "statuses": ["current", "superseded", "historical", "draft"],
        "default_hidden_statuses": ["superseded", "historical"],
        "archive_dir": "docs/archive",
        "require_front_matter": False,
        "stale_patterns": [],
        "evidence_roots": [],
        "max_chunk_chars": 1200,
    },
    "requirements": {
        "globs": ["docs/requirements/**/*.md", "docs/requirements/**/*.json"],
        "exclude": ["**/node_modules/**"],
        "id_pattern": r"\b[A-Z][A-Z0-9]{1,7}-R?\d{1,4}\b",
        "status_pattern": r"\b(TODO|IN PROGRESS|DONE|BLOCKED|DEPRECATED)\b",
        "write_file": "docs/requirements/requirements.json",
    },
    "flows": {
        "globs": ["docs/flows/**/*.md", "docs/flows/**/*.json"],
        "exclude": ["**/node_modules/**"],
        "heading_pattern": r"^##\s+(.+)$",
        "require_screen_role": False,
        "write_file": "docs/flows/flows.json",
    },
    "datamodel": {
        "globs": ["docs/datamodel/*.json"],
        "exclude": ["**/node_modules/**"],
        "max_gsis_per_table": 3,
        "write_file": "docs/datamodel/datamodel.json",
    },
    "components": {
        "globs": ["front-end/vendor/duotio-ui/src/components/*/*.registry.json"],
        "exclude": ["**/node_modules/**"],
        "index_file_globs": ["front-end/vendor/duotio-ui/registry/index.json"],
        "tokens_css": "front-end/vendor/duotio-ui/src/styles/tokens.css",
        "expected_files": [".tsx", ".test.tsx", ".e2e.ts", ".stories.tsx", ".md", ".registry.json", "index.ts"],
    },
    "designs": {
        "globs": ["docs/design/views/*.json"],
        "exclude": ["**/node_modules/**"],
        "write_dir": "docs/design/views",
        "messages_file": "front-end/messages/pl.json",
    },
    "records": {},
    "links_file": ".spec-links.json",
    "index_file": ".spec-index.json",
}

_REGEX_KEYS = (("requirements", "id_pattern"), ("requirements", "status_pattern"), ("flows", "heading_pattern"))


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def config_path() -> Path:
    return Path(os.environ.get("SPEC_CONFIG") or ".spec-mcp.json")


def load_config(path: Path | None = None, root: Path | None = None) -> dict[str, Any]:
    """Load config. `root` is the directory relative globs resolve against (default: cwd)."""
    path = path or config_path()
    root = Path(root or os.getcwd()).resolve()
    cfg = copy.deepcopy(DEFAULTS)
    source: str | None = None
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise SpecError("config", f"cannot read {path}: {e}") from e
        if not isinstance(data, dict):
            raise SpecError("config", f"{path}: top level must be a JSON object")
        cfg = _merge(cfg, data)
        source = str(path.resolve())
    for section, key in _REGEX_KEYS:
        pat = cfg[section][key]
        try:
            re.compile(pat, re.MULTILINE)
        except re.error as e:
            raise SpecError("config", f"{section}.{key} is not a valid regex: {e}") from e
    _validate_docs(cfg["docs"])
    cfg["_root"] = str(root)
    cfg["_source"] = source
    return cfg


def _validate_docs(docs: dict[str, Any]) -> None:
    """Fail early (category `config`) on malformed document-status settings."""
    for key in ("statuses", "default_hidden_statuses", "evidence_roots"):
        if not isinstance(docs.get(key), list) or not all(isinstance(x, str) for x in docs[key]):
            raise SpecError("config", f"docs.{key} must be a list of strings")
    unknown = [x for x in docs["default_hidden_statuses"] if x not in docs["statuses"]]
    if unknown:
        raise SpecError("config", f"docs.default_hidden_statuses not in docs.statuses: {unknown}")
    pats = docs.get("stale_patterns")
    if not isinstance(pats, list):
        raise SpecError("config", "docs.stale_patterns must be a list")
    for i, sp in enumerate(pats):
        if not isinstance(sp, dict) or not sp.get("id") or not sp.get("regex"):
            raise SpecError("config", f"docs.stale_patterns[{i}] needs `id` and `regex`")
        flags = re.IGNORECASE if "i" in str(sp.get("flags", "")) else 0
        try:
            re.compile(sp["regex"], flags)
        except re.error as e:
            raise SpecError("config", f"docs.stale_patterns[{sp['id']}].regex is not a valid regex: {e}") from e
