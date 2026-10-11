import json
from pathlib import Path

from spec_mcp.config import DEFAULTS, load_config


def test_defaults_when_file_missing(tmp_path: Path):
    cfg = load_config(tmp_path / "nope.json", root=tmp_path)
    assert cfg["docs"]["globs"] == DEFAULTS["docs"]["globs"]
    assert cfg["requirements"]["id_pattern"] == DEFAULTS["requirements"]["id_pattern"]
    assert cfg["links_file"] == ".spec-links.json"
    assert cfg["_root"] == str(tmp_path)
    assert cfg["_source"] is None


def test_partial_file_merges_with_defaults(tmp_path: Path):
    p = tmp_path / ".spec-mcp.json"
    p.write_text(json.dumps({"requirements": {"globs": ["reqs/*.md"]}}))
    cfg = load_config(p, root=tmp_path)
    assert cfg["requirements"]["globs"] == ["reqs/*.md"]
    # untouched keys keep defaults
    assert cfg["requirements"]["write_file"] == DEFAULTS["requirements"]["write_file"]
    assert cfg["flows"]["globs"] == DEFAULTS["flows"]["globs"]
    assert cfg["_source"] == str(p)


def test_invalid_regex_is_config_error(tmp_path: Path):
    p = tmp_path / ".spec-mcp.json"
    p.write_text(json.dumps({"requirements": {"id_pattern": "([unclosed"}}))
    from spec_mcp.errors import SpecError

    try:
        load_config(p, root=tmp_path)
    except SpecError as e:
        assert e.category == "config"
    else:
        raise AssertionError("expected SpecError")
