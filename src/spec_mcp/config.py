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
    "docs": {"globs": ["docs/**/*.md"], "exclude": ["**/node_modules/**"]},
    "requirements": {
        "globs": ["docs/requirements/**/*.md", "docs/requirements/**/*.json"],
        "id_pattern": r"\b[A-Z][A-Z0-9]{1,7}-R?\d{1,4}\b",
        "status_pattern": r"\b(TODO|IN PROGRESS|DONE|BLOCKED|DEPRECATED)\b",
        "write_file": "docs/requirements/requirements.json",
    },
    "flows": {
        "globs": ["docs/flows/**/*.md", "docs/flows/**/*.json"],
        "heading_pattern": r"^##\s+(.+)$",
        "write_file": "docs/flows/flows.json",
    },
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
    cfg["_root"] = str(root)
    cfg["_source"] = source
    return cfg
