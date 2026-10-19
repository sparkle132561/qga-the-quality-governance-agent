import json

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from quality.agent_core import Tool, ToolExecutor
from quality.domain import ToolResult
from quality.providers import AgnesClient
from quality.retrieval_agent import EvidenceSearch, build_retrieval_agent
from test_retrieval_agent import ScriptedModel, final, initial, tool_call


@pytest.mark.parametrize(("max_rounds", "max_tools"), [(2, 8), (4, 1)])
def test_last_round_reserves_final_response_with_current_evidence(max_rounds, max_tools):
    executions = []

    def search(arguments, actor):
        executions.append(arguments.query)
        return ToolResult(status="ok", evidence_refs=["e1"],
                          data={"hits": [{"id": "e1", "text": "证书要求"}]})

    class ToolSeekingModel:
        def complete(self, messages, tools):
            reply = tool_call() if tools else final("needs_info", ["e1"])
            if not tools:
                assert "最后" in messages[0]["content"]
                assert "e1" in json.dumps(messages)
            return {"message": reply, "usage": {"total_tokens": 15},
                    "elapsed_ms": 0, "engine_mode": "fake"}

    executor = ToolExecutor([
        Tool("search_evidence", "检索", EvidenceSearch, frozenset({"rules"}), search)])
    state = initial()
    state.update(max_rounds=max_rounds, max_tools=max_tools)
    result = build_retrieval_agent(ToolSeekingModel(), executor, InMemorySaver()).invoke(
        state, {"configurable": {"thread_id": "final-round-reserved"}})
    assert result["status"] == "completed"
    assert result["rounds"] == 2 and executions == ["证书"]
    assert result["final"]["evidence_ids"] == ["e1"]


def test_ignored_final_round_tool_call_is_blocked_and_usage_preserved():
    executions = []

    def search(arguments, actor):
        executions.append(arguments.query)
        return ToolResult(status="not_found")

    class IgnoringModel(ScriptedModel):
        def complete(self, messages, tools):
            response = super().complete(messages, tools)
            response.update(usage={"total_tokens": 31}, request_meta={"attempts": 1})
            return response

    executor = ToolExecutor([
        Tool("search_evidence", "检索", EvidenceSearch, frozenset({"rules"}), search)])
    state = initial()
    state["max_rounds"] = 1
    result = build_retrieval_agent(IgnoringModel([tool_call()]), executor,
                                   InMemorySaver()).invoke(
        state, {"configurable": {"thread_id": "final-round-ignored"}})
    assert executions == [] and result["tool_count"] == 0
    assert result["error_code"] == "budget_exhausted"
    assert result["trace"][0]["usage"]["total_tokens"] == 31
    assert result["trace"][-1]["reason"] == "final_round_tool_call"
    assert json.loads(result["messages"][-1]["content"])["status"] == "failed"


@pytest.mark.parametrize("ids, expected", [([], "completed"), (["invented"], "needs_info")])
def test_final_function_channel_uses_schema_and_still_validates_citations(ids, expected):
    def respond(request):
        payload = json.loads(request.content)
        assert payload["tool_choice"] == {"type": "function",
                                          "function": {"name": "return_final_advice"}}
        assert [tool["function"]["name"] for tool in payload["tools"]] == ["return_final_advice"]
        assert payload["tools"][0]["function"]["parameters"]["additionalProperties"] is False
        reply = tool_call("return_final_advice", final("needs_info", ids)["content"])
        return httpx.Response(200, json={"choices": [{"message": reply}],
                                        "usage": {"total_tokens": 17}})

    state = initial()
    state["max_rounds"] = 1
    model = AgnesClient("fixture-key", transport=httpx.MockTransport(respond))
    result = build_retrieval_agent(model, ToolExecutor([]), InMemorySaver()).invoke(
        state, {"configurable": {"thread_id": "final-function-" + expected}})
    assert result["status"] == expected and result["tool_count"] == 0
    assert result["trace"][0]["usage"]["total_tokens"] == 17
    assert result["trace"][0]["final_submission"]["channel"] == "function_arguments"


@pytest.mark.parametrize("reason", ["analysis_cancelled", "snapshot_changed"])
def test_stop_between_final_response_and_blocked_tool_keeps_stop_reason(reason):
    checks = []

    def control():
        checks.append(True)
        return reason if len(checks) >= 3 else None

    state = initial()
    state["max_rounds"] = 1
    result = build_retrieval_agent(ScriptedModel([tool_call()]), ToolExecutor([]),
                                   InMemorySaver(), control=control).invoke(
        state, {"configurable": {"thread_id": "final-round-stop-" + reason}})
    assert result["status"] == "cancelled" and result["error_code"] == reason
    assert result["tool_count"] == 0
