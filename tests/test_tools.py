import json
import os
from pathlib import Path

import pytest

from spec_mcp import server


@pytest.fixture
def svc(project: Path, monkeypatch):
    monkeypatch.chdir(project)
    monkeypatch.setenv("SPEC_CONFIG", str(project / ".spec-mcp.json"))
    server.reset()
    return project


def test_spec_config_reports_counts(svc):
    out = server.spec_config()
    assert out["counts"] == {"docs": 4, "requirements": 5, "flows": 2, "datamodel": 0, "components": 0, "designs": 0, "records": {}}
    assert out["config"]["requirements"]["write_file"] == "docs/requirements/requirements.json"
    assert "index_age_seconds" in out


def test_docs_search_has_denominator_and_hits(svc):
    out = server.docs_search("organization activation two-sided", limit=3)
    assert out["searched_chunks"] > 0 and out["searched_files"] == 4
    assert out["hits"][0]["file"] == "docs/HOSTING.md"
    assert out["hits"][0]["heading"] == "Activation funnel"
    assert out["hits"][0]["line"] == 7


def test_docs_search_glob_filter(svc):
    out = server.docs_search("activation", glob="docs/flows/**")
    assert all(h["file"].startswith("docs/flows/") for h in out["hits"])
    assert out["searched_files"] == 1


def test_docs_get_whole_and_section(svc):
    whole = server.docs_get("docs/HOSTING.md")
    assert whole["text"].startswith("# Hosting")
    sec = server.docs_get("docs/HOSTING.md", heading="Domains")
    assert sec["line_start"] == 3 and "gateway" in sec["text"] and "two-sided" not in sec["text"]
    missing = server.docs_get("docs/NOPE.md")
    assert missing["error"]["category"] == "not_found"


def test_docs_list(svc):
    out = server.docs_list()
    assert out["total"] == 4
    hosting = next(f for f in out["files"] if f["file"] == "docs/HOSTING.md")
    assert hosting["title"] == "Hosting" and hosting["bytes"] > 0


def test_req_list_filters(svc):
    assert server.req_list()["total"] == 5
    assert [r["id"] for r in server.req_list(domain="invitations")["items"]] == ["INV-R1", "INV-R2"]
    assert [r["id"] for r in server.req_list(status="DONE")["items"]] == ["ACT-R3"]
    assert [r["id"] for r in server.req_list(with_flow=True)["items"]] == ["ACT-R2", "ACT-R3", "INV-R1"]
    assert [r["id"] for r in server.req_list(with_test=True)["items"]] == ["ACT-R2"]
    assert server.req_list(with_flow=False)["total"] == 2


def test_req_get(svc):
    out = server.req_get("ACT-R3")
    assert out["requirement"]["status"] == "DONE"
    assert out["requirement"]["refs"] == ["ACT-R1"]
    assert out["flows"] == ["1-from-signup-to-activation"]
    assert server.req_get("ZZZ-R1")["error"]["category"] == "not_found"


def test_req_upsert_creates_json_and_updates_in_place(svc: Path):
    out = server.req_upsert("NEW-R1", "First new requirement", status="TODO", tags=["billing"], acceptance=["Given X, when Y, then Z"])
    assert out["action"] == "created" and out["format"] == "json"
    path = svc / "docs/requirements/requirements.json"
    data = json.loads(path.read_text())
    assert data["items"][0]["id"] == "NEW-R1"
    assert list(data["items"][0].keys())[:4] == ["id", "title", "text", "status"]
    assert data["items"][0]["links"] == {"flows": [], "docs": [], "tests": []}
    assert server.req_get("NEW-R1")["requirement"]["status"] == "TODO"
    assert server.req_get("NEW-R1")["requirement"]["tags"] == ["billing"]
    out2 = server.req_upsert("NEW-R1", "First new requirement, revised", status="DONE")
    assert out2["action"] == "updated"
    data = json.loads(path.read_text())
    assert len(data["items"]) == 1 and data["items"][0]["text"].endswith("revised") and data["items"][0]["status"] == "DONE"
    assert data["items"][0]["tags"] == ["billing"]  # untouched fields survive an update
    # sorted by id, 2-space indent
    server.req_upsert("ABC-R9", "earlier id")
    data = json.loads(path.read_text())
    assert [i["id"] for i in data["items"]] == ["ABC-R9", "NEW-R1"]
    assert path.read_text().startswith('{\n  "$schema"')
    assert server.req_list()["total"] == 7


def test_req_upsert_refuses_id_defined_elsewhere(svc):
    out = server.req_upsert("ACT-R1", "clash")
    assert out["error"]["category"] == "validation"
    assert "activation.md" in out["error"]["message"]


def test_req_link_validates_targets(svc):
    ok = server.req_link("ACT-R1", "flow", "1-from-signup-to-activation")
    assert ok["links"]["flows"] == ["1-from-signup-to-activation"]
    bad_flow = server.req_link("ACT-R1", "flow", "no-such-flow")
    assert bad_flow["error"]["category"] == "not_found"
    bad_req = server.req_link("NOPE-R1", "test", "x.spec.ts")
    assert bad_req["error"]["category"] == "not_found"
    bad_type = server.req_link("ACT-R1", "bogus", "x")
    assert bad_type["error"]["category"] == "validation"
    doc = server.req_link("ACT-R1", "doc", "docs/HOSTING.md")
    assert doc["links"]["docs"] == ["docs/HOSTING.md"]


def test_req_coverage(svc):
    server.req_link("INV-R2", "test", "inv.spec.ts")
    cov = server.req_coverage()
    assert cov["total"] == 5 and cov["denominator"]["requirement_files"] == 2
    assert cov["with_flow"] == 3
    assert cov["with_test"] == 2  # ACT-R2 inline + INV-R2 sidecar
    assert cov["with_status"] == 2
    assert cov["by_domain"]["activation"]["total"] == 3
    assert "ACT-R1" in cov["uncovered"]
    sub = server.req_coverage(domain="invitations")
    assert sub["total"] == 2


def test_flow_list_get_upsert(svc: Path):
    lst = server.flow_list()
    assert lst["total"] == 2
    got = server.flow_get("2-employee-joins-organization")
    assert got["flow"]["requirements"] == ["INV-R1", "INV-R9"]
    assert server.flow_get("nope")["error"]["category"] == "not_found"
    out = server.flow_upsert(
        "shift-swap",
        "Shift swap",
        steps=[{"role": "employee", "screen": "/schedule", "action": "request swap", "evidence": "swap.spec.ts"}],
        requirements=["ACT-R1"],
        roles=["employee", "manager"],
    )
    assert out["action"] == "created" and out["format"] == "json"
    path = svc / "docs/flows/flows.json"
    data = json.loads(path.read_text())
    assert data["items"][0]["id"] == "shift-swap"
    assert data["items"][0]["steps"][0] == {"n": "1", "role": "employee", "screen": "/schedule", "action": "request swap", "evidence": "swap.spec.ts"}
    assert server.flow_get("shift-swap")["flow"]["requirements"] == ["ACT-R1"]
    assert server.flow_list()["total"] == 3
    out2 = server.flow_upsert("shift-swap", "Shift swap v2", steps=[])
    assert out2["action"] == "updated" and json.loads(path.read_text())["items"][0]["title"] == "Shift swap v2"
    # a flow defined in markdown cannot be shadowed by the JSON writer
    clash = server.flow_upsert("2-employee-joins-organization", "x", steps=[])
    assert clash["error"]["category"] == "validation"


def test_spec_validate_findings(svc):
    server.req_link("ACT-R1", "test", "ghost.spec.ts")
    out = server.spec_validate()
    assert out["checked"]["requirements"] == 5 and out["checked"]["flows"] == 2
    kinds = {f["kind"] for f in out["findings"]}
    assert "dangling_id" in kinds  # INV-R9 referenced in flow but undefined
    dangling = [f for f in out["findings"] if f["kind"] == "dangling_id"]
    assert any(f["id"] == "INV-R9" for f in dangling)
    assert "duplicate_id" not in kinds


def test_spec_validate_duplicate(svc: Path):
    (svc / "docs/requirements/domains/dup.md").write_text("- ACT-R1 duplicate here\n")
    out = server.spec_validate()
    dups = [f for f in out["findings"] if f["kind"] == "duplicate_id"]
    assert dups and dups[0]["id"] == "ACT-R1" and len(dups[0]["files"]) == 2


def test_spec_index_writes_index_md(svc: Path):
    out = server.spec_index()
    assert out["requirements"] == 5 and out["flows"] == 2
    req_idx = svc / "docs/requirements/domains/INDEX.md"
    flow_idx = svc / "docs/flows/INDEX.md"
    assert req_idx.exists() and "ACT-R1" in req_idx.read_text() and "spec-mcp" in req_idx.read_text()
    assert flow_idx.exists() and "1-from-signup-to-activation" in flow_idx.read_text()
    # INDEX.md itself must not be parsed as requirements on next build
    assert server.req_coverage()["total"] == 5


def test_tool_registry_has_45_tools():
    names = sorted(server.mcp._tool_manager._tools)  # noqa: SLF001
    assert len(names) == 45
    assert "spec_import_markdown" in names
    assert {"spec_config", "docs_search", "req_coverage", "spec_validate", "spec_index", "flow_upsert"} <= set(names)
