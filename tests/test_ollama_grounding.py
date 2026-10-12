"""0.7.1: question-paraphrase / preamble handling in `uncited_sentences`, `[S]` citations of the
structured result, grounded/confidence for routed questions, deterministic refusal normalization.
No network, no real Ollama."""

import json

import pytest

from spec_mcp import ollama as ol
from spec_mcp import server
from spec_mcp.config import load_config
from spec_mcp.errors import SpecError

from test_ollama import rag  # noqa: F401  (fixture reused)

Q = "ile tras REST i operacji GraphQL ma backend Nest"
ASK = "how does organization activation work two-sided owner publishes schedule"


# ------------------------------------------------------------------ uncited_sentences


def test_a_paraphrase_of_the_question_is_not_an_uncited_claim():
    ans = "1. Liczba tras REST i operacji GraphQL w backendzie Nest\n2. Backend Nest ma 201 tras REST [S]"
    assert ol.uncited_sentences(ans, Q) == []


def test_a_sentence_with_a_new_number_is_not_a_paraphrase():
    ans = "Liczba tras REST i operacji GraphQL w backendzie Nest wynosi 201"
    assert ol.uncited_sentences(ans, Q) == [ans]


def test_a_sentence_with_a_new_identifier_is_not_a_paraphrase():
    s = "Liczba tras REST i operacji GraphQL w backendzie Nest jest w routes.json"
    assert ol.is_question_paraphrase(s, Q) is False


def test_without_a_question_nothing_is_treated_as_a_paraphrase():
    s = "Liczba tras REST i operacji GraphQL w backendzie Nest nie wynika z źródeł"
    assert ol.uncited_sentences(s) == [s[:160]]


@pytest.mark.parametrize(
    "lead",
    [
        "Na podstawie dostarczonych źródeł poniżej wynik:",
        "Na podstawie źródeł wiadomo następujące",
        "**Wynik:**",
    ],
)
def test_headings_and_lead_ins_are_not_claims(lead):
    assert ol.is_preamble(lead) is True
    assert ol.uncited_sentences(lead + " ", None) == []


def test_a_lead_in_with_a_number_is_a_claim():
    assert ol.is_preamble("Na podstawie źródeł jest 201 tras REST w Nest") is False


# ------------------------------------------------------------------ [S] citations


def test_s_is_a_valid_reference_but_not_a_numbered_citation():
    ans = "Backend Nest ma 201 tras REST [S] oraz opis [1, S]."
    assert ol.parse_citations(ans) == [[1]]
    assert ol.cites_structured(ans) is True
    assert ol.cites_structured("Backend ma trasy [1].") is False


def test_the_structured_source_is_labelled_s_in_prompt_and_sources():
    hits = [
        {"kind": "structured", "file": "tool:rec_aggregate", "line": None, "line_end": None,
         "path": "wynik", "heading": "h", "status": None, "text": "201", "corpus": "structured", "score": 1.0},
        {"kind": "doc", "file": "docs/a.md", "line": 1, "line_end": 3, "path": "A", "heading": "A",
         "status": "current", "text": "x", "corpus": "docs", "score": 0.9},
    ]
    src = ol._number_sources(hits)
    assert [s["n"] for s in src] == ["S", 1]
    prompt = ol._user_prompt("q", hits, 1500)
    assert "[S] (structured" in prompt and "[1] (doc" in prompt


def _structured(rag):
    fake, _, set_cfg = rag
    set_cfg(
        structured_patterns=[
            {"pattern": "zrobienia", "tool": "req_list", "arguments": {"status": "TODO"}, "execute": True}
        ],
        route_structured=True,
    )
    server.docs_reindex()
    return fake


def test_a_routed_answer_citing_s_is_grounded_with_high_confidence(rag):
    fake = _structured(rag)
    fake.reply = "Do zrobienia jest 1 wymaganie: INV-R1 [S]."
    out = server.docs_ask("co jest do zrobienia w organization activation owner publishes schedule")
    assert out["cited_structured"] is True and out["citations_valid"] is True
    assert out["grounded"] is True and out["confidence"] == "high"


def test_a_routed_answer_restating_the_question_stays_grounded(rag):
    fake = _structured(rag)
    fake.reply = (
        "Co jest do zrobienia w organization activation owner publishes schedule:\n"
        "Do zrobienia jest 1 wymaganie: INV-R1 [S]."
    )
    out = server.docs_ask("co jest do zrobienia w organization activation owner publishes schedule")
    assert out["uncited_sentences"] == [] and out["grounded"] is True


def test_numbers_that_come_from_the_structured_result_count_as_support(rag):
    fake = _structured(rag)
    fake.reply = "Pasuje 1 z 1 wymagań, to INV-R1."
    out = server.docs_ask("co jest do zrobienia w organization activation owner publishes schedule")
    assert out["cited"] == [] and out["cited_structured"] is False
    assert out["grounded"] is True and out["confidence"] == "high"


def test_a_number_the_structured_result_does_not_contain_is_not_support(rag):
    fake = _structured(rag)
    fake.reply = "Pasuje 999 wymagań, to bardzo dużo wymagań w tej domenie."
    out = server.docs_ask("co jest do zrobienia w organization activation owner publishes schedule")
    assert out["grounded"] is False


def test_s_without_a_structured_result_is_an_invalid_citation(rag):
    fake, _, _ = rag
    server.docs_reindex()
    fake.reply = "Aktywacja jest dwustronna [S]."
    out = server.docs_ask(ASK)
    assert out["citations_valid"] is False and out["grounded"] is False


def test_default_prompt_mentions_s_and_no_restating():
    assert "[S]" in ol.SYSTEM_PROMPT and "Nie powtarzaj" in ol.SYSTEM_PROMPT


# ------------------------------------------------------------------ refusal normalization


def test_a_looped_refusal_collapses_to_one_line():
    ans = (
        "Brak w dokumentacji: stawki godzinowej w Poznaniu.\n"
        "Brak w dokumentacji: danych o lokalizacji.\n"
        "Brak w dokumentacji: informacji o stawkach."
    )
    out, info = ol.normalize_refusals(ans)
    assert out == "Brak w dokumentacji: stawki godzinowej w Poznaniu."
    assert info == {"normalized": True, "refusal": True, "partial_gap": False}


def test_bullets_and_markdown_are_stripped_from_the_kept_line():
    ans = "- **Brak w dokumentacji**: stawki.\n- Brak w dokumentacji: lokalizacji."
    out, _ = ol.normalize_refusals(ans)
    assert out == "Brak w dokumentacji: stawki."


def test_cited_claims_are_kept_and_gaps_collapse_to_one_trailing_line():
    ans = (
        "Aktywacja jest dwustronna [1].\n"
        "Brak w dokumentacji: szczegółów A.\n"
        "Właściciel publikuje grafik [2].\n"
        "Brak w dokumentacji: szczegółów B."
    )
    out, info = ol.normalize_refusals(ans)
    lines = out.split("\n")
    assert lines[:2] == ["Aktywacja jest dwustronna [1].", "Właściciel publikuje grafik [2]."]
    assert lines[-1] == "Brak w dokumentacji: szczegółów A." and out.count("Brak w dokumentacji") == 1
    assert info["partial_gap"] is True and info["refusal"] is False


def test_normalization_is_idempotent():
    ans = "Brak w dokumentacji: a.\nBrak w dokumentacji: b."
    once, _ = ol.normalize_refusals(ans)
    twice, info = ol.normalize_refusals(once)
    assert twice == once and info == {"normalized": False}


def test_a_single_gap_line_is_left_alone():
    ans = "Aktywacja jest dwustronna [1].\nBrak w dokumentacji: szczegółów."
    assert ol.normalize_refusals(ans) == (ans, {"normalized": False})


def test_ask_normalizes_a_looped_refusal_and_reports_it(rag):
    fake, _, _ = rag
    server.docs_reindex()
    fake.reply = "Brak w dokumentacji: A.\nBrak w dokumentacji: B.\nBrak w dokumentacji: C."
    out = server.docs_ask(ASK)
    assert out["answer"] == "Brak w dokumentacji: A."
    assert out["answer_normalized"] is True and out["refusal"] is True
    assert out["answer_raw"].count("Brak w dokumentacji") == 3
    assert out["full_refusal"] is True and out["grounded"] is True


def test_ask_keeps_a_cited_claim_and_marks_partial_gap(rag):
    fake, _, _ = rag
    server.docs_reindex()
    fake.reply = "Aktywacja jest dwustronna [1].\nBrak w dokumentacji: A.\nBrak w dokumentacji: B."
    out = server.docs_ask(ASK)
    assert out["answer_normalized"] is True and out["partial_gap"] is True
    assert out["refusal"] is False and out["answer"].count("Brak w dokumentacji") == 1


def test_normalization_can_be_switched_off(rag):
    fake, _, set_cfg = rag
    set_cfg(normalize_answers=False)
    server.docs_reindex()
    fake.reply = "Brak w dokumentacji: A.\nBrak w dokumentacji: B."
    out = server.docs_ask(ASK)
    assert out["answer_normalized"] is False and "answer_raw" not in out
    assert out["answer"].count("Brak w dokumentacji") == 2


def test_a_plain_answer_has_no_normalization_flags(rag):
    server.docs_reindex()
    out = server.docs_ask(ASK)
    assert out["answer_normalized"] is False and out["refusal"] is False


def test_normalize_answers_must_be_boolean(project):
    (project / ".spec-mcp.json").write_text(
        json.dumps({"ollama": {"normalize_answers": "yes"}})
    )
    with pytest.raises(SpecError) as e:
        load_config(project / ".spec-mcp.json", root=project)
    assert e.value.category == "config"
