"""Local-RAG layer: config, sqlite cache, hybrid ranking, strict answers, errors. No network and no
real Ollama: every HTTP call goes through httpx.MockTransport."""

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from spec_mcp import ollama as ol
from spec_mcp import server
from spec_mcp.bm25 import tokenize
from spec_mcp.config import load_config
from spec_mcp.errors import SpecError

DIM = 64


def lexical_vec(text: str) -> list[float]:
    v = [0.0] * DIM
    for t in tokenize(text):
        v[int(hashlib.md5(t.encode()).hexdigest(), 16) % DIM] += 1.0
    return v


class FakeOllama:
    """Scriptable Ollama: /api/tags, /api/embed, /api/chat."""

    def __init__(
        self,
        models: list[str] | None = None,
        reply: str | Callable[[dict[str, Any]], str] = "Odpowiedź [1].",
        embed_fn: Callable[[str], list[float]] = lexical_vec,
    ):
        self.models = (
            models if models is not None else ["bge-m3:latest", "qwen2.5:7b-instruct"]
        )
        self.reply = reply
        self.embed_fn = embed_fn
        self.embedded: list[str] = []
        self.chats: list[dict[str, Any]] = []
        self.fail: Exception | None = None
        self.status: dict[str, int] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.fail is not None:
            raise self.fail
        path = request.url.path
        if path in self.status:
            return httpx.Response(
                self.status[path], json={"error": "model 'x' not found"}
            )
        if path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": m} for m in self.models]}
            )
        body = json.loads(request.content or b"{}")
        if path == "/api/embed":
            self.embedded.extend(body["input"])
            return httpx.Response(
                200, json={"embeddings": [self.embed_fn(t) for t in body["input"]]}
            )
        if path == "/api/chat":
            self.chats.append(body)
            text = self.reply(body) if callable(self.reply) else self.reply
            return httpx.Response(
                200, json={"message": {"role": "assistant", "content": text}}
            )
        return httpx.Response(404, json={"error": "no route"})


@pytest.fixture
def rag(project: Path, monkeypatch):
    """Project with the optional ollama layer configured; returns (fake, root, set_cfg)."""
    fake = FakeOllama()
    monkeypatch.setattr(ol, "_TRANSPORT", httpx.MockTransport(fake))
    monkeypatch.chdir(project)
    monkeypatch.setenv("SPEC_CONFIG", str(project / ".spec-mcp.json"))

    def set_cfg(**over: Any) -> None:
        cfg = json.loads((project / ".spec-mcp.json").read_text())
        o = {
            "cache_path": ".spec-embeddings.sqlite",
            "min_score": 0.2,
            "chat_model": "qwen2.5:7b-instruct",
        }
        o.update(over)
        cfg["ollama"] = o
        (project / ".spec-mcp.json").write_text(json.dumps(cfg))
        server.reset()

    set_cfg()
    return fake, project, set_cfg


# ------------------------------------------------------------------ configuration


def test_absent_section_means_not_configured(project: Path):
    cfg = load_config(project / ".spec-mcp.json", root=project)
    assert cfg["_ollama"] is None


def test_section_merges_defaults(project: Path):
    (project / ".spec-mcp.json").write_text(
        json.dumps({"ollama": {"chat_model": "m", "hybrid": {"embed": 0.9}}})
    )
    o = load_config(project / ".spec-mcp.json", root=project)["_ollama"]
    assert o["chat_model"] == "m" and o["embed_model"] == "bge-m3"
    assert o["hybrid"] == {"bm25": 0.35, "embed": 0.9}
    assert o["top_k"] == 8 and o["cache_path"] == ".spec-embeddings.sqlite"


@pytest.mark.parametrize(
    "bad",
    [
        {"nope": 1},
        {"corpora": ["docs", "wat"]},
        {"corpora": []},
        {"hybrid": {"bm25": 0, "embed": 0}},
        {"hybrid": {"x": 1}},
        {"base_url": "localhost:11434"},
        {"top_k": 0},
        {"min_score": -1},
    ],
)
def test_invalid_section_is_a_config_error(project: Path, bad):
    (project / ".spec-mcp.json").write_text(json.dumps({"ollama": bad}))
    with pytest.raises(SpecError) as e:
        load_config(project / ".spec-mcp.json", root=project)
    assert e.value.category == "config"


def test_unconfigured_tools_report_ollama_not_configured(project: Path, monkeypatch):
    monkeypatch.chdir(project)
    monkeypatch.setenv("SPEC_CONFIG", str(project / ".spec-mcp.json"))
    server.reset()
    for out in (
        server.ollama_status(),
        server.docs_reindex(),
        server.docs_semantic_search("x"),
        server.docs_ask("x"),
    ):
        assert out["error"]["category"] == "ollama_not_configured"
        assert "hint" in out["error"] and out["error"]["retryable"] is False


def test_bm25_tools_unchanged_without_ollama(project: Path, monkeypatch):
    monkeypatch.chdir(project)
    monkeypatch.setenv("SPEC_CONFIG", str(project / ".spec-mcp.json"))
    server.reset()
    assert server.docs_search("activation funnel")["hits"]
    assert server.spec_config()["ollama"] == {"configured": False}


# ------------------------------------------------------------------ indexing


def test_reindex_embeds_every_embeddable_item(rag):
    fake, root, _ = rag
    out = server.docs_reindex()
    assert out["embedded"] > 0 and out["embedded"] == out["total_chunks"] == len(
        fake.embedded
    )
    assert out["removed"] == 0 and out["by_corpus"]["docs"]["total"] > 0
    assert (root / ".spec-embeddings.sqlite").is_file()


def test_second_reindex_reuses_everything(rag):
    fake, _, _ = rag
    first = server.docs_reindex()
    n_calls = len(fake.embedded)
    second = server.docs_reindex()
    assert second["embedded"] == 0 and second["reused"] == first["total_chunks"]
    assert len(fake.embedded) == n_calls


def test_changed_content_reembeds_only_that_chunk(rag):
    fake, root, _ = rag
    server.docs_reindex()
    before = len(fake.embedded)
    p = root / "docs" / "HOSTING.md"
    p.write_text(p.read_text().replace("two-sided", "two-sided and quite different"))
    server.reset()
    out = server.docs_reindex()
    assert out["embedded"] == 1 and len(fake.embedded) == before + 1


def test_removed_document_removes_its_vectors(rag):
    _, root, _ = rag
    first = server.docs_reindex()
    (root / "docs" / "HOSTING.md").unlink()
    server.reset()
    out = server.docs_reindex()
    assert (
        out["removed"] >= 1
        and out["total_chunks"] == first["total_chunks"] - out["removed"]
    )


def test_embed_model_change_rebuilds_all(rag):
    fake, _, set_cfg = rag
    first = server.docs_reindex()
    set_cfg(embed_model="other-embed")
    out = server.docs_reindex()
    assert out["model_changed"] is True and out["embedded"] == first["total_chunks"]


def test_force_reembeds_all(rag):
    _, _, _ = rag
    first = server.docs_reindex()
    out = server.docs_reindex(force=True)
    assert out["embedded"] == first["total_chunks"]


def test_historical_documents_are_bm25_only(rag):
    fake, root, _ = rag
    (root / "docs" / "OLD.md").write_text(
        "---\nstatus: historical\ntopic: x\n---\n\n# Old\n\nLegacy lambda hosting notes.\n"
    )
    server.reset()
    out = server.docs_reindex()
    assert out["bm25_only_items"] >= 1
    assert not any("Legacy lambda" in t for t in fake.embedded)


def test_corpus_scope_limits_reindex(rag):
    fake, _, _ = rag
    out = server.docs_reindex(scope="requirements")
    assert out["embedded"] > 0 and set(out["by_corpus"]) >= {"docs", "requirements"}
    assert all("Aktualne" not in t for t in fake.embedded)
    assert out["by_corpus"]["docs"]["embedded"] == 0


# ------------------------------------------------------------------ retrieval


def test_semantic_search_finds_relevant_chunk_with_scores(rag):
    _, _, _ = rag
    server.docs_reindex()
    out = server.docs_semantic_search(
        "organization activation two-sided owner publishes schedule", limit=3
    )
    top = out["hits"][0]
    assert top["file"] == "docs/HOSTING.md" and top["heading"] == "Activation funnel"
    assert {
        "corpus",
        "kind",
        "score",
        "score_bm25",
        "score_embed",
        "status",
        "line",
    } <= set(top)
    assert (
        out["searched_chunks"] > 0
        and out["embedded_chunks"] > 0
        and out["stale_index"] is False
    )


def _two_docs(root: Path) -> None:
    for f in (root / "docs").glob("*.md"):
        f.unlink()
    (root / "docs" / "A.md").write_text("# Alpha\n\nzebra keyword only here.\n")
    (root / "docs" / "B.md").write_text("# Beta\n\nsomething unrelated about gamma.\n")


def test_hybrid_weights_decide_order(rag):
    fake, root, set_cfg = rag
    _two_docs(root)
    # embeddings say: B is near the query, A is far; BM25 says: only A contains "zebra"
    fake.embed_fn = lambda t: (
        [0.0, 1.0] if ("gamma" in t or t.startswith("zebra")) else [1.0, 0.0]
    )
    set_cfg(hybrid={"bm25": 0.0, "embed": 1.0})
    server.docs_reindex()
    assert server.docs_semantic_search("zebra")["hits"][0]["file"] == "docs/B.md"
    set_cfg(hybrid={"bm25": 1.0, "embed": 0.0})
    assert server.docs_semantic_search("zebra")["hits"][0]["file"] == "docs/A.md"


def test_stale_documents_hidden_unless_requested(rag):
    fake, root, _ = rag
    (root / "docs" / "OLD.md").write_text(
        "---\nstatus: historical\n---\n\n# Old hosting\n\nuniquewordzzz lives here.\n"
    )
    server.reset()
    server.docs_reindex()
    default = server.docs_semantic_search("uniquewordzzz")
    assert not [h for h in default["hits"] if h["file"].endswith("OLD.md")]
    shown = server.docs_semantic_search("uniquewordzzz", include_stale=True)
    hit = next(h for h in shown["hits"] if h["file"].endswith("OLD.md"))
    assert hit["status"] == "historical" and hit["score_embed"] == 0


def test_corpus_filter(rag):
    server.docs_reindex()
    only_req = server.docs_semantic_search("invitation", corpus="requirements")
    assert only_req["hits"] and all(
        h["corpus"] == "requirements" for h in only_req["hits"]
    )
    only_flow = server.docs_semantic_search("signup activation", corpus="flows")
    assert only_flow["hits"] and all(h["corpus"] == "flows" for h in only_flow["hits"])


def test_lazy_refresh_embeds_small_backlog(rag):
    fake, root, _ = rag
    server.docs_reindex()
    p = root / "docs" / "HOSTING.md"
    p.write_text(
        p.read_text() + "\n## Brand new\n\nfreshwordqq appears after the reindex.\n"
    )
    server.reset()
    out = server.docs_semantic_search("freshwordqq")
    assert out["stale_index"] is False and out["pending_embeddings"] == 0
    assert out["hits"][0]["heading"] == "Brand new"


def test_large_backlog_is_not_embedded_lazily(rag):
    fake, root, set_cfg = rag
    set_cfg(lazy_embed_limit=1)
    out = server.docs_semantic_search("activation")
    assert out["stale_index"] is True and "docs_reindex" in out["index_hint"]
    assert fake.embedded == ["activation"]  # only the query was embedded


def test_records_corpus_is_retrievable(rag, project: Path):
    fake, root, set_cfg = rag
    (root / "claims.json").write_text(
        json.dumps(
            {
                "records": [
                    {"id": "CL-1", "text": "pricing plans claim", "status": "verified"}
                ]
            }
        )
    )
    cfg = json.loads((root / ".spec-mcp.json").read_text())
    cfg["records"] = {"claims": {"globs": ["claims.json"], "write_file": "claims.json"}}
    (root / ".spec-mcp.json").write_text(json.dumps(cfg))
    set_cfg(corpora=["docs", "records:claims"])
    server.docs_reindex()
    out = server.docs_semantic_search("pricing plans claim", corpus="records")
    assert out["hits"][0]["kind"] == "record" and out["hits"][0]["heading"] == "CL-1"


# ------------------------------------------------------------------ docs_ask


def test_ask_returns_grounded_cited_answer(rag):
    fake, _, _ = rag
    server.docs_reindex()
    out = server.docs_ask(
        "how does organization activation work two-sided owner publishes schedule"
    )
    assert (
        out["answer"] == "Odpowiedź [1]."
        and out["grounded"] is True
        and out["citations_valid"] is True
    )
    assert (
        out["sources"][0]["n"] == 1 and out["sources"][0]["path"] == "docs/HOSTING.md"
    )
    assert out["model"] == "qwen2.5:7b-instruct" and out["confidence"] in (
        "high",
        "medium",
    )
    assert out["retrieval"]["searched_chunks"] > 0 and out["latency_ms"] >= 0
    assert len(fake.chats) == 1


def test_ask_prompt_contains_rules_question_and_numbered_sources(rag):
    fake, _, _ = rag
    server.docs_reindex()
    server.docs_ask("activation two-sided owner publishes schedule")
    msgs = fake.chats[0]["messages"]
    assert (
        msgs[0]["role"] == "system"
        and "WYŁĄCZNIE na podstawie źródeł" in msgs[0]["content"]
    )
    assert "brak w dokumentacji" in msgs[0]["content"]
    user = msgs[1]["content"]
    assert (
        "Pytanie: activation two-sided" in user and "[1] (doc, docs/HOSTING.md" in user
    )
    assert (
        fake.chats[0]["stream"] is False and fake.chats[0]["options"]["num_ctx"] == 8192
    )


def test_ask_strict_refuses_below_threshold_without_calling_generator(rag):
    fake, _, set_cfg = rag
    set_cfg(min_score=0.95)
    server.docs_reindex()
    out = server.docs_ask("completely unrelated question about helicopters")
    assert (
        out["error"]["category"] == "insufficient_evidence"
        and out["error"]["retryable"] is False
    )
    assert fake.chats == [] and "nearest" in out and out["best_score"] < 0.95


def test_ask_not_strict_calls_generator_anyway(rag):
    fake, _, set_cfg = rag
    set_cfg(min_score=0.95)
    server.docs_reindex()
    out = server.docs_ask("activation two-sided", strict=False)
    assert "error" not in out and len(fake.chats) == 1 and out["confidence"] == "low"


def test_ask_invalid_citation_is_flagged(rag):
    fake, _, _ = rag
    fake.reply = "Tak, tak jest zawsze [99]."
    server.docs_reindex()
    out = server.docs_ask("activation two-sided owner publishes schedule")
    assert (
        out["citations_valid"] is False
        and out["grounded"] is False
        and out["confidence"] == "low"
    )


def test_ask_detects_uncited_sentences(rag):
    fake, _, _ = rag
    fake.reply = "To jest długie zdanie bez żadnego odwołania do źródeł. Drugie zdanie ma odwołanie [1]."
    server.docs_reindex()
    out = server.docs_ask("activation two-sided owner publishes schedule")
    assert len(out["uncited_sentences"]) == 1 and out["uncited_sentences"][
        0
    ].startswith("To jest długie")


def test_ask_no_answer_reply_is_low_confidence(rag):
    fake, _, _ = rag
    fake.reply = "brak w dokumentacji — nie ma informacji o stawkach."
    server.docs_reindex()
    out = server.docs_ask("activation two-sided owner publishes schedule")
    assert out["no_answer"] is True and out["confidence"] == "low"


def test_ask_model_override_and_corpus(rag):
    fake, _, _ = rag
    server.docs_reindex()
    out = server.docs_ask("invitation", model="bielik", corpus="requirements")
    assert fake.chats[0]["model"] == "bielik" and out["model"] == "bielik"
    assert all(s["corpus"] == "requirements" for s in out["sources"])


def test_ask_checks_flow_evidence_against_code(rag):
    fake, root, set_cfg = rag
    (root / "src").mkdir()
    (root / "src" / "svc.ts").write_text("a\nb\nexport function approveSwap() {}\nc\n")
    flow = {
        "items": [
            {
                "id": "swap",
                "title": "Swap approval",
                "steps": [
                    {
                        "n": "1",
                        "role": "MANAGER",
                        "screen": "x",
                        "action": "manager approves the swap",
                        "evidence": "`src/svc.ts:3`",
                        "anchors": [
                            {"file": "src/svc.ts", "symbol": "approveSwap", "line": 3}
                        ],
                    },
                    {
                        "n": "2",
                        "role": "MANAGER",
                        "screen": "x",
                        "action": "manager rejects the swap",
                        "evidence": "`src/gone.ts:9`",
                        "anchors": [
                            {"file": "src/gone.ts", "symbol": "rejectSwap", "line": 9}
                        ],
                    },
                ],
            }
        ]
    }
    (root / "docs" / "flows" / "swap.json").write_text(json.dumps(flow))
    cfg = json.loads((root / ".spec-mcp.json").read_text())
    cfg["flows"]["globs"] = ["docs/flows/**/*.md", "docs/flows/**/*.json"]
    (root / ".spec-mcp.json").write_text(json.dumps(cfg))
    set_cfg(corpora=["flows"])
    server.docs_reindex()
    out = server.docs_ask("manager approves the swap", top_k=4)
    by_heading = {s["heading"]: s for s in out["sources"]}
    assert by_heading["Swap approval > krok 1"]["evidence_ok"] is True
    assert by_heading["Swap approval > krok 2"]["evidence_ok"] is False


# ------------------------------------------------------------------ status and errors


def test_status_reports_models_and_index(rag):
    _, _, _ = rag
    server.docs_reindex()
    out = server.ollama_status()
    assert out["available"] is True and out["models_present"] == {
        "embed_model": True,
        "chat_model": True,
    }
    assert (
        out["index"]["exists"]
        and out["index"]["chunks"] > 0
        and out["index"]["pending"] == 0
    )


def test_status_flags_missing_model(rag):
    fake, _, _ = rag
    fake.models = ["qwen2.5:7b-instruct"]
    assert server.ollama_status()["models_present"] == {
        "embed_model": False,
        "chat_model": True,
    }


def test_server_down_is_a_retryable_structured_error(rag):
    fake, _, _ = rag
    fake.fail = httpx.ConnectError("refused")
    for out in (
        server.docs_reindex(),
        server.docs_semantic_search("x"),
        server.docs_ask("x"),
    ):
        e = out["error"]
        assert e["category"] == "ollama_unavailable" and e["retryable"] is True
        assert "open -a Ollama" in e["hint"]
    st = server.ollama_status()
    assert st["available"] is False and st["error"]["category"] == "ollama_unavailable"


def test_missing_model_error_has_pull_hint(rag):
    fake, _, _ = rag
    fake.status["/api/embed"] = 404
    out = server.docs_reindex()
    assert (
        out["error"]["category"] == "model_missing"
        and "ollama pull bge-m3" in out["error"]["hint"]
    )


# ------------------------------------------------------------------ units


def test_parse_citations_and_uncited_helpers():
    assert ol.parse_citations("A [1] i B [2, 3] oraz [4][5]") == [[1], [2, 3], [4], [5]]
    assert ol.parse_citations("brak odwołań") == []
    assert ol.uncited_sentences(
        "Krótkie. Brak w dokumentacji tego faktu, więc nie zgaduję. Zdanie bez źródła, ale długie. Dobre [1]."
    ) == ["Zdanie bez źródła, ale długie."]


def test_store_roundtrips_float32_vectors(tmp_path: Path):
    s = ol.Store(tmp_path / "v.sqlite")
    s.put("k", "docs", "h", [0.5, -0.25, 1.0])
    s.commit()
    assert s.count() == 1 and s.hashes() == {"k": "h"}
    assert list(s.vectors()["k"]) == [0.5, -0.25, 1.0]
    assert s.delete(["k"]) == 1 and s.count() == 0
    s.close()


def test_cosine_scan_of_10k_vectors_is_fast(rag, monkeypatch):
    _, root, _ = rag
    server.docs_reindex()
    eng = server._eng()
    import array
    import random

    rnd = random.Random(1)
    vecs = {}
    for i in range(10_000):
        v = [rnd.random() - 0.5 for _ in range(256)]
        n = sum(x * x for x in v) ** 0.5
        vecs[f"k{i}"] = array.array("f", [x / n for x in v])
    monkeypatch.setattr(eng, "_load_vectors", lambda: vecs)
    monkeypatch.setattr(ol, "np", None)  # worst case: pure python
    q = [1.0 / 16] * 256
    t0 = time.time()
    out = eng._cosines(q)
    assert len(out) == 10_000 and time.time() - t0 < 3.0
