import json
from pathlib import Path

import pytest

from spec_mcp import server


@pytest.fixture
def svc(project: Path, monkeypatch):
    (project / "docs/requirements/billing.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": "BIL-R1",
                        "title": "Invoice monthly",
                        "text": "Invoices are issued monthly per organization (depends on ACT-R1)",
                        "status": "DONE",
                        "tags": ["billing", "stripe"],
                        "acceptance": ["Given an active org, when the month ends, then an invoice exists"],
                        "links": {"flows": ["1-from-signup-to-activation"], "docs": ["docs/HOSTING.md"], "tests": ["billing.spec.ts"]},
                    },
                    {"id": "BIL-R2", "text": "Bare array items also work", "domain": "payments"},
                ]
            }
        )
    )
    (project / "docs/flows/checkout.json").write_text(
        json.dumps(
            [
                {
                    "id": "checkout",
                    "title": "Checkout",
                    "roles": ["owner"],
                    "requirements": ["BIL-R1"],
                    "steps": [{"role": "owner", "screen": "/billing", "action": "pay invoice BIL-R2", "evidence": "pay.e2e.ts"}, "confirm"],
                }
            ]
        )
    )
    monkeypatch.chdir(project)
    monkeypatch.setenv("SPEC_CONFIG", str(project / ".spec-mcp.json"))
    # widen globs to include json for this fixture
    cfg = json.loads((project / ".spec-mcp.json").read_text())
    cfg["requirements"]["globs"] = ["docs/requirements/domains/*.md", "docs/requirements/*.json"]
    cfg["flows"]["globs"] = ["docs/flows/**/*.md", "docs/flows/**/*.json"]
    (project / ".spec-mcp.json").write_text(json.dumps(cfg))
    server.reset()
    return project


def test_json_requirements_are_read_with_schema(svc):
    r = server.req_get("BIL-R1")["requirement"]
    assert r["format"] == "json" and r["file"] == "docs/requirements/billing.json"
    assert r["domain"] == "billing" and r["status"] == "DONE"
    assert r["tags"] == ["billing", "stripe"]
    assert r["refs"] == ["ACT-R1"]
    assert r["tests"] == ["billing.spec.ts"] and r["docs"] == ["docs/HOSTING.md"]
    assert "1-from-signup-to-activation" in r["flows"] and "checkout" in r["flows"]
    assert server.req_get("BIL-R2")["requirement"]["domain"] == "payments"
    assert server.spec_config()["counts"]["requirements"] == 7


def test_json_flows_are_read(svc):
    f = server.flow_get("checkout")["flow"]
    assert f["format"] == "json" and f["roles"] == ["owner"]
    assert f["requirements"] == ["BIL-R1", "BIL-R2"]  # inline list + IDs found in steps
    assert f["steps"][0]["n"] == "1" and f["steps"][1] == {"n": "2", "role": "", "screen": "", "action": "confirm", "evidence": ""}
    assert server.flow_list()["total"] == 3


def test_req_link_on_json_item_is_inline_not_sidecar(svc: Path):
    out = server.req_link("BIL-R2", "test", "payments.spec.ts")
    assert out["links"]["tests"] == ["payments.spec.ts"]
    data = json.loads((svc / "docs/requirements/billing.json").read_text())
    item = next(i for i in data["items"] if i["id"] == "BIL-R2")
    assert item["links"]["tests"] == ["payments.spec.ts"]
    assert not (svc / ".spec-links.json").exists()
    # markdown item still goes to the sidecar
    server.req_link("ACT-R1", "test", "act.spec.ts")
    assert json.loads((svc / ".spec-links.json").read_text())["ACT-R1"]["tests"] == ["act.spec.ts"]


def test_coverage_counts_inline_links(svc):
    cov = server.req_coverage(domain="billing")
    assert cov["total"] == 1 and cov["with_test"] == 1 and cov["with_doc"] == 1 and cov["with_flow"] == 1


def test_import_markdown_dry_run_and_merge(svc: Path):
    server.req_link("ACT-R1", "test", "act.spec.ts")  # sidecar link must travel into JSON
    dry = server.spec_import_markdown("requirements")
    assert dry["dry_run"] is True and dry["unique"] == 5 and dry["parsed"] == 5
    assert dry["sample"][0]["source"] == {"file": "docs/requirements/domains/activation.md", "line": 7}
    assert not (svc / "docs/requirements/requirements.json").exists()
    real = server.spec_import_markdown("requirements", dry_run=False)
    assert real["result"] == {"added": 5, "skipped_existing": 0, "total": 5}
    data = json.loads((svc / "docs/requirements/requirements.json").read_text())
    act1 = next(i for i in data["items"] if i["id"] == "ACT-R1")
    assert act1["source"] == {"file": "docs/requirements/domains/activation.md", "line": 7}
    assert act1["links"]["tests"] == ["act.spec.ts"]
    assert [i["id"] for i in data["items"]] == sorted(i["id"] for i in data["items"])
    # duplicates across md + json are now reported, not crashed, and a re-import adds nothing
    again = server.spec_import_markdown("requirements", dry_run=False)
    assert again["result"]["added"] == 0 and again["result"]["skipped_existing"] == 5
    dups = [f for f in server.spec_validate()["findings"] if f["kind"] == "duplicate_id"]
    assert {d["id"] for d in dups} == {"ACT-R1", "ACT-R2", "ACT-R3", "INV-R1", "INV-R2"}


def test_import_markdown_flows_with_glob(svc: Path):
    out = server.spec_import_markdown("flows", glob="docs/flows/PROCESSES.md", dry_run=False)
    assert out["unique"] == 2 and out["result"]["added"] == 2
    data = json.loads((svc / "docs/flows/flows.json").read_text())
    f1 = next(i for i in data["items"] if i["id"] == "1-from-signup-to-activation")
    assert f1["source"] == {"file": "docs/flows/PROCESSES.md", "line": 3}
    assert f1["steps"][0]["n"] == "1.1" and f1["requirements"] == ["ACT-R2", "ACT-R3"]
    bad = server.spec_import_markdown("nope")
    assert bad["error"]["category"] == "validation"
