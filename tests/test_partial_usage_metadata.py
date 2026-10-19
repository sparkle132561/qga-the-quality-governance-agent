from conftest import headers
from quality.providers import ProviderError


def test_downstream_failure_preserves_completed_rewrite_call_id(client, monkeypatch):
    rewrite_meta = {"local_call_id": "completed-rewrite", "attempts": 1}
    plan = {"original_query": "证书", "queries": ["证书"], "status": "rewritten",
            "elapsed_ms": 1, "usage": {"total_tokens": 10}, "request_meta": rewrite_meta}
    monkeypatch.setattr("quality.retrieval.rewrite_query", lambda query, model: plan)

    def fail(self, texts):
        raise ProviderError("request_deadline", "模拟后续Embedding预算耗尽", False,
                            {"local_call_id": "embedding-not-sent", "attempts": 0})
    monkeypatch.setattr("quality.retrieval.SiliconFlowClient.embed", fail)
    response = client.post("/api/search", headers=headers(client), json={
        "query": "证书", "as_of": "2025-07-01", "method": "hybrid", "rewrite": True})
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["failed_stage"] == "embedding"
    assert detail["request_meta"]["local_call_id"] == "embedding-not-sent"
    assert detail["partial_usage"]["rewrite"] == {"total_tokens": 10}
    assert detail["partial_usage"]["rewrite_request_meta"] == rewrite_meta
