import json
from pathlib import Path

import pytest

from spec_mcp import server


@pytest.fixture
def svc(project: Path, monkeypatch):
    # files that must be ignored via `exclude`
    (project / "docs/requirements/jira-mapping.json").write_text(
        json.dumps({"ACT-R1": "KAN-1", "mapping": True})
    )
    (project / "docs/requirements/domains/DRAFT-ignored.md").write_text(
        "- ZZZ-R1 draft requirement\n"
    )
    (project / "docs/flows/archive").mkdir()
    (project / "docs/flows/archive/OLD.md").write_text(
        "## Process 9 — Old flow\n\n1. step (ACT-R1)\n"
    )
    # a JSON file in scope but with an unexpected shape: skipped with a warning, no crash
    (project / "docs/requirements/odd.json").write_text(
        json.dumps({"not": "a collection"})
    )
    cfg = json.loads((project / ".spec-mcp.json").read_text())
    cfg["requirements"]["globs"] = [
        "docs/requirements/domains/*.md",
        "docs/requirements/*.json",
    ]
    cfg["requirements"]["exclude"] = ["**/jira-mapping*.json", "**/DRAFT-*.md"]
    cfg["flows"]["globs"] = ["docs/flows/**/*.md"]
    cfg["flows"]["exclude"] = ["docs/flows/archive/**"]
    (project / ".spec-mcp.json").write_text(json.dumps(cfg))
    monkeypatch.chdir(project)
    monkeypatch.setenv("SPEC_CONFIG", str(project / ".spec-mcp.json"))
    server.reset()
    return project


def test_exclude_applies_to_requirements_and_flows(svc):
    out = server.spec_config()
    assert out["counts"] == {"docs": 6, "requirements": 5, "flows": 2, "datamodel": 0, "components": 0, "designs": 0, "records": {}}
    assert "docs/requirements/jira-mapping.json" not in out["files"]["requirements"]
    assert (
        "docs/requirements/domains/DRAFT-ignored.md" not in out["files"]["requirements"]
    )
    assert "docs/flows/archive/OLD.md" not in out["files"]["flows"]
    assert server.req_get("ZZZ-R1")["error"]["category"] == "not_found"


def test_malformed_json_collection_is_warned_not_crashed(svc):
    out = server.spec_config()
    assert "docs/requirements/odd.json" in out["files"]["requirements"]
    assert out["counts"]["requirements"] == 5
    warns = out["warnings"]
    assert len(warns) == 1
    assert (
        warns[0]["category"] == "config"
        and warns[0]["file"] == "docs/requirements/odd.json"
    )
    assert "items" in warns[0]["message"]
