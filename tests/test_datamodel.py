import json
from pathlib import Path

import pytest

from spec_mcp import server

COMPANIES = {
    "name": "companies",
    "source": "backend/terraform/modules/dynamodb/companies.tf:12",
    "description": "One row per organization",
    "pk": {"name": "PK", "type": "S"},
    "sk": {"name": "SK", "type": "S"},
    "gsis": [
        {
            "name": "GSI_COMPANY_URL",
            "pk": "companyUrl",
            "sk": None,
            "projection": "ALL",
            "attributes": [],
        }
    ],
    "ttl": None,
    "stream": False,
    "entities": ["Company"],
    "attributes": [
        {
            "name": "companyUrl",
            "type": "S",
            "required": True,
            "pii": False,
            "description": "kebab slug",
        },
        {
            "name": "ownerEmail",
            "type": "S",
            "required": True,
            "pii": True,
            "description": "",
        },
    ],
    "relations": [],
    "access_patterns": [
        {
            "name": "company by url",
            "operation": "Query",
            "index": "GSI_COMPANY_URL",
            "key": "companyUrl = :url",
            "repository": "src/repos/company.repository.ts:getByUrl",
            "description": "slug lookup",
        },
        {
            "name": "company by id",
            "operation": "GetItem",
            "index": None,
            "key": "PK = :id",
            "repository": "src/repos/company.repository.ts:getById",
            "description": "",
        },
    ],
}

LOCATIONS = {
    "name": "locations",
    "source": "backend/terraform/modules/dynamodb/locations.tf:1",
    "pk": {"name": "PK", "type": "S"},
    "sk": {"name": "SK", "type": "S"},
    "gsis": [
        {
            "name": "GSI_1",
            "pk": "companyId",
            "sk": "createdAt",
            "projection": "KEYS_ONLY",
            "attributes": [],
        },
        {
            "name": "GSI_2",
            "pk": "companyId",
            "sk": None,
            "projection": "INCLUDE",
            "attributes": ["name"],
        },
        {
            "name": "GSI_3",
            "pk": "name",
            "sk": None,
            "projection": "ALL",
            "attributes": [],
        },
        {
            "name": "GSI_4",
            "pk": "ghostAttr",
            "sk": None,
            "projection": "ALL",
            "attributes": [],
        },
    ],
    "ttl": "expiresAt",
    "stream": True,
    "entities": ["Location", "LocationMember"],
    "attributes": [
        {
            "name": "companyId",
            "type": "S",
            "required": True,
            "pii": False,
            "description": "",
        },
        {
            "name": "name",
            "type": "S",
            "required": True,
            "pii": False,
            "description": "",
        },
        {
            "name": "createdAt",
            "type": "S",
            "required": False,
            "pii": False,
            "description": "",
        },
    ],
    "relations": [
        {
            "from": "locations.companyId",
            "to": "companies.PK",
            "kind": "belongs_to",
            "enforced_by": "code",
            "note": "tenant scope",
        },
        {
            "from": "locations.companyId",
            "to": "ghosts.PK",
            "kind": "references",
            "enforced_by": "none",
            "note": "",
        },
        {
            "from": "locations.name",
            "to": "companies.nope",
            "kind": "references",
            "enforced_by": "none",
            "note": "",
        },
    ],
    "access_patterns": [
        {
            "name": "locations of company",
            "operation": "Query",
            "index": "GSI_1",
            "key": "companyId = :c",
            "repository": "src/repos/location.repository.ts:listByCompany",
            "description": "",
        },
        {
            "name": "all locations",
            "operation": "Scan",
            "index": None,
            "key": "",
            "repository": "src/repos/location.repository.ts:scanAll",
            "description": "ops only",
        },
        {
            "name": "by missing index",
            "operation": "Query",
            "index": "GSI_NOPE",
            "key": "x = :x",
            "repository": "src/repos/missing.repository.ts:q",
            "description": "",
        },
    ],
}


@pytest.fixture
def svc(project: Path, monkeypatch):
    (project / "docs/datamodel").mkdir()
    (project / "docs/datamodel/core.json").write_text(
        json.dumps({"tables": [COMPANIES, LOCATIONS]})
    )
    (project / "src/repos").mkdir(parents=True)
    (project / "src/repos/company.repository.ts").write_text(
        "export class CompanyRepository {}\n"
    )
    (project / "src/repos/location.repository.ts").write_text(
        "export class LocationRepository {}\n"
    )
    monkeypatch.chdir(project)
    monkeypatch.setenv("SPEC_CONFIG", str(project / ".spec-mcp.json"))
    server.reset()
    return project


def test_dm_tables_summary(svc):
    out = server.dm_tables()
    assert out["total"] == 2 and out["scanned_files"] == 1
    comp = next(t for t in out["items"] if t["name"] == "companies")
    assert comp == {
        "name": "companies",
        "pk": "PK",
        "sk": "SK",
        "gsis": 1,
        "entities": 1,
        "access_patterns": 2,
        "relations": 0,
        "attributes": 2,
        "file": "docs/datamodel/core.json",
    }
    assert server.spec_config()["counts"]["datamodel"] == 2


def test_dm_table_full_with_inbound_outbound(svc):
    out = server.dm_table("companies")
    t = out["table"]
    assert (
        t["gsis"][0]["name"] == "GSI_COMPANY_URL"
        and t["ttl"] is None
        and t["stream"] is False
    )
    assert [r["from"] for r in out["inbound"]] == [
        "locations.companyId",
        "locations.name",
    ]
    assert out["outbound"] == []
    loc = server.dm_table("locations")
    assert len(loc["outbound"]) == 3 and loc["inbound"] == []
    assert server.dm_table("nope")["error"]["category"] == "not_found"


def test_dm_relations_json_and_mermaid(svc):
    out = server.dm_relations()
    assert out["total"] == 3 and out["denominator"]["tables"] == 2
    by_table = server.dm_relations(table="companies")
    assert by_table["total"] == 2
    by_entity = server.dm_relations(entity="Location")
    assert by_entity["total"] == 3
    mm = server.dm_relations(format="mermaid")
    assert mm["mermaid"].startswith("erDiagram")
    assert 'locations }o--|| companies : "belongs_to' in mm["mermaid"]
    assert "ghosts" in mm["mermaid"]
    assert server.dm_relations(format="dot")["error"]["category"] == "validation"


def test_dm_access_patterns_filters(svc):
    assert server.dm_access_patterns()["total"] == 5
    assert server.dm_access_patterns(table="companies")["total"] == 2
    assert [p["name"] for p in server.dm_access_patterns(index="GSI_1")["items"]] == [
        "locations of company"
    ]
    assert server.dm_access_patterns(operation="scan")["total"] == 1
    assert server.dm_access_patterns(repository="company.repository")["total"] == 2
    item = server.dm_access_patterns(table="companies")["items"][0]
    assert (
        item["table"] == "companies"
        and item["repository_file"] == "src/repos/company.repository.ts"
    )


def test_dm_query_generic(svc):
    out = server.dm_query(where={"entity": "Company"})
    assert [t["name"] for t in out["tables"]] == ["companies"]
    assert out["counts"]["access_patterns"] == 2
    out = server.dm_query(where={"attribute": "companyId"})
    assert [t["name"] for t in out["tables"]] == ["locations"]
    assert [a["name"] for a in out["attributes"]] == ["companyId"]
    out = server.dm_query(where={"operation": "Query", "index": "GSI_1"})
    assert (
        out["counts"]["access_patterns"] == 1
        and out["access_patterns"][0]["table"] == "locations"
    )
    out = server.dm_query(where={"kind": "belongs_to"})
    assert out["counts"]["relations"] == 1
    bad = server.dm_query(where={"colour": "red"})
    assert bad["error"]["category"] == "validation"


def test_dm_aggregate(svc):
    assert server.dm_aggregate(by="table")["buckets"] == {
        "companies": 2,
        "locations": 3,
    }
    assert server.dm_aggregate(by="table", metric="gsis")["buckets"] == {
        "companies": 1,
        "locations": 4,
    }
    assert server.dm_aggregate(by="operation")["buckets"] == {
        "GetItem": 1,
        "Query": 3,
        "Scan": 1,
    }
    assert server.dm_aggregate(by="index")["buckets"] == {
        "(table)": 2,
        "GSI_1": 1,
        "GSI_COMPANY_URL": 1,
        "GSI_NOPE": 1,
    }
    assert server.dm_aggregate(by="entity")["buckets"] == {
        "Company": 1,
        "Location": 1,
        "LocationMember": 1,
    }
    assert server.dm_aggregate(by="kind")["buckets"] == {
        "belongs_to": 1,
        "references": 2,
    }
    rep = server.dm_aggregate(by="repository")["buckets"]
    assert (
        rep["src/repos/location.repository.ts"] == 2
        and rep["src/repos/company.repository.ts"] == 2
    )
    assert server.dm_aggregate(by="colour")["error"]["category"] == "validation"
    assert (
        server.dm_aggregate(by="index", metric="gsis")["error"]["category"]
        == "validation"
    )


def test_dm_validate_findings(svc):
    out = server.dm_validate()
    kinds = {}
    for f in out["findings"]:
        kinds.setdefault(f["kind"], []).append(f)
    assert (
        out["checked"]["tables"] == 2
        and out["checked"]["access_patterns"] == 5
        and out["checked"]["relations"] == 3
    )
    assert (
        kinds["too_many_gsis"][0]["table"] == "locations"
        and kinds["too_many_gsis"][0]["gsis"] == 4
    )
    assert kinds["scan_pattern"][0]["pattern"] == "all locations"
    assert kinds["unknown_index"][0]["index"] == "GSI_NOPE"
    assert {f["to"] for f in kinds["relation_unknown_table"]} == {"ghosts.PK"}
    assert {f["to"] for f in kinds["relation_unknown_attribute"]} == {"companies.nope"}
    assert kinds["gsi_key_not_in_attributes"][0]["attribute"] == "ghostAttr"
    assert kinds["repository_missing"][0]["path"] == "src/repos/missing.repository.ts"
    assert "duplicate_table" not in kinds
    # relation to a table's own PK name is fine (companies.PK)
    assert all(
        f["to"] != "companies.PK" for f in kinds.get("relation_unknown_attribute", [])
    )


def test_dm_upsert_pattern_and_table(svc: Path):
    out = server.dm_upsert_pattern(
        "companies",
        {
            "name": "company by url",
            "operation": "Query",
            "index": "GSI_COMPANY_URL",
            "key": "companyUrl = :url",
            "repository": "src/repos/company.repository.ts:getByUrl",
            "description": "updated",
        },
    )
    assert out["action"] == "updated" and out["file"] == "docs/datamodel/core.json"
    out2 = server.dm_upsert_pattern(
        "companies", {"name": "new pattern", "operation": "Put", "key": "PK, SK"}
    )
    assert out2["action"] == "created"
    data = json.loads((svc / "docs/datamodel/core.json").read_text())
    comp = next(t for t in data["tables"] if t["name"] == "companies")
    assert len(comp["access_patterns"]) == 3
    assert comp["access_patterns"][0]["description"] == "updated"
    assert list(data["tables"][0].keys())[:4] == ["name", "source", "description", "pk"]
    assert [t["name"] for t in data["tables"]] == ["companies", "locations"]
    assert (
        server.dm_upsert_pattern("nope", {"name": "x", "operation": "Query"})["error"][
            "category"
        ]
        == "not_found"
    )
    assert (
        server.dm_upsert_pattern("companies", {"operation": "Query"})["error"][
            "category"
        ]
        == "validation"
    )
    # new table -> default write_file
    t = server.dm_upsert_table(
        {"name": "shifts", "pk": {"name": "PK", "type": "S"}, "entities": ["Shift"]}
    )
    assert t["action"] == "created" and t["file"] == "docs/datamodel/datamodel.json"
    assert server.dm_tables()["total"] == 3
    # existing table -> updated in its owning file, fields merged
    t2 = server.dm_upsert_table({"name": "shifts", "description": "shift rows"})
    assert t2["action"] == "updated"
    full = server.dm_table("shifts")["table"]
    assert (
        full["description"] == "shift rows"
        and full["entities"] == ["Shift"]
        and full["sk"] is None
    )
    assert (
        server.dm_upsert_table({"pk": {"name": "PK"}})["error"]["category"]
        == "validation"
    )


def test_spec_index_writes_datamodel_index(svc: Path):
    out = server.spec_index()
    assert out["datamodel"] == 2
    idx = svc / "docs/datamodel/INDEX.md"
    assert idx.exists() and "docs/datamodel/INDEX.md" in out["written"]
    txt = idx.read_text()
    assert (
        "## companies" in txt
        and "GSI_COMPANY_URL" in txt
        and "locations.companyId" in txt
        and "company by url" in txt
    )


def test_dm_tools_registered():
    names = sorted(server.mcp._tool_manager._tools)  # noqa: SLF001
    assert len(names) == 40
    assert {
        "dm_tables",
        "dm_table",
        "dm_relations",
        "dm_access_patterns",
        "dm_query",
        "dm_aggregate",
        "dm_validate",
        "dm_upsert_pattern",
        "dm_upsert_table",
    } <= set(names)
