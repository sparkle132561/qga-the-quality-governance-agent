import json

import pytest
from conftest import headers
from langgraph.checkpoint.memory import InMemorySaver
from quality.agent_core import build_context, fake_tool_roundtrip
from quality.domain import Actor, Role
from quality.graph import build_graph


def test_tool_roundtrip_rechecks_ownership_and_pairs_result(client):
    case = client.post(
        "/api/cases",
        headers=headers(client),
        json={"title": "工具读取", "workflow": "qualification"},
    ).json()
    tools = client.app.state.tools
    owner = Actor(user_id="merchant-a", role=Role.MERCHANT, merchant_id="merchant-a")
    messages = fake_tool_roundtrip(tools, owner, case["id"])
    assert messages[-1]["tool_call_id"] == messages[-2]["tool_calls"][0]["id"]
    assert json.loads(messages[-1]["content"])["status"] == "ok"
    other = Actor(user_id="merchant-b", role=Role.MERCHANT, merchant_id="merchant-b")
    assert tools.execute("read_case", {"case_id": case["id"]}, other, "rules").status == "not_found"
    assert tools.execute("read_case", {}, owner, "rules").status == "invalid"
    assert (
        tools.execute("read_case", {"case_id": case["id"]}, owner, "unregistered").status
        == "denied"
    )
    assert tools.execute("run_shell", {}, owner, "rules").status == "denied"


def test_context_keeps_facts_and_budget_drops_whole_evidence():
    messages = build_context("核对型号", {"model": "K170"}, [{"text": "x" * 1000}], 100)
    payload = json.loads(messages[1]["content"])
    assert payload["facts"]["model"] == "K170"
    assert payload["evidence"] == []
    assert sum(len(message["content"]) for message in messages) <= 100
    with pytest.raises(ValueError, match="超过预算"):
        build_context("x" * 200, {}, [], 100)
    many = build_context("", {}, [{}] * 20, 100)
    assert sum(len(message["content"]) for message in many) <= 100


def test_parallel_results_and_checkpoint_thread_isolation():
    graph = build_graph(InMemorySaver())
    config = {"configurable": {"thread_id": "case-a-run-1"}}
    result = graph.invoke({"case_id": "case-a", "revision": 1}, config)
    assert result["summary"]["engine_mode"] == "stub"
    branches = list(result["summary"]["branches"].values())
    assert max(item["started"] for item in branches) < min(item["finished"] for item in branches)
    restored = graph.get_state(config)
    assert restored.values["case_id"] == "case-a"
    assert graph.get_state({"configurable": {"thread_id": "case-b-run-1"}}).values == {}
