import json
import os
import time
from pathlib import Path

from spec_mcp.config import load_config
from spec_mcp.index import SpecIndex


def make(project: Path) -> SpecIndex:
    cfg = load_config(project / ".spec-mcp.json", root=project)
    return SpecIndex(cfg)


def test_build_counts(project: Path):
    idx = make(project)
    idx.ensure()
    assert len(idx.requirements) == 5
    assert len(idx.flows) == 2
    assert idx.doc_files == 4  # HOSTING + 2 req files + PROCESSES (all under docs/**)
    assert idx.chunks


def test_cache_written_and_reused(project: Path):
    idx = make(project)
    idx.ensure()
    cache = project / ".spec-index.json"
    assert cache.exists()
    data = json.loads(cache.read_text())
    assert "fingerprint" in data and "requirements" in data
    idx2 = make(project)
    assert idx2.ensure() == "cache"


def test_stale_cache_rebuilds_when_file_changes(project: Path):
    idx = make(project)
    idx.ensure()
    f = project / "docs/requirements/domains/invitations.md"
    time.sleep(0.01)
    f.write_text(f.read_text() + "\n- INV-R3 · Third rule\n")
    os.utime(f, None)
    idx2 = make(project)
    assert idx2.ensure() == "rebuilt"
    assert "INV-R3" in idx2.requirements


def test_links_sidecar_roundtrip(project: Path):
    idx = make(project)
    idx.ensure()
    idx.add_link("ACT-R1", "test", "src/activation.spec.ts")
    sidecar = json.loads((project / ".spec-links.json").read_text())
    assert sidecar["ACT-R1"]["tests"] == ["src/activation.spec.ts"]
    # idempotent
    idx.add_link("ACT-R1", "test", "src/activation.spec.ts")
    assert json.loads((project / ".spec-links.json").read_text())["ACT-R1"]["tests"] == ["src/activation.spec.ts"]
