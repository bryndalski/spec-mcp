from spec_mcp.bm25 import BM25, tokenize


def test_tokenize_lowercases_and_splits_identifiers():
    toks = tokenize("Organization activation: funnelStage ACT-R1 api.dev")
    assert "organization" in toks
    assert "activation" in toks
    assert "act-r1" in toks or "act" in toks
    assert "funnelstage" in toks


def test_bm25_ranks_relevant_doc_first():
    docs = [
        "The application is served behind a gateway.",
        "Organization activation is two-sided: owner publishes a schedule.",
        "Schedules are published weekly by the owner.",
    ]
    idx = BM25([tokenize(d) for d in docs])
    hits = idx.search(tokenize("organization activation"), limit=2)
    assert hits[0][0] == 1
    assert hits[0][1] > 0
    assert len(hits) <= 2


def test_bm25_empty_query_returns_nothing():
    idx = BM25([tokenize("a b c")])
    assert idx.search([], limit=5) == []
