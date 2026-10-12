"""0.7.0 retrieval tuning: boosts, reserved slots, adaptive top_k, context fitting, prompt rules,
gap-consistency post-check, structured routing. No network, no real Ollama."""

import json
from pathlib import Path

import pytest

from spec_mcp import ollama as ol
from spec_mcp import server
from spec_mcp.config import load_config
from spec_mcp.errors import SpecError

from test_ollama import FakeOllama, rag  # noqa: F401  (fixtures reused)

TWIN = "unikalnaxyz pricing plans gateway domains hosting"


def _doc(root: Path, name: str, status: str | None, body: str = TWIN) -> None:
    fm = f"---\nstatus: {status}\ntopic: t\n---\n\n" if status else ""
    (root / "docs" / name).write_text(
        f"{fm}# Twin {name}\n\n{body}\n", encoding="utf-8"
    )


def _routes(root: Path, rows: list[dict]) -> None:
    (root / "routes.json").write_text(json.dumps({"records": rows}), encoding="utf-8")
    cfg = json.loads((root / ".spec-mcp.json").read_text())
    cfg["records"] = {"routes": {"globs": ["routes.json"], "write_file": "routes.json"}}
    (root / ".spec-mcp.json").write_text(json.dumps(cfg))


# ------------------------------------------------------------------ configuration


def test_defaults_are_neutral(project: Path):
    (project / ".spec-mcp.json").write_text(json.dumps({"ollama": {"chat_model": "m"}}))
    o = load_config(project / ".spec-mcp.json", root=project)["_ollama"]
    assert o["rank"] == {"status_boost": {}, "kind_boost": {}} and o["reserve"] == {}
    assert o["top_k_list"] == 12 and o["max_context_chars"] == 14000
    assert o["route_structured"] is False and o["system_prompt"] is None


def test_rank_section_merges_per_key(project: Path):
    (project / ".spec-mcp.json").write_text(
        json.dumps({"ollama": {"rank": {"kind_boost": {"record": 1.1}}}})
    )
    o = load_config(project / ".spec-mcp.json", root=project)["_ollama"]
    assert (
        o["rank"]["kind_boost"] == {"record": 1.1} and o["rank"]["status_boost"] == {}
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"rank": {"nope": {}}},
        {"rank": {"kind_boost": {"wat": 1.0}}},
        {"rank": {"status_boost": {"draft": -1}}},
        {"reserve": {"wat": 1}},
        {"reserve": {"record": -1}},
        {"route_terms": [""]},
        {"top_k_list": 0},
        {"max_context_chars": 10},
        {"route_structured": "yes"},
        {"structured_patterns": [{"pattern": "(", "tool": "req_list"}]},
        {"structured_patterns": [{"pattern": "x", "tool": "rec_upsert"}]},
        {"system_prompt": " "},
    ],
)
def test_invalid_tuning_is_a_config_error(project: Path, bad):
    (project / ".spec-mcp.json").write_text(json.dumps({"ollama": bad}))
    with pytest.raises(SpecError) as e:
        load_config(project / ".spec-mcp.json", root=project)
    assert e.value.category == "config"


# ------------------------------------------------------------------ question classification


@pytest.mark.parametrize(
    "q,terms,hit",
    [
        ("Którą trasą publikuję grafik?", ["trasa"], True),
        ("Jakie trasy ma moduł?", ["trasa"], True),
        ("Opisz endpointy platformy", ["endpoint"], True),
        ("POST /x publish", ["POST"], True),
        (
            "post factum analiza",
            ["POST"],
            False,
        ),  # ALL-CAPS terms are case-sensitive words
        ("Co nadal działa na Lambdach?", ["co nadal"], True),
        ("Ile tras ma Nest?", ["ile"], True),
        ("pilesto", ["ile"], False),  # short terms need an exact word
        ("Opisz proces zamiany", ["proces"], True),
        ("Jaki jest termin?", ["wymień"], False),
    ],
)
def test_term_hit(q, terms, hit):
    assert ol.term_hit(q, terms) is hit


def test_structured_route_matches_and_skips_missing_collections(project: Path):
    _routes(project, [{"id": "a", "kind": "rest"}])
    cfg = load_config(project / ".spec-mcp.json", root=project)
    r = ol.structured_route(
        "Które wymagania są niezaimplementowane?", ol.DEFAULT_STRUCTURED_PATTERNS, cfg
    )
    assert (
        r
        and r["tool"] == "req_list"
        and r["arguments"] == {"status": "not_implemented"}
    )
    r = ol.structured_route(
        "Ile tras REST ma backend?", ol.DEFAULT_STRUCTURED_PATTERNS, cfg
    )
    assert (
        r and r["tool"] == "rec_aggregate" and r["arguments"]["collection"] == "routes"
    )
    r = ol.structured_route(
        "Jaką rolę ma trasa publish?", ol.DEFAULT_STRUCTURED_PATTERNS, cfg
    )
    assert r and r["tool"] == "rec_list" and r["execute"] is False
    assert (
        ol.structured_route(
            "Jak działa zamiana zmian?", ol.DEFAULT_STRUCTURED_PATTERNS, cfg
        )
        is None
    )


def test_structured_route_skips_pattern_when_collection_absent(project: Path):
    cfg = load_config(project / ".spec-mcp.json", root=project)  # no records configured
    assert (
        ol.structured_route(
            "Ile tras REST ma backend?", ol.DEFAULT_STRUCTURED_PATTERNS, cfg
        )
        is None
    )


# ------------------------------------------------------------------ gap consistency


def test_gap_consistency_flags_contradiction_about_the_asked_entity():
    q = "Czy endpoint platform dunning istnieje w Nest?"
    a = "Frontend woła /platform/dunning, a w Nest go nie ma [1]. Brak w dokumentacji: endpoint platform dunning w Nest."
    assert ol._gap_consistency(a, q) == (True, True)


def test_gap_consistency_unrelated_gap_is_partial_but_not_contradictory():
    q = "Czy endpoint platform dunning istnieje w Nest?"
    a = "Endpoint platform dunning nie istnieje w Nest [1]. Brak w dokumentacji: harmonogram wdrożenia."
    assert ol._gap_consistency(a, q) == (True, False)


def test_gap_consistency_needs_both_content_and_gap():
    assert ol._gap_consistency(
        "Brak w dokumentacji: cokolwiek o stawce.", "stawka kierownika"
    ) == (False, False)
    assert ol._gap_consistency(
        "Pełna odpowiedź bez luk i bez dopisku [1].", "pytanie"
    ) == (False, False)


# ------------------------------------------------------------------ prompt


def test_default_prompt_carries_the_new_rules():
    p = ol.SYSTEM_PROMPT
    for needle in (
        "KAŻDĄ część",
        "Brak w dokumentacji:",
        "0 trafień",
        "bez wywołań",
        "[n]",
        "po polsku",
    ):
        assert needle in p
    assert ol._NO_ANSWER.search(p)


def test_system_prompt_override_and_file(rag):
    fake, root, set_cfg = rag
    server.docs_reindex()
    set_cfg(system_prompt="MÓJ PROMPT")
    server.docs_ask(
        "how does organization activation work two-sided owner publishes schedule"
    )
    assert fake.chats[-1]["messages"][0]["content"] == "MÓJ PROMPT"
    (root / "prompt.txt").write_text("PROMPT Z PLIKU\n", encoding="utf-8")
    set_cfg(system_prompt_file="prompt.txt")
    server.docs_ask(
        "how does organization activation work two-sided owner publishes schedule"
    )
    assert fake.chats[-1]["messages"][0]["content"] == "PROMPT Z PLIKU"


def test_missing_prompt_file_is_a_config_error(rag):
    _, _, set_cfg = rag
    server.docs_reindex()
    set_cfg(system_prompt_file="nie-ma.txt")
    out = server.docs_ask("how does organization activation work")
    assert out["error"]["category"] == "config" and "system_prompt_file" in out["error"]["message"]


# ------------------------------------------------------------------ boosts


def test_status_boost_orders_current_before_a_draft_twin(rag):
    _, root, set_cfg = rag
    _doc(root, "a-draft.md", "draft")
    _doc(root, "b-current.md", "current")
    set_cfg()
    server.docs_reindex()
    base = server.docs_semantic_search("unikalnaxyz pricing plans", limit=2)["hits"]
    assert {h["file"] for h in base} == {"docs/a-draft.md", "docs/b-current.md"}
    assert base[0]["file"] == "docs/a-draft.md"  # without boost the draft wins on raw score
    set_cfg(rank={"status_boost": {"current": 1.0, "draft": 0.5}})
    boosted = server.docs_semantic_search("unikalnaxyz pricing plans", limit=2)["hits"]
    assert boosted[0]["file"] == "docs/b-current.md"
    # the draft twin has the HIGHER raw score yet is ranked second: boost orders, it never rewrites `score`
    assert boosted[1]["file"] == "docs/a-draft.md" and boosted[1]["score"] == base[0]["score"]
    assert boosted[1]["score"] > boosted[0]["score"]
    assert boosted[1]["rank_score"] < boosted[1]["score"]


def test_unknown_status_multiplier_applies_to_documents_without_status(rag):
    _, root, set_cfg = rag
    _doc(root, "a-nostatus.md", None)
    _doc(root, "b-current.md", "current")
    cfg = json.loads((root / ".spec-mcp.json").read_text())
    cfg["docs"]["require_front_matter"] = True  # without it a missing status counts as current
    (root / ".spec-mcp.json").write_text(json.dumps(cfg))
    set_cfg(rank={"status_boost": {"unknown": 0.5}})
    server.docs_reindex()
    hits = server.docs_semantic_search("unikalnaxyz pricing plans", limit=2)["hits"]
    assert hits[0]["file"] == "docs/b-current.md"


def test_no_rank_score_field_without_boosts(rag):
    server.docs_reindex()
    hit = server.docs_semantic_search("organization activation", limit=1)["hits"][0]
    assert "rank_score" not in hit and "reserved" not in hit


def test_min_score_is_compared_with_the_raw_score_not_the_boosted_one(rag):
    fake, _, set_cfg = rag
    server.docs_reindex()
    set_cfg(min_score=0.99, rank={"kind_boost": {"doc": 50.0}})
    out = server.docs_ask(
        "how does organization activation work two-sided owner publishes schedule"
    )
    assert (
        out["error"]["category"] == "insufficient_evidence" and out["best_score"] < 0.99
    )
    assert fake.chats == []


# ------------------------------------------------------------------ reserved slots


ROUTE_ROWS = [
    {
        "id": "POST /x/publish",
        "kind": "rest",
        "note": "endpoint publish schedule",
        "roles": ["ADMIN"],
    },
    {"id": "GET /x/list", "kind": "rest", "note": "endpoint list", "roles": ["ALL"]},
    {
        "id": "publishWeek",
        "kind": "graphql",
        "note": "endpoint mutation",
        "roles": ["ADMIN"],
    },
]


def test_reserve_inserts_records_when_the_question_names_a_route(rag):
    _, root, set_cfg = rag
    _routes(root, ROUTE_ROWS)
    set_cfg(corpora=["docs", "records:routes"], reserve={"record": 1})
    server.docs_reindex()
    q = "api gateway domains hosting endpoint"
    out = server.docs_semantic_search(q, limit=1)
    assert out["hits"][0]["kind"] == "record" and out["hits"][0]["reserved"] is True
    set_cfg(corpora=["docs", "records:routes"])
    assert server.docs_semantic_search(q, limit=1)["hits"][0]["kind"] == "doc"


def test_reserve_is_idle_when_the_question_does_not_name_it(rag):
    _, root, set_cfg = rag
    _routes(root, ROUTE_ROWS)
    set_cfg(corpora=["docs", "records:routes"], reserve={"record": 1})
    server.docs_reindex()
    out = server.docs_semantic_search("api gateway domains hosting", limit=1)
    assert out["hits"][0]["kind"] == "doc" and "reserved" not in out["hits"][0]


def test_reserved_sources_are_flagged_in_ask(rag):
    _, root, set_cfg = rag
    _routes(root, ROUTE_ROWS)
    set_cfg(corpora=["docs", "records:routes"], reserve={"record": 1}, top_k=1, min_score=0.01)
    server.docs_reindex()
    out = server.docs_ask("api gateway domains hosting endpoint", top_k=1)
    assert any(s.get("reserved") and s["kind"] == "record" for s in out["sources"])


# ------------------------------------------------------------------ adaptive top_k and context


def test_list_questions_widen_top_k_and_plain_ones_do_not(rag):
    _, _, set_cfg = rag
    set_cfg(top_k=2, top_k_list=5)
    server.docs_reindex()
    plain = server.docs_ask("how does organization activation work", top_k=2)
    listq = server.docs_ask("które sekcje opisują organization activation", top_k=2)
    assert plain["top_k_used"] == 2 and plain["list_question"] is False
    assert listq["top_k_used"] == 5 and listq["list_question"] is True


def test_list_top_k_is_capped_at_14(rag):
    _, _, set_cfg = rag
    set_cfg(top_k_list=40)
    server.docs_reindex()
    assert (
        server.docs_ask("które sekcje opisują organization activation", top_k=2)[
            "top_k_used"
        ]
        == 14
    )


def test_an_explicit_larger_top_k_wins_over_the_list_default(rag):
    _, _, set_cfg = rag
    set_cfg(top_k_list=5)
    server.docs_reindex()
    assert (
        server.docs_ask("które sekcje opisują organization activation", top_k=9)[
            "top_k_used"
        ]
        == 9
    )


def _hit(kind="doc", n=1, text="x" * 500, reserved=False):
    return {"kind": kind, "text": text, "score": 0.5, "reserved": reserved, "n": n}


def test_fit_context_drops_the_weakest_and_flags_truncation():
    hits = [_hit(n=i) for i in range(6)]
    kept, truncated = ol._fit_context(hits, 1500, 1400)
    assert truncated and [h["n"] for h in kept] == [0, 1] and len(kept) < len(hits)
    kept, truncated = ol._fit_context(hits, 1500, 100000)
    assert not truncated and len(kept) == 6


def test_fit_context_keeps_structured_and_reserved_hits():
    hits = [
        _hit("structured", 0),
        _hit(n=1),
        _hit("record", 2, reserved=True),
        _hit(n=3),
        _hit(n=4),
    ]
    kept, truncated = ol._fit_context(hits, 1500, 1000)
    kinds = [(h["kind"], h["n"]) for h in kept]
    assert ("structured", 0) in kinds and ("record", 2) in kinds and truncated


def test_ask_reports_context_truncation(rag):
    fake, _, set_cfg = rag
    set_cfg(max_context_chars=200, top_k=6)
    server.docs_reindex()
    out = server.docs_ask(
        "how does organization activation work two-sided owner publishes schedule",
        top_k=6,
    )
    assert out["context_truncated"] is True and len(out["sources"]) < 6
    assert len(fake.chats) == 1


# ------------------------------------------------------------------ gap post-check inside ask


def test_ask_flags_a_contradicting_gap_line_but_keeps_confidence(rag):
    fake, _, _ = rag
    server.docs_reindex()
    fake.reply = (
        "Aktywacja organizacji jest dwustronna: właściciel publikuje grafik [1]. "
        "Brak w dokumentacji: aktywacja organizacji szczegóły."
    )
    out = server.docs_ask(
        "jak działa aktywacja organizacji i publikacja grafiku organization activation"
    )
    assert out["partial_gap"] is True and out["consistency_warning"] is True
    assert out["confidence"] in ("medium", "high")


def test_ask_plain_answer_has_no_gap_flags(rag):
    server.docs_reindex()
    out = server.docs_ask(
        "how does organization activation work two-sided owner publishes schedule"
    )
    assert out["partial_gap"] is False and out["consistency_warning"] is False
    assert out["route_hint"] is None and "suggested_tool" not in out


# ------------------------------------------------------------------ structured routing


def _pattern(**over):
    p = {
        "pattern": "zrobienia",
        "tool": "req_list",
        "arguments": {"status": "TODO"},
        "execute": True,
    }
    p.update(over)
    return p


def test_route_hint_is_only_a_hint_by_default(rag):
    fake, _, set_cfg = rag
    set_cfg(structured_patterns=[_pattern()])
    server.docs_reindex()
    out = server.docs_ask(
        "co jest do zrobienia w organization activation owner publishes schedule"
    )
    assert out["route_hint"] == "structured"
    assert out["suggested_tool"] == {
        "tool": "req_list",
        "arguments": {"status": "TODO"},
        "executed": False,
    }
    assert "structured_result" not in out
    assert "Wynik narzędzia" not in fake.chats[-1]["messages"][1]["content"]


def test_route_structured_executes_the_tool_and_feeds_the_generator(rag):
    fake, _, set_cfg = rag
    set_cfg(structured_patterns=[_pattern()], route_structured=True)
    server.docs_reindex()
    out = server.docs_ask(
        "co jest do zrobienia w organization activation owner publishes schedule"
    )
    sr = out["structured_result"]
    assert (
        sr["tool"] == "req_list"
        and sr["matched"] == 1
        and sr["items"][0]["id"] == "INV-R1"
    )
    assert out["suggested_tool"]["executed"] is True
    assert out["sources"][0]["kind"] == "structured"
    assert (
        "Wynik narzędzia req_list(status=TODO)"
        in fake.chats[-1]["messages"][1]["content"]
    )


def test_structured_result_counts_as_evidence_for_the_strict_gate(rag):
    fake, _, set_cfg = rag
    set_cfg(
        structured_patterns=[_pattern(pattern="zzzzz")],
        route_structured=True,
        min_score=0.95,
    )
    server.docs_reindex()
    out = server.docs_ask("zzzzz zzzzz")
    assert "error" not in out and out["sources"][0]["kind"] == "structured"
    assert len(fake.chats) == 1


def test_rec_aggregate_route_gives_counts_with_denominator(rag):
    fake, root, set_cfg = rag
    _routes(root, ROUTE_ROWS)
    set_cfg(
        corpora=["docs", "records:routes"],
        route_structured=True,
    )
    server.docs_reindex()
    out = server.docs_ask("Ile tras REST i operacji GraphQL ma backend Nest?")
    sr = out["structured_result"]
    assert (
        sr["tool"] == "rec_aggregate"
        and sr["groups"] == {"rest": 2, "graphql": 1}
        and sr["denominator"] == 3
    )
    prompt = fake.chats[-1]["messages"][1]["content"]
    assert "rest: 2" in prompt and "mianownik 3" in prompt


def test_not_executable_pattern_stays_a_hint_even_when_routing_is_on(rag):
    _, root, set_cfg = rag
    _routes(root, ROUTE_ROWS)
    set_cfg(corpora=["docs", "records:routes"], route_structured=True)
    server.docs_reindex()
    out = server.docs_ask("Jaką rolę ma trasa publish? endpoint publish schedule")
    assert (
        out["route_hint"] == "structured" and out["suggested_tool"]["executed"] is False
    )
    assert "structured_result" not in out


# ------------------------------------------------------------------ registry / compatibility


def test_tool_registry_is_unchanged_at_49():
    assert len(server.mcp._tool_manager._tools) == 49  # noqa: SLF001
