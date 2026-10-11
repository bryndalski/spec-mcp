import json
from pathlib import Path

import pytest

from spec_mcp import server

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["id", "text", "status"],
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string", "pattern": "^CL-\\d+$"},
        "text": {"type": "string", "minLength": 1},
        "status": {"enum": ["verified", "unverified", "forbidden"]},
        "evidence": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "score": {"type": "number"},
        "checked_at": {"type": "string", "format": "date"},
    },
    "dependentRequired": {"score": ["evidence"]},
}

RECORDS = {
    "meta": {"owner": "marketing"},
    "records": [
        {"id": "CL-2", "text": "Second", "status": "unverified", "tags": ["landing"], "score": 2, "evidence": "x"},
        {"id": "CL-1", "text": "First", "status": "verified", "tags": ["landing", "pricing"], "score": 4, "evidence": "pl.json:12"},
    ],
}


def _write(root: Path, rel: str, data) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


@pytest.fixture
def svc(project: Path, monkeypatch):
    _write(project, "docs/company/marketing/schema/claims.schema.json", SCHEMA)
    _write(project, "docs/company/marketing/claims.json", RECORDS)
    _write(project, "docs/company/leads/schema/leads.schema.json", {"type": "object", "required": ["id", "name"], "properties": {"id": {"type": "string"}, "name": {"type": "string"}, "status": {"type": "string"}}})
    _write(project, "docs/company/leads/batch-a.json", {"records": [{"id": "L-1", "name": "A", "status": "new"}]})
    _write(project, "docs/company/leads/batch-b.json", {"records": [{"id": "L-2", "name": "B", "status": "new"}, {"id": "L-1", "name": "dup", "status": "demo"}]})
    cfg = json.loads((project / ".spec-mcp.json").read_text())
    cfg["records"] = {
        "claims": {
            "globs": ["docs/company/marketing/claims.json"],
            "schema": "docs/company/marketing/schema/claims.schema.json",
            "id_field": "id",
            "write_file": "docs/company/marketing/claims.json",
        },
        "leads": {"globs": ["docs/company/leads/*.json"], "schema": "docs/company/leads/schema/leads.schema.json"},
    }
    (project / ".spec-mcp.json").write_text(json.dumps(cfg))
    monkeypatch.chdir(project)
    monkeypatch.setenv("SPEC_CONFIG", str(project / ".spec-mcp.json"))
    server.reset()
    return project


def test_rec_tools_registered():
    names = set(server.mcp._tool_manager._tools)  # noqa: SLF001
    assert {"rec_collections", "rec_list", "rec_get", "rec_upsert", "rec_validate", "rec_aggregate"} <= names


def test_rec_collections_lists_counts_files_schema(svc):
    out = server.rec_collections()
    assert out["total"] == 2
    claims = next(c for c in out["collections"] if c["name"] == "claims")
    assert claims["records"] == 2
    assert claims["files"] == ["docs/company/marketing/claims.json"]
    assert claims["schema"].endswith("claims.schema.json")
    leads = next(c for c in out["collections"] if c["name"] == "leads")
    assert leads["records"] == 3 and len(leads["files"]) == 2
    assert server.spec_config()["counts"]["records"] == {"claims": 2, "leads": 3}


def test_rec_list_where_limit_fields_denominator(svc):
    out = server.rec_list("claims")
    assert out["total"] == 2 and out["matched"] == 2
    assert out["items"][0]["_file"] == "docs/company/marketing/claims.json"
    out = server.rec_list("claims", where={"status": "verified"}, fields=["id", "text"])
    assert out["matched"] == 1 and out["total"] == 2
    assert out["items"] == [{"id": "CL-1", "text": "First", "_file": "docs/company/marketing/claims.json"}]
    out = server.rec_list("claims", where={"tags": "pricing"})
    assert [r["id"] for r in out["items"]] == ["CL-1"]
    out = server.rec_list("claims", limit=1)
    assert len(out["items"]) == 1 and out["matched"] == 2
    assert server.rec_list("claims", where={"status": "nope"})["matched"] == 0


def test_rec_get_and_not_found(svc):
    out = server.rec_get("claims", "CL-1")
    assert out["record"]["text"] == "First" and "_file" not in out["record"]
    err = server.rec_get("claims", "CL-9")["error"]
    assert err["category"] == "not_found" and err["available"] == ["CL-1", "CL-2"]
    assert server.rec_list("nope")["error"]["category"] == "not_found"
    assert server.rec_list("nope")["error"]["available"] == ["claims", "leads"]


def test_rec_upsert_creates_keeps_order_and_meta(svc: Path):
    out = server.rec_upsert("claims", {"id": "CL-3", "text": "Trzeci — ąę", "status": "forbidden"})
    assert out == {"collection": "claims", "action": "created", "id": "CL-3", "file": "docs/company/marketing/claims.json"}
    raw = (svc / "docs/company/marketing/claims.json").read_text(encoding="utf-8")
    doc = json.loads(raw)
    assert doc["meta"] == {"owner": "marketing"}
    assert [r["id"] for r in doc["records"]] == ["CL-2", "CL-1", "CL-3"]
    assert "ąę" in raw and raw.endswith("}\n") and "\n  \"records\"" in raw


def test_rec_upsert_replaces_in_place(svc: Path):
    out = server.rec_upsert("claims", {"id": "CL-2", "text": "Second v2", "status": "verified"})
    assert out["action"] == "updated"
    doc = json.loads((svc / "docs/company/marketing/claims.json").read_text())
    assert [r["id"] for r in doc["records"]] == ["CL-2", "CL-1"]
    assert doc["records"][0] == {"id": "CL-2", "text": "Second v2", "status": "verified"}  # full replace, not merge


def test_rec_upsert_refuses_invalid_record_without_writing(svc: Path):
    before = (svc / "docs/company/marketing/claims.json").read_text()
    out = server.rec_upsert("claims", {"id": "bad", "text": "", "status": "maybe", "extra": 1, "score": 3})
    err = out["error"]
    assert err["category"] == "validation"
    paths = {e["json_path"] for e in err["errors"]}
    assert {"$.id", "$.text", "$.status", "$", "$.score"} <= paths or {"$.id", "$.text", "$.status", "$"} <= paths
    assert (svc / "docs/company/marketing/claims.json").read_text() == before
    out = server.rec_upsert("claims", {"text": "no id", "status": "verified"})
    assert any(e["json_path"] == "$.id" for e in out["error"]["errors"])


def test_rec_upsert_validates_date_format(svc):
    out = server.rec_upsert("claims", {"id": "CL-7", "text": "t", "status": "verified", "checked_at": "2026-13-45"})
    assert out["error"]["errors"][0]["json_path"] == "$.checked_at"
    assert server.rec_upsert("claims", {"id": "CL-7", "text": "t", "status": "verified", "checked_at": "2026-10-11"})["action"] == "created"


def test_rec_upsert_multi_file_needs_file_or_write_file(svc: Path):
    out = server.rec_upsert("leads", {"id": "L-3", "name": "C"})
    assert out["error"]["category"] == "validation" and "pass `file`" in out["error"]["message"]
    out = server.rec_upsert("leads", {"id": "L-3", "name": "C"}, file="docs/company/leads/batch-a.json")
    assert out["action"] == "created" and out["file"] == "docs/company/leads/batch-a.json"
    doc = json.loads((svc / "docs/company/leads/batch-a.json").read_text())
    assert [r["id"] for r in doc["records"]] == ["L-1", "L-3"]
    # new file via `file`
    out = server.rec_upsert("leads", {"id": "L-4", "name": "D"}, file="docs/company/leads/batch-c.json")
    assert out["action"] == "created" and (svc / "docs/company/leads/batch-c.json").is_file()


def test_rec_validate_reports_duplicates_schema_and_files(svc: Path):
    _write(svc, "docs/company/leads/broken.json", {"nope": 1})
    (svc / "docs/company/leads/empty.json").write_text("")
    out = server.rec_validate()
    assert out["collections"] == 2 and out["checked"] == 5
    leads = next(r for r in out["results"] if r["collection"] == "leads")
    kinds = sorted(e["kind"] for e in leads["errors"])
    assert kinds == ["duplicate_id", "file", "file"]
    dup = next(e for e in leads["errors"] if e["kind"] == "duplicate_id")
    assert dup["id"] == "L-1" and dup["file"] == "docs/company/leads/batch-b.json" and "batch-a.json[0]" in dup["message"]
    claims = next(r for r in out["results"] if r["collection"] == "claims")
    assert claims["errors"] == [] and claims["checked"] == 2 and claims["files"] == 1
    one = server.rec_validate("claims")
    assert one["collections"] == 1 and one["errors"] == 0


def test_rec_validate_schema_errors_have_json_path(svc: Path):
    doc = json.loads((svc / "docs/company/marketing/claims.json").read_text())
    doc["records"].append({"id": "CL-5", "text": "x", "status": "verified", "tags": ["ok", 3]})
    doc["records"].append("not an object")
    _write(svc, "docs/company/marketing/claims.json", doc)
    out = server.rec_validate("claims")
    errs = out["results"][0]["errors"]
    assert out["checked"] == 4 and len(errs) == 2
    assert errs[0] == {"kind": "schema", "file": "docs/company/marketing/claims.json", "index": 2, "id": "CL-5", "json_path": "$.tags[1]", "message": "3 is not of type 'string'", "keyword": "type"}
    assert errs[1]["json_path"] == "$" and errs[1]["index"] == 3


def test_rec_validate_missing_schema_is_config_error(svc: Path):
    (svc / "docs/company/marketing/schema/claims.schema.json").unlink()
    out = server.rec_validate("claims")
    assert out["results"][0]["errors"][0]["kind"] == "config"
    assert "schema file not found" in out["results"][0]["errors"][0]["message"]


def test_rec_aggregate_count_sum_avg_where(svc):
    out = server.rec_aggregate("claims", "status")
    assert out["groups"] == {"unverified": 1, "verified": 1} and out["denominator"] == 2 and out["total"] == 2
    assert server.rec_aggregate("claims", "tags")["groups"] == {"landing": 2, "pricing": 1}
    assert server.rec_aggregate("claims", "status", "sum:score")["groups"] == {"verified": 4.0, "unverified": 2.0}
    assert server.rec_aggregate("claims", "tags", "avg:score")["groups"] == {"pricing": 4.0, "landing": 3.0}
    out = server.rec_aggregate("claims", "status", where={"tags": "pricing"})
    assert out["groups"] == {"verified": 1} and out["denominator"] == 1 and out["total"] == 2
    assert server.rec_aggregate("leads", "missing")["groups"] == {"(none)": 3}
    assert server.rec_aggregate("claims", "status", "max:score")["error"]["category"] == "validation"


def test_spec_index_writes_records_sections(svc: Path):
    out = server.spec_index()
    assert out["records"] == 5
    assert "docs/company/marketing/INDEX.md" in out["written"] and "docs/company/leads/INDEX.md" in out["written"]
    text = (svc / "docs/company/marketing/INDEX.md").read_text(encoding="utf-8")
    assert text.startswith("<!-- generated by spec-mcp")
    assert "## claims" in text and "_2 records in 1 files._" in text
    assert "| verified | 1 |" in text and "| unverified | 1 |" in text
    assert "| CL-1 |" in text and "`claims.json`" in text
    leads = (svc / "docs/company/leads/INDEX.md").read_text()
    assert "_3 records in 2 files._" in leads
    # generated INDEX.md must not be parsed as a doc chunk source of requirements
    assert server.spec_validate()["duplicates"] == [] if "duplicates" in server.spec_validate() else True


def test_records_config_errors(svc: Path):
    cfg = json.loads((svc / ".spec-mcp.json").read_text())
    cfg["records"]["bad"] = {"schema": "x.json"}
    (svc / ".spec-mcp.json").write_text(json.dumps(cfg))
    server.reset()
    out = server.rec_collections()
    assert out["error"]["category"] == "config" and "records.bad" in out["error"]["message"]
