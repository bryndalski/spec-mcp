import json
from pathlib import Path

import pytest

from spec_mcp import server

ALERT = {
    "name": "AlertBanner",
    "import": "import { AlertBanner } from '@duotio/ui/AlertBanner'",
    "description": "Inline status message with four tones",
    "category": "feedback",
    "props": [
        {
            "name": "variant",
            "type": "'info' | 'success'",
            "default": "'info'",
            "required": False,
            "desc": "Tone of the banner",
        },
        {
            "name": "dismissible",
            "type": "boolean",
            "default": "false",
            "required": False,
            "desc": "Adds a close button",
        },
    ],
    "variants": {"variant": ["info", "success"]},
    "tokensUsed": ["--info", "--ring", "--ghost-token"],
    "a11y": {"role": "alert"},
    "examples": [{"title": "Dismissible info", "code": "<AlertBanner dismissible />"}],
    "dependsOn": ["class-variance-authority", "Button", "GhostComponent"],
}
BUTTON = {
    "name": "Button",
    "import": "import { Button } from '@duotio/ui/Button'",
    "description": "Primary action control with evergreen chrome",
    "category": "action",
    "props": [
        {
            "name": "size",
            "type": "'sm' | 'md'",
            "default": "'md'",
            "required": False,
            "desc": "Control size",
        }
    ],
    "variants": {},
    "tokensUsed": ["--ring"],
    "a11y": {},
    "examples": [],
    "dependsOn": [],
}
# on disk but not in index.json
TOAST = {
    "name": "Toast",
    "import": "import { Toast } from '@duotio/ui/Toast'",
    "description": "Transient notification",
    "category": "feedback",
    "props": [],
    "tokensUsed": [],
    "examples": [],
    "dependsOn": [],
}

ALL_FILES = [
    ".tsx",
    ".test.tsx",
    ".e2e.ts",
    ".stories.tsx",
    ".md",
    ".registry.json",
    "index.ts",
]


def _component(root: Path, reg: dict, files: list[str]):
    d = root / "ui/src/components" / reg["name"]
    d.mkdir(parents=True)
    (d / f"{reg['name']}.registry.json").write_text(json.dumps(reg))
    for f in files:
        name = f"{reg['name']}{f}" if f.startswith(".") else f
        if name.endswith(".registry.json"):
            continue
        (d / name).write_text("// stub\n")


@pytest.fixture
def svc(project: Path, monkeypatch):
    _component(project, ALERT, ALL_FILES)
    _component(project, BUTTON, [".tsx", "index.ts"])  # missing test/e2e/stories/md
    _component(project, TOAST, ALL_FILES)
    (project / "ui/registry").mkdir(parents=True)
    (project / "ui/registry/index.json").write_text(
        json.dumps(
            {
                "$meta": {"count": 3},
                "components": [
                    {"name": "AlertBanner"},
                    {"name": "Button"},
                    {"name": "Modal"},
                ],
            }
        )
    )
    (project / "ui/src/styles").mkdir(parents=True)
    (project / "ui/src/styles/tokens.css").write_text(
        ":root {\n  --info: #0a0;\n  --ring: #000;\n  --r-control: 8px;\n}\n"
    )
    (project / "messages").mkdir()
    (project / "messages/pl.json").write_text(
        json.dumps({"schedule": {"title": "Grafik", "publish": {"cta": "Publikuj"}}})
    )
    (project / "docs/design/views").mkdir(parents=True)
    (project / "docs/design/views/schedule-week.json").write_text(
        json.dumps(
            {
                "id": "schedule-week",
                "title": "Week schedule",
                "route": "/app/[slug]/schedule",
                "roles": ["manager"],
                "flow": "1-from-signup-to-activation",
                "requirements": ["ACT-R1", "NOPE-R9"],
                "status": "implemented",
                "artifact_url": "",
                "artboards": [
                    {
                        "name": "default",
                        "viewport": 390,
                        "theme": "light",
                        "url": "https://x/1",
                    }
                ],
                "brief": {
                    "goal": "publish a week",
                    "primary_action": "Publish",
                    "style": "calm",
                    "decoration_budget": 1,
                    "references": [],
                },
                "components": [
                    {
                        "name": "AlertBanner",
                        "source": "duotio-ui",
                        "path": "",
                        "props": ["variant"],
                        "states": ["info"],
                        "i18n_keys": [
                            "schedule.title",
                            "schedule.publish.cta",
                            "schedule.missing",
                        ],
                        "tests": [],
                    },
                    {
                        "name": "Ghost",
                        "source": "duotio-ui",
                        "path": "",
                        "props": [],
                        "states": [],
                        "i18n_keys": [],
                        "tests": [],
                    },
                    {
                        "name": "WeekGrid",
                        "source": "feature",
                        "path": "",
                        "props": [],
                        "states": [],
                        "i18n_keys": [],
                        "tests": [],
                    },
                ],
                "acceptance": [],
                "decisions": [],
                "evidence": [],
            }
        )
    )
    cfg = json.loads((project / ".spec-mcp.json").read_text())
    cfg["components"] = {
        "globs": ["ui/src/components/*/*.registry.json"],
        "index_file_globs": ["ui/registry/index.json"],
        "tokens_css": "ui/src/styles/tokens.css",
    }
    cfg["designs"] = {
        "globs": ["docs/design/views/*.json"],
        "write_dir": "docs/design/views",
        "messages_file": "messages/pl.json",
    }
    (project / ".spec-mcp.json").write_text(json.dumps(cfg))
    monkeypatch.chdir(project)
    monkeypatch.setenv("SPEC_CONFIG", str(project / ".spec-mcp.json"))
    server.reset()
    return project


# ---------------------------------------------------------------- components


def test_ui_components_list(svc):
    out = server.ui_components()
    assert out["total"] == 3 and out["scanned_files"] == 3
    alert = next(c for c in out["items"] if c["name"] == "AlertBanner")
    assert (
        alert["category"] == "feedback"
        and alert["props"] == 2
        and alert["import"].startswith("import {")
    )
    assert alert["files"] == {"present": 7, "expected": 7}
    button = next(c for c in out["items"] if c["name"] == "Button")
    assert button["files"] == {"present": 3, "expected": 7}
    assert server.ui_components(category="action")["total"] == 1
    assert server.spec_config()["counts"]["components"] == 3


def test_ui_component_full(svc):
    out = server.ui_component("Button")
    assert out["component"]["props"][0]["name"] == "size"
    assert (
        out["files"]["Button.tsx"] is True
        and out["files"]["Button.test.tsx"] is False
        and out["files"]["index.ts"] is True
    )
    assert out["missing"] == [
        "Button.test.tsx",
        "Button.e2e.ts",
        "Button.stories.tsx",
        "Button.md",
    ]
    assert out["md"] is None
    assert (
        server.ui_component("AlertBanner")["md"]
        == "ui/src/components/AlertBanner/AlertBanner.md"
    )
    assert server.ui_component("Nope")["error"]["category"] == "not_found"


def test_ui_find(svc):
    out = server.ui_find("dismissible banner close button")
    assert out["hits"][0]["name"] == "AlertBanner" and out["searched"] == 3
    out = server.ui_find("primary action evergreen")
    assert out["hits"][0]["name"] == "Button"
    assert server.ui_find("zzzz-nothing")["hits"] == []


def test_ui_validate(svc):
    out = server.ui_validate()
    kinds = {}
    for f in out["findings"]:
        kinds.setdefault(f["kind"], []).append(f)
    assert out["checked"] == {
        "components": 3,
        "index_entries": 3,
        "tokens": 3,
        "expected_files": 21,
    }
    assert (
        kinds["missing_file"][0]["component"] == "Button"
        and len(kinds["missing_file"]) == 4
    )
    assert kinds["unknown_token"] == [
        {"kind": "unknown_token", "component": "AlertBanner", "token": "--ghost-token"}
    ]
    assert [f["component"] for f in kinds["not_in_index"]] == ["Toast"]
    assert [f["component"] for f in kinds["not_on_disk"]] == ["Modal"]
    assert kinds["unknown_dependency"] == [
        {
            "kind": "unknown_dependency",
            "component": "AlertBanner",
            "dependsOn": "GhostComponent",
        }
    ]


def test_ui_aggregate(svc):
    assert server.ui_aggregate(by="category")["buckets"] == {"action": 1, "feedback": 2}
    assert server.ui_aggregate(by="token")["buckets"] == {
        "--ghost-token": 1,
        "--info": 1,
        "--ring": 2,
    }
    assert server.ui_aggregate(by="dependsOn")["buckets"] == {
        "Button": 1,
        "GhostComponent": 1,
        "class-variance-authority": 1,
    }
    assert server.ui_aggregate(by="colour")["error"]["category"] == "validation"


# ---------------------------------------------------------------- designs


def test_design_list_get(svc):
    out = server.design_list()
    assert (
        out["total"] == 1
        and out["items"][0]["id"] == "schedule-week"
        and out["items"][0]["components"] == 3
    )
    assert server.design_list(status="brief")["total"] == 0
    assert server.design_list(status="implemented")["total"] == 1
    got = server.design_get("schedule-week")
    assert (
        got["design"]["route"].startswith("/app")
        and got["file"] == "docs/design/views/schedule-week.json"
    )
    assert server.design_get("nope")["error"]["category"] == "not_found"
    assert server.design_list(status="bogus")["error"]["category"] == "validation"


def test_design_upsert(svc: Path):
    out = server.design_upsert(
        {"id": "login", "title": "Login", "route": "/login", "status": "brief"}
    )
    assert out["action"] == "created" and out["file"] == "docs/design/views/login.json"
    data = json.loads((svc / "docs/design/views/login.json").read_text())
    assert list(data.keys())[:5] == ["id", "title", "route", "roles", "flow"]
    assert (
        data["status"] == "brief"
        and data["components"] == []
        and data["artboards"] == []
    )
    out2 = server.design_upsert(
        {
            "id": "login",
            "status": "designed",
            "artifact_url": "https://claude.ai/artifact/x",
        }
    )
    assert out2["action"] == "updated"
    data = json.loads((svc / "docs/design/views/login.json").read_text())
    assert data["title"] == "Login" and data["status"] == "designed"
    assert server.design_list()["total"] == 2
    assert server.design_upsert({"title": "no id"})["error"]["category"] == "validation"
    assert (
        server.design_upsert({"id": "x", "status": "weird"})["error"]["category"]
        == "validation"
    )
    assert server.design_upsert({"id": "Bad Id"})["error"]["category"] == "validation"


def test_design_validate(svc):
    out = server.design_validate()
    kinds = {}
    for f in out["findings"]:
        kinds.setdefault(f["kind"], []).append(f)
    assert out["checked"]["designs"] == 1 and out["checked"]["components"] == 3
    assert kinds["component_not_in_registry"] == [
        {
            "kind": "component_not_in_registry",
            "design": "schedule-week",
            "component": "Ghost",
        }
    ]
    assert kinds["component_without_path"][0]["component"] == "WeekGrid"
    assert kinds["unknown_requirement"][0]["requirement"] == "NOPE-R9"
    assert kinds["missing_artifact_url"][0]["design"] == "schedule-week"
    assert kinds["missing_artboards"][0]["have"] == ["390/light"]
    assert sorted(f["key"] for f in kinds["missing_i18n_key"]) == ["schedule.missing"]
    assert kinds["empty_acceptance"][0]["design"] == "schedule-week"
    assert server.design_validate(id="nope")["error"]["category"] == "not_found"
    # a brief does not need an artifact, artboards or acceptance
    server.design_upsert({"id": "login", "status": "brief"})
    assert server.design_validate(id="login")["total"] == 0


def test_design_coverage(svc):
    server.design_upsert(
        {
            "id": "login",
            "status": "brief",
            "components": [
                {"name": "Button", "source": "duotio-ui"},
                {"name": "X", "source": "new-duotio-ui", "path": "src/components/X"},
            ],
        }
    )
    cov = server.design_coverage()
    assert cov["by_status"] == {
        "brief": 1,
        "designed": 0,
        "specified": 0,
        "implemented": 1,
        "verified": 0,
    }
    assert cov["components_by_source"] == {
        "duotio-ui": 3,
        "feature": 1,
        "new-duotio-ui": 1,
    }
    assert cov["denominator"] == {
        "designs": 2,
        "design_components": 5,
        "registry_components": 3,
    }


def test_spec_index_writes_design_index(svc: Path):
    out = server.spec_index()
    assert out["designs"] == 1 and "docs/design/views/INDEX.md" in out["written"]
    txt = (svc / "docs/design/views/INDEX.md").read_text()
    assert "schedule-week" in txt and "implemented" in txt
    assert server.design_list()["total"] == 1  # INDEX.md is not a design


def test_frontend_tools_registered():
    names = set(server.mcp._tool_manager._tools)  # noqa: SLF001
    assert len(names) == 49
    assert {
        "ui_components",
        "ui_component",
        "ui_find",
        "ui_validate",
        "ui_aggregate",
        "design_list",
        "design_get",
        "design_upsert",
        "design_validate",
        "design_coverage",
    } <= names
