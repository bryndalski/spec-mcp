import re
from pathlib import Path

from spec_mcp.parse import chunk_doc, parse_flows, parse_requirements

ID_RE = re.compile(r"\b[A-Z]{2,6}-R\d{1,3}\b")
STATUS_RE = re.compile(r"\b(TODO|IN PROGRESS|DONE|BLOCKED|DEPRECATED)\b")


def test_parse_requirements_table_and_bullets(project: Path):
    act = project / "docs/requirements/domains/activation.md"
    reqs = parse_requirements(act, ID_RE, STATUS_RE, root=project)
    assert [r["id"] for r in reqs] == ["ACT-R1", "ACT-R2", "ACT-R3"]
    r1 = reqs[0]
    assert r1["domain"] == "activation"
    assert r1["file"] == "docs/requirements/domains/activation.md"
    assert r1["line"] == 7
    assert "|" not in r1["text"]
    assert "activation.types.ts:5-6" in r1["evidence"]
    assert r1["status"] is None
    r2 = reqs[1]
    assert any(e.endswith(".spec.ts") for e in r2["evidence"])
    r3 = reqs[2]
    assert r3["status"] == "DONE"
    assert r3["refs"] == ["ACT-R1"]

    inv = parse_requirements(project / "docs/requirements/domains/invitations.md", ID_RE, STATUS_RE, root=project)
    assert [r["id"] for r in inv] == ["INV-R1", "INV-R2"]
    assert inv[0]["status"] == "TODO"
    assert inv[0]["text"].startswith("Admin creates")


def test_parse_flows_sections(project: Path):
    flows = parse_flows(
        project / "docs/flows/PROCESSES.md",
        re.compile(r"^##\s+Process\s+(.+)$"),
        ID_RE,
        root=project,
    )
    assert len(flows) == 2
    f1 = flows[0]
    assert f1["id"] == "1-from-signup-to-activation"
    assert f1["title"] == "1 — From signup to activation"
    assert f1["line"] == 3
    assert f1["requirements"] == ["ACT-R2", "ACT-R3"]
    assert len(f1["steps"]) == 2
    assert f1["steps"][0]["n"] == "1.1"
    assert "availability check" in f1["steps"][0]["action"]
    assert f1["steps"][0]["evidence"] == "`onboarding.service.ts:11`"
    f2 = flows[1]
    assert f2["requirements"] == ["INV-R1", "INV-R9"]
    assert [s["n"] for s in f2["steps"]] == ["1", "2"]


def test_chunk_doc_by_headings(project: Path):
    chunks = chunk_doc(project / "docs/HOSTING.md", root=project)
    headings = [c["heading"] for c in chunks]
    assert headings == ["Hosting", "Domains", "Activation funnel"]
    assert chunks[2]["line"] == 7
    assert "two-sided" in chunks[2]["text"]
    assert chunks[0]["file"] == "docs/HOSTING.md"
