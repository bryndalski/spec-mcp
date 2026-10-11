"""Document status model, chunking, docs_check, evidence verification, upsert/supersede."""

import json
import subprocess
from pathlib import Path

import pytest

from spec_mcp import server
from spec_mcp.docscheck import glob_match, headings_of, slug
from spec_mcp.parse import chunk_doc, doc_meta, parse_frontmatter

FM_CURRENT = (
    "---\nstatus: current\ntopic: hosting\nowner: team\nverified_at: 2026-10-11\n---\n"
)
FM_OLD = "---\nstatus: superseded\nsuperseded_by: docs/NEW.md\ntopic: hosting\n---\n"
FM_HIST = "---\nstatus: historical\n---\n"


def _cfg(project: Path, **docs) -> None:
    cfg = json.loads((project / ".spec-mcp.json").read_text())
    cfg["docs"] = {
        "globs": ["docs/**/*.md"],
        "exclude": ["docs/archive/**"],
        "require_front_matter": True,
        "archive_dir": "docs/archive",
        "evidence_roots": ["src"],
        **docs,
    }
    (project / ".spec-mcp.json").write_text(json.dumps(cfg))


@pytest.fixture
def svc(project: Path, monkeypatch):
    monkeypatch.chdir(project)
    monkeypatch.setenv("SPEC_CONFIG", str(project / ".spec-mcp.json"))
    (project / "docs" / "HOSTING.md").write_text(
        FM_CURRENT + (project / "docs" / "HOSTING.md").read_text()
    )
    (project / "docs" / "OLD.md").write_text(
        FM_OLD + "# Old hosting\n\nThe gateway lambda handler serves everything.\n"
    )
    (project / "docs" / "NEW.md").write_text(
        "---\nstatus: current\n---\n# New hosting\n\nGateway routes to the service.\n"
    )
    (project / "docs" / "HIST.md").write_text(
        FM_HIST + "# Hist\n\ngateway lambda archive text\n"
    )
    (project / "docs" / "NOSTATUS.md").write_text(
        "# No status\n\nSee [missing](./nope.md) and [new](NEW.md#new-hosting) and [bad](NEW.md#zzz).\n\nPath `src/real/thing.ts` and `src/ghost/none.ts:3`.\n"
    )
    _cfg(project)
    server.reset()
    return project


# ---------------------------------------------------------------- front matter / chunking


def test_frontmatter_parse_tolerates_eof_and_lists():
    fm, body = parse_frontmatter(
        "---\nstatus: current\nsupersedes:\n  - a.md\n  - b.md\n---\n# T\n"
    )
    assert (
        fm["status"] == "current"
        and fm["supersedes"] == ["a.md", "b.md"]
        and body.startswith("# T")
    )
    assert parse_frontmatter("# no fm\n") == ({}, "# no fm\n")


def test_doc_meta_reads_status(svc):
    assert doc_meta(svc / "docs" / "OLD.md")["superseded_by"] == "docs/NEW.md"
    assert doc_meta(svc / "docs" / "NOSTATUS.md") == {}


def test_chunk_breadcrumb_and_front_matter_skipped(tmp_path: Path):
    f = tmp_path / "a.md"
    f.write_text(
        "---\nstatus: current\n---\n# Top\n\nintro\n\n## Mid\n\n### Deep\n\nbody\n\n## Other\n\nx\n"
    )
    chunks = chunk_doc(f, tmp_path)
    paths = [c["path"] for c in chunks]
    assert paths == ["Top", "Top > Mid", "Top > Mid > Deep", "Top > Other"]
    assert chunks[0]["line"] == 4 and "status" not in chunks[0]["text"]


def test_long_section_split_on_paragraphs_keeps_section_end(tmp_path: Path):
    f = tmp_path / "long.md"
    paras = "\n\n".join(f"paragraph {i} " + "word " * 60 for i in range(8))
    f.write_text(f"# Big\n\n{paras}\n\n# Next\n\nz\n")
    chunks = [c for c in chunk_doc(f, tmp_path, max_chars=500) if c["heading"] == "Big"]
    assert len(chunks) > 2
    assert all(len(c["text"]) <= 700 for c in chunks)
    assert {c["section_end"] for c in chunks} == {chunks[0]["section_end"]}
    assert (
        chunks[0]["part"] == 0
        and chunks[1]["part"] == 1
        and chunks[1]["line"] > chunks[0]["line"]
    )


def test_docs_get_returns_whole_split_section(svc):
    cfg = json.loads((svc / ".spec-mcp.json").read_text())
    cfg["docs"]["max_chunk_chars"] = 120
    (svc / ".spec-mcp.json").write_text(json.dumps(cfg))
    (svc / "docs" / "SPLIT.md").write_text(
        "---\nstatus: current\n---\n# S\n\n"
        + "\n\n".join(f"para {i} " + "x" * 80 for i in range(5))
        + "\n"
    )
    server.reset()
    out = server.docs_get("docs/SPLIT.md", heading="S")
    assert out["text"].count("para") == 5 and out["status"] == "current"


# ---------------------------------------------------------------- search / list visibility


def test_search_hides_superseded_and_historical_by_default(svc):
    out = server.docs_search("gateway lambda handler")
    files = {h["file"] for h in out["hits"]}
    assert "docs/OLD.md" not in files and "docs/HIST.md" not in files
    assert (
        out["hidden_files"] == 2
        and out["hidden_count"] == 2
        and out["hidden_chunks"] >= 2
    )
    assert all(h["status"] in ("current", "unknown") for h in out["hits"])


def test_search_include_stale_and_explicit_status(svc):
    both = {
        h["file"]
        for h in server.docs_search("gateway lambda", include_stale=True)["hits"]
    }
    assert {"docs/OLD.md", "docs/HIST.md"} <= both
    only = server.docs_search("gateway lambda", status="historical")
    assert {h["file"] for h in only["hits"]} == {"docs/HIST.md"} and only[
        "hidden_files"
    ] == 0


def test_search_topic_filter_and_breadcrumb(svc):
    out = server.docs_search("activation funnel", topic="hosting")
    assert out["hits"] and {h["file"] for h in out["hits"]} == {"docs/HOSTING.md"}
    assert out["hits"][0]["path"] == "Hosting > Activation funnel"


def test_unknown_status_not_hidden_when_front_matter_required(svc):
    out = server.docs_list()
    row = next(f for f in out["files"] if f["file"] == "docs/NOSTATUS.md")
    assert row["status"] == "unknown"
    assert out["hidden_count"] == 2


def test_status_current_when_front_matter_optional(project: Path, monkeypatch):
    monkeypatch.chdir(project)
    monkeypatch.setenv("SPEC_CONFIG", str(project / ".spec-mcp.json"))
    server.reset()
    assert all(f["status"] == "current" for f in server.docs_list()["files"])


# ---------------------------------------------------------------- docs_check


def test_docs_check_findings(svc):
    cfg = json.loads((svc / ".spec-mcp.json").read_text())
    cfg["docs"]["stale_patterns"] = [
        {
            "id": "lambda-live",
            "regex": "lambda handler",
            "reason": "Lambdas are decommissioned",
            "flags": "i",
            "exclude_statuses": ["historical", "superseded"],
        }
    ]
    (svc / ".spec-mcp.json").write_text(json.dumps(cfg))
    (svc / "docs" / "LIVE.md").write_text(
        "---\nstatus: current\n---\n# Live\n\nUses the lambda handler directly.\n"
    )
    (svc / "src" / "real").mkdir(parents=True)
    (svc / "src" / "real" / "thing.ts").write_text("export const x = 1;\n")
    server.reset()
    out = server.docs_check()
    rules = out["counts_by_rule"]
    assert (
        rules["no_status"] >= 1
    )  # NOSTATUS.md and the generated-free requirement docs
    assert rules["dead_link"] == 1 and rules["dead_anchor"] == 1
    stale = [f for f in out["findings"] if f["rule"] == "stale_pattern"]
    assert [f["file"] for f in stale] == [
        "docs/LIVE.md"
    ]  # OLD.md is superseded -> excluded
    unresolved = [f for f in out["findings"] if f["rule"] == "unresolved_path"]
    assert (
        len(unresolved) == 1
        and "src/ghost/none.ts" in unresolved[0]["detail"]
        and unresolved[0]["severity"] == "info"
    )
    assert out["checked"] == out["total_docs"] and out["by_status"]["superseded"] == 1


def test_docs_check_superseded_rules_and_historical_outside_archive(svc):
    (svc / "docs" / "BAD1.md").write_text("---\nstatus: superseded\n---\n# b\n")
    (svc / "docs" / "BAD2.md").write_text(
        "---\nstatus: superseded\nsuperseded_by: docs/GONE.md\n---\n# b\n"
    )
    (svc / "docs" / "BAD3.md").write_text("---\nstatus: weird\n---\n# b\n")
    server.reset()
    rules = {f["rule"] for f in server.docs_check()["findings"]}
    assert {
        "superseded_without_target",
        "superseded_by_missing",
        "unknown_status",
        "historical_outside_archive",
    } <= rules


def test_docs_check_checks_filter_and_unknown_rule(svc):
    out = server.docs_check(checks=["dead_link"])
    assert set(out["counts_by_rule"]) == {"dead_link"}
    assert server.docs_check(checks=["nope"])["error"]["category"] == "validation"


def test_glob_match_and_slug():
    assert glob_match("docs/a.md", "docs/**/*.md") and glob_match(
        "docs/x/y/a.md", "docs/**/*.md"
    )
    assert not glob_match("docs/archive/a.md", "docs/archive/*/x.md")
    assert slug("Activation `funnel` & more!") == "activation-funnel--more"
    assert headings_of("# A\n\n```\n# not\n```\n\n## A\n") == {"a", "a-1"}


# ---------------------------------------------------------------- evidence verification


@pytest.fixture
def code(project: Path, monkeypatch):
    monkeypatch.chdir(project)
    monkeypatch.setenv("SPEC_CONFIG", str(project / ".spec-mcp.json"))
    (project / "svc" / "a" / "shared").mkdir(parents=True)
    (project / "svc" / "b" / "shared").mkdir(parents=True)
    (project / "svc" / "a" / "mod").mkdir(parents=True)
    body = "\n".join(
        ["// pad"] * 9 + ["export function publish() {}"] + ["// pad"] * 10
    )
    (project / "svc" / "a" / "mod" / "pub.ts").write_text(body + "\n")
    (project / "svc" / "a" / "shared" / "util.ts").write_text("export const u = 1;\n")
    (project / "svc" / "b" / "shared" / "util.ts").write_text("export const u = 2;\n")
    (project / "outside").mkdir()
    (project / "outside" / "legacy.ts").write_text("x\n")
    _cfg(project, evidence_roots=["svc/a", "svc/b"])
    flows = {
        "$schema": "spec-mcp/flows/v1",
        "items": [
            {
                "id": "pub",
                "title": "Publish",
                "roles": [],
                "requirements": [],
                "steps": [
                    {
                        "n": "1",
                        "role": "admin",
                        "screen": "/schedule",
                        "action": "publish",
                        "evidence": "mod/pub.ts:3",
                        "anchors": [
                            {"file": "mod/pub.ts", "symbol": "publish", "line": 3}
                        ],
                    },
                    {
                        "n": "2",
                        "role": "",
                        "screen": "",
                        "action": "ok",
                        "evidence": "mod/pub.ts:10",
                        "anchors": [
                            {"file": "mod/pub.ts", "symbol": "publish", "line": 10}
                        ],
                    },
                    {
                        "n": "3",
                        "role": "",
                        "screen": "",
                        "action": "gone",
                        "evidence": "mod/nothere.ts:4",
                    },
                    {
                        "n": "4",
                        "role": "",
                        "screen": "",
                        "action": "dup",
                        "evidence": "shared/util.ts:1",
                    },
                    {
                        "n": "5",
                        "role": "",
                        "screen": "",
                        "action": "legacy",
                        "evidence": "outside/legacy.ts:1",
                    },
                    {
                        "n": "6",
                        "role": "",
                        "screen": "",
                        "action": "far",
                        "evidence": "mod/pub.ts:999",
                    },
                    {
                        "n": "7",
                        "role": "",
                        "screen": "",
                        "action": "sym",
                        "evidence": "mod/pub.ts:10",
                        "anchors": [
                            {"file": "mod/pub.ts", "symbol": "doesNotExist", "line": 10}
                        ],
                    },
                ],
                "async": [
                    {
                        "n": "1",
                        "runtime": "lambda",
                        "action": "mail",
                        "evidence": "mod/gone.ts:1",
                    }
                ],
            }
        ],
    }
    (project / "docs" / "flows" / "flows.json").write_text(
        json.dumps(flows, indent=2) + "\n"
    )
    server.reset()
    return project


def test_flow_verify_statuses(code):
    out = server.flow_verify(include_ok=True)
    by = {(i["where"]): i["status"] for i in out["items"]}
    assert by["step 1"] == "moved" and by["step 2"] == "ok"
    assert by["step 3"] == "file_missing" and by["step 4"] == "ambiguous"
    assert (
        by["step 5"] == "wrong_root"
        and by["step 6"] == "line_out_of_range"
        and by["step 7"] == "symbol_missing"
    )
    assert by["async 1"] == "file_missing"
    assert out["evidence_entries"] == sum(out["counts"].values()) == 8
    row4 = next(i for i in out["items"] if i["where"] == "step 4")
    assert sorted(row4["candidates"]) == [
        "svc/a/shared/util.ts",
        "svc/b/shared/util.ts",
    ]
    assert out["evidence_roots"] == ["svc/a", "svc/b"]


def test_flow_verify_default_hides_ok_and_unknown_flow(code):
    out = server.flow_verify()
    assert all(i["status"] != "ok" for i in out["items"]) and out["counts"]["ok"] == 1
    assert server.flow_verify(id="nope")["error"]["category"] == "not_found"


def test_flow_verify_fix_rewrites_moved_only(code):
    before = json.loads((code / "docs" / "flows" / "flows.json").read_text())
    out = server.flow_verify(fix=True)
    assert out["fixed_total"] == 1 and out["fixed"][0]["new_line"] == 10
    after = json.loads((code / "docs" / "flows" / "flows.json").read_text())
    s1 = after["items"][0]["steps"][0]
    assert s1["evidence"] == "mod/pub.ts:10" and s1["anchors"][0]["line"] == 10
    assert after["items"][0]["steps"][2] == before["items"][0]["steps"][2]  # untouched
    assert (
        after["items"][0]["async"] == before["items"][0]["async"]
        and after["$schema"] == before["$schema"]
    )
    again = server.flow_verify()
    assert again["counts"]["moved"] == 0 and again["fixed_total"] == 0


def test_flow_verify_fix_keeps_range_width(code):
    p = code / "docs" / "flows" / "flows.json"
    d = json.loads(p.read_text())
    d["items"][0]["steps"][0]["evidence"] = "mod/pub.ts:3-5"
    p.write_text(json.dumps(d))
    server.reset()
    server.flow_verify(fix=True)
    assert (
        json.loads(p.read_text())["items"][0]["steps"][0]["evidence"]
        == "mod/pub.ts:10-12"
    )


def test_req_verify_text_refs_and_fix(code):
    reqs = {
        "items": [
            {
                "id": "PUB-R1",
                "text": "Publish writes audit `mod/pub.ts:3` and `mod/nothere.ts:1`",
                "domain": "pub",
                "anchors": [{"file": "mod/pub.ts", "symbol": "publish", "line": 3}],
            },
            {"id": "OTH-R1", "text": "other `mod/pub.ts:10`", "domain": "oth"},
        ]
    }
    (code / "docs" / "requirements" / "requirements.json").write_text(json.dumps(reqs))
    server.reset()
    out = server.req_verify(domain="pub")
    assert out["checked_items"] == 1
    assert out["counts"]["moved"] == 1 and out["counts"]["file_missing"] == 1
    fixed = server.req_verify(domain="pub", fix=True)
    assert fixed["fixed_total"] == 1
    saved = json.loads(
        (code / "docs" / "requirements" / "requirements.json").read_text()
    )
    assert (
        "mod/pub.ts:10" in saved["items"][0]["text"]
        and saved["items"][0]["anchors"][0]["line"] == 10
    )
    assert saved["items"][1]["text"] == "other `mod/pub.ts:10`"


def test_spec_validate_flags_missing_screen_role_and_bad_runtime(code):
    cfg = json.loads((code / ".spec-mcp.json").read_text())
    cfg["flows"] = {
        **cfg["flows"],
        "globs": ["docs/flows/**/*.md", "docs/flows/**/*.json"],
        "require_screen_role": True,
    }
    (code / ".spec-mcp.json").write_text(json.dumps(cfg))
    d = json.loads((code / "docs" / "flows" / "flows.json").read_text())
    d["items"][0]["async"].append(
        {"n": "2", "runtime": "cron", "action": "x", "evidence": ""}
    )
    (code / "docs" / "flows" / "flows.json").write_text(json.dumps(d))
    server.reset()
    kinds = [f["kind"] for f in server.spec_validate()["findings"]]
    assert (
        kinds.count("step_missing_screen") == 6
        and kinds.count("step_missing_role") == 6
    )
    assert "invalid_async_runtime" in kinds


def test_flow_upsert_keeps_anchors_and_async(code):
    out = server.flow_upsert(
        "pub",
        "Publish",
        [
            {
                "n": "1",
                "action": "a",
                "evidence": "mod/pub.ts:10",
                "anchors": [{"file": "mod/pub.ts", "symbol": "publish", "line": 10}],
            }
        ],
        async_steps=[{"runtime": "sqs", "action": "queue", "evidence": ""}],
    )
    assert out["action"] == "updated"
    saved = json.loads((code / "docs" / "flows" / "flows.json").read_text())["items"][0]
    assert (
        saved["steps"][0]["anchors"][0]["symbol"] == "publish"
        and saved["async"][0]["runtime"] == "sqs"
    )
    bad = server.flow_upsert("x1", "T", [], async_steps=[{"runtime": "cron"}])
    assert bad["error"]["category"] == "validation"


# ---------------------------------------------------------------- docs_upsert / docs_supersede


def test_docs_upsert_updates_front_matter_and_keeps_body(svc):
    out = server.docs_upsert(
        "docs/NOSTATUS.md",
        front_matter={
            "status": "current",
            "topic": "misc",
            "verified_at": "2026-10-11",
            "supersedes": ["docs/OLD.md"],
        },
    )
    assert out["created"] is False
    text = (svc / "docs" / "NOSTATUS.md").read_text()
    assert text.startswith(
        "---\nstatus: current\ntopic: misc\nsupersedes:\n  - docs/OLD.md\nverified_at: 2026-10-11\n---\n\n# No status"
    )
    server.docs_upsert("docs/NOSTATUS.md", front_matter={"topic": None})
    assert "topic" not in (svc / "docs" / "NOSTATUS.md").read_text().split("---")[1]
    assert server.docs_list(status="current")["total"] >= 1


def test_docs_upsert_validation_and_scope(svc):
    assert (
        server.docs_upsert("docs/NEW.md", front_matter={"status": "bogus"})["error"][
            "category"
        ]
        == "validation"
    )
    assert (
        server.docs_upsert("docs/NEW.md", front_matter={"verified_at": "11/10/2026"})[
            "error"
        ]["category"]
        == "validation"
    )
    assert (
        server.docs_upsert("docs/archive/x.md", body="# x")["error"]["category"]
        == "validation"
    )
    assert (
        server.docs_upsert("README.md", body="# x")["error"]["category"] == "validation"
    )
    assert (
        server.docs_upsert("docs/FRESH.md", front_matter={"status": "draft"})["error"][
            "category"
        ]
        == "validation"
    )  # new file needs body
    made = server.docs_upsert(
        "docs/FRESH.md",
        front_matter={"status": "draft", "title": "Fresh: doc"},
        body="# Fresh\n",
    )
    assert (
        made["created"] is True
        and 'title: "Fresh: doc"' in (svc / "docs" / "FRESH.md").read_text()
    )


def _git(repo: Path, *a: str) -> None:
    subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)


def test_supersede_moves_with_git_mv_and_banner(svc):
    _git(svc, "init", "-q")
    _git(svc, "-c", "user.email=t@t", "-c", "user.name=t", "add", "docs")
    _git(svc, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    (svc / "docs" / "legacy").mkdir()
    (svc / "docs" / "legacy" / "OLDER.md").write_text(
        "---\nstatus: current\n---\n# Older\n\nsee [x](../NEW.md)\n"
    )
    _git(svc, "add", "docs/legacy/OLDER.md")
    _git(svc, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "older")
    (svc / "docs" / "LINKER.md").write_text(
        "---\nstatus: current\n---\n# Linker\n\n[older](legacy/OLDER.md)\n"
    )
    server.reset()
    plan = server.docs_supersede(
        "docs/legacy/OLDER.md", by="docs/NEW.md", reason="rewritten", dry_run=True
    )
    assert (
        plan["new_path"] == "docs/archive/legacy/OLDER.md"
        and plan["method"] == "none"
        and (svc / "docs/legacy/OLDER.md").exists()
    )
    assert plan["inbound_references"] == ["docs/LINKER.md"]
    out = server.docs_supersede(
        "docs/legacy/OLDER.md", by="docs/NEW.md", reason="rewritten"
    )
    assert out["method"] == "git mv" and out["moved"] is True
    moved = (svc / "docs" / "archive" / "legacy" / "OLDER.md").read_text()
    assert (
        "status: superseded" in moved
        and "superseded_by: docs/NEW.md" in moved
        and "> ZASTĄPIONY przez `docs/NEW.md` — rewritten (" in moved
    )
    assert not (svc / "docs/legacy/OLDER.md").exists()
    status = subprocess.run(
        ["git", "-C", str(svc), "status", "--porcelain"], capture_output=True, text=True
    ).stdout
    assert "docs/legacy/OLDER.md -> docs/archive/legacy/OLDER.md" in status


def test_supersede_rename_for_untracked_and_topic_group(svc):
    out = server.docs_supersede(
        "docs/OLD.md", by="docs/NEW.md", reason="superseded again"
    )
    assert (
        out["method"] == "rename" and out["new_path"] == "docs/archive/hosting/OLD.md"
    )  # topic wins over directory
    text = (svc / "docs" / "archive" / "hosting" / "OLD.md").read_text()
    assert text.count("ZASTĄPIONY przez") == 1


def test_supersede_refuses_missing_replacement_and_taken_destination(svc):
    r = server.docs_supersede("docs/HIST.md", by="docs/MISSING.md", reason="x")
    assert (
        r["error"]["category"] == "not_found"
        and "status: historical" in (svc / "docs" / "HIST.md").read_text()
    )
    (svc / "docs" / "archive" / "misc").mkdir(parents=True)
    (svc / "docs" / "archive" / "misc" / "HIST.md").write_text("taken")
    assert (
        server.docs_supersede("docs/HIST.md", by="docs/NEW.md", reason="x")["error"][
            "category"
        ]
        == "validation"
    )
    assert (
        server.docs_supersede("docs/HIST.md", by="docs/HIST.md", reason="x")["error"][
            "category"
        ]
        == "validation"
    )
    assert (
        server.docs_supersede("docs/HIST.md", by="docs/NEW.md", reason=" ")["error"][
            "category"
        ]
        == "validation"
    )


def test_supersede_without_archive_only_marks(svc):
    out = server.docs_supersede(
        "docs/NOSTATUS.md", by="docs/NEW.md", reason="r", move_to_archive=False
    )
    assert out["moved"] is False and out["new_path"] == "docs/NOSTATUS.md"
    row = next(
        f
        for f in server.docs_list(status="superseded")["files"]
        if f["file"] == "docs/NOSTATUS.md"
    )
    assert row["superseded_by"] == "docs/NEW.md"


def test_config_rejects_bad_stale_pattern_and_hidden_status(project: Path, monkeypatch):
    monkeypatch.chdir(project)
    monkeypatch.setenv("SPEC_CONFIG", str(project / ".spec-mcp.json"))
    _cfg(project, stale_patterns=[{"id": "x", "regex": "("}])
    server.reset()
    assert server.docs_list()["error"]["category"] == "config"
    _cfg(project, default_hidden_statuses=["nope"])
    server.reset()
    assert server.docs_list()["error"]["category"] == "config"
