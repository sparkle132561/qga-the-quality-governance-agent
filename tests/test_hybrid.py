import pytest
from conftest import headers
from quality.retrieval import rrf_fuse


def hit(identifier, score):
    return {"id": identifier, "text": identifier, "score": score, "method": "test"}


def test_rrf_uses_ranks_not_incompatible_score_scales():
    lexical = [hit("a", 1000000), hit("b", 1)]
    vector = [hit("b", 0.9), hit("a", 0.1)]
    first = rrf_fuse({"bm25": lexical, "vector": vector}, 2)
    lexical[0]["score"] = -100
    second = rrf_fuse({"bm25": lexical, "vector": vector}, 2)
    assert [row["id"] for row in first] == [row["id"] for row in second]
    assert first[0]["score"] == pytest.approx(1 / 61 + 1 / 62)
    assert first[0]["ranks"] == {"bm25": 1, "vector": 2}


def test_rrf_deduplicates_within_and_across_rankings_and_respects_limit():
    output = rrf_fuse(
        {"bm25": [hit("a", 1), hit("a", 1)], "vector": [hit("a", 0.2), hit("b", 0.1)]}, 1
    )
    assert len(output) == 1
    assert output[0]["score"] == pytest.approx(2 / 61)
    assert rrf_fuse({"bm25": [], "vector": []}, 5) == []
    with pytest.raises(ValueError):
        rrf_fuse({}, 1, 0)


def test_hybrid_never_claims_live_results_without_key(client):
    response = client.post(
        "/api/search",
        headers=headers(client),
        json={"query": "证书", "as_of": "2025-07-01", "method": "hybrid"},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "missing_key"
