import json
from pathlib import Path

import httpx
import pytest
from quality.providers import AgnesClient, ProviderError, SiliconFlowClient
from quality.query_planning import rerank_hits, rewrite_query


def model_with_response(content):
    return AgnesClient(
        "test-key",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": content,
                                "reasoning_content": "must not be persisted",
                            }
                        }
                    ],
                    "usage": {"total_tokens": 20},
                },
            )
        ),
    )


def test_rewrite_preserves_identifiers_or_explicitly_falls_back():
    original = "查K170在2025-03-01批次适用的规则"
    good = rewrite_query(
        original,
        model_with_response(
            json.dumps(
                {"queries": ["K170 2025-03-01 批次规则", "认证资料要求"]}, ensure_ascii=False
            )
        ),
    )
    assert good["status"] == "rewritten"
    assert good["engine_mode"] == "contract_fixture"
    assert good["original_query"] == original
    bad = rewrite_query(original, model_with_response('{"queries":["一般规则"]}'))
    assert bad["status"] == "fallback_original"
    assert bad["queries"] == [original]
    drift = rewrite_query(
        original, model_with_response(json.dumps({"queries": ["K170S 2025-03-01 规则"]}))
    )
    assert drift["status"] == "fallback_original"
    malformed = rewrite_query(original, model_with_response("not json"))
    assert malformed["queries"] == [original]


def test_replay_live_rewrite_json_fence():
    path = Path(__file__).resolve().parents[1] / "tests/fixtures/rewrite-fence.json"
    captured = json.loads(path.read_text(encoding="utf-8"))
    result = rewrite_query(captured["query"],
                           model_with_response(captured["public_response_content"]))
    assert result["status"] == "rewritten"
    assert len(result["queries"]) == 3


@pytest.mark.parametrize("content", [
    '解释后输出：```json\n{"queries":["K170 2025-03-01"]}\n```',
    '```json\n{"queries":["K170S 2025-03-01"]}\n```',
])
def test_fenced_rewrite_still_rejects_prose_or_identifier_drift(content):
    result = rewrite_query("K170 2025-03-01认证", model_with_response(content))
    assert result["status"] == "fallback_original"


def test_agnes_does_not_persist_hidden_thought_and_missing_key_is_explicit():
    response = model_with_response("ok").complete([{"role": "user", "content": "hello"}])
    assert "reasoning_content" not in response["message"]
    with pytest.raises(ProviderError) as missing:
        AgnesClient("").complete([{"role": "user", "content": "hello"}])
    assert missing.value.code == "missing_agnes_key"
    with pytest.raises(ValueError):
        AgnesClient("test-key", model="agnes-2.5-pro")


@pytest.mark.parametrize("message", [None, "text", []])
def test_malformed_agnes_message_is_a_provider_error(message):
    model = AgnesClient(
        "test-key",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"choices": [{"message": message}]})
        ),
    )
    with pytest.raises(ProviderError) as error:
        model.complete([{"role": "user", "content": "hello"}])
    assert error.value.code == "invalid_agnes_response"


def test_malformed_tool_envelope_is_rejected_before_execution():
    model = AgnesClient(
        "test-key",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-a",
                                        "type": "function",
                                        "function": {"name": "read_case"},
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        ),
    )
    with pytest.raises(ProviderError) as error:
        model.complete([{"role": "user", "content": "hello"}])
    assert error.value.code == "invalid_agnes_response"


def test_rerank_retains_evidence_identity_and_empty_does_not_call_provider():
    client = SiliconFlowClient(
        "test-key",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "results": [
                        {"index": 1, "relevance_score": 0.95},
                        {"index": 0, "relevance_score": 0.2},
                    ]
                },
            )
        ),
    )
    hits = [
        {"id": "a", "text": "证书", "score": 9, "location": {"page": 1}},
        {"id": "b", "text": "检测报告", "score": 2, "location": {"page": 2}},
    ]
    result, metadata = rerank_hits("报告", hits, client, 2)
    assert [hit["id"] for hit in result] == ["b", "a"]
    assert result[0]["location"] == {"page": 2}
    assert result[0]["retrieval_score"] == 2
    empty, status = rerank_hits("报告", [], SiliconFlowClient(""), 2)
    assert empty == []
    assert status["status"] == "skipped_empty"
