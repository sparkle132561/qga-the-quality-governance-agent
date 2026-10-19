import json

import httpx
import pytest
from quality.providers import ProviderError, SiliconFlowClient


def vector(position=0):
    values = [0.0] * 1024
    values[position] = 1.0
    return values


def test_embedding_orders_results_and_keeps_usage_without_paid_fallback():
    def respond(request):
        body = json.loads(request.content)
        assert body["model"] == "BAAI/bge-m3"
        assert body["input"] == ["证书", "报告"]
        assert request.url.path == "/v1/embeddings"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": vector(1)},
                    {"index": 0, "embedding": vector(0)},
                ],
                "usage": {"total_tokens": 5},
            },
        )

    client = SiliconFlowClient("test-key", transport=httpx.MockTransport(respond))
    result = client.embed(["证书", "报告"])
    assert result.vectors == [vector(0), vector(1)]
    assert result.usage["total_tokens"] == 5


def test_missing_key_bad_vectors_and_rate_limit_are_explicit(monkeypatch):
    from quality import providers

    monkeypatch.setattr(providers, "RATE_LIMIT_BACKOFF_SECONDS", 0.0)
    with pytest.raises(ProviderError, match="没有执行真实调用"):
        SiliconFlowClient("").embed(["证书"])
    invalid = SiliconFlowClient(
        "test-key",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.0]}]})
        ),
    )
    with pytest.raises(ProviderError, match="不符合契约"):
        invalid.embed(["证书"])
    limited = SiliconFlowClient(
        "test-key",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, json={"message": "not logged"})
        ),
    )
    with pytest.raises(ProviderError) as failure:
        limited.embed(["证书"])
    assert failure.value.code == "http_429"


def test_rate_limit_without_retry_after_backs_off_and_retries_once(monkeypatch):
    from quality import providers

    monkeypatch.setattr(providers, "RATE_LIMIT_BACKOFF_SECONDS", 0.05)
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": {"code": "", "type": "rate"}})
        return httpx.Response(
            200,
            json={
                "data": [{"index": 0, "embedding": vector(0)}],
                "usage": {"total_tokens": 3},
            },
        )

    client = SiliconFlowClient("test-key", transport=httpx.MockTransport(respond))
    result = client.embed(["证书"])
    assert result.vectors == [vector(0)]
    assert len(calls) == 2


def test_reranker_returns_candidate_indices_and_rejects_out_of_range():
    def respond(request):
        assert request.url.path == "/v1/rerank"
        assert json.loads(request.content)["model"] == "BAAI/bge-reranker-v2-m3"
        return httpx.Response(200, json={"results": [{"index": 1, "relevance_score": 0.9}]})
    valid = SiliconFlowClient(
        "test-key",
        transport=httpx.MockTransport(respond),
    )
    assert valid.rerank("报告", ["证书", "报告"], 1)["results"][0]["index"] == 1
    invalid = SiliconFlowClient(
        "test-key",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"results": [{"index": 9, "relevance_score": 0.9}]}
            )
        ),
    )
    with pytest.raises(ProviderError, match="不符合契约"):
        invalid.rerank("报告", ["证书", "报告"], 1)
