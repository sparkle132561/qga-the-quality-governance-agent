import importlib.util
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from quality.config import Settings
from quality.providers import AgnesClient
from quality.task_routing import choose_route, execute_route, graph_start_ids, visible_graph_starts


def snapshot():
    return {"case": {"id": "current"}, "evidence_refs": [], "matching_records": [],
            "unresolved": {"conflicts": [], "missing_fields": []}}


def test_malformed_planner_preserves_attempt_and_consumes_round(monkeypatch):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": []})
    fixture = AgnesClient("fixture-only", transport=httpx.MockTransport(respond))
    monkeypatch.setattr("quality.task_routing.AgnesClient", lambda *args: fixture)
    plan = choose_route("核对材料", snapshot(), Settings(_env_file=None, agnes_api_key=""))
    assert len(calls) == 1 and plan["planner_request_meta"]["attempts"] == 1
    assert plan["planner_error"] == "invalid_agnes_response"
    assert 4 - int(bool(plan["planner_request_meta"]["attempts"])) == 3


def test_document_starts_use_same_scoped_filtered_graph_reader():
    calls = []
    def graph_reader(**kwargs):
        calls.append(kwargs)
        return {"nodes": [{"id": "doc:verified:p", "origin": "reviewed_document"},
                          {"id": "product:unrelated", "origin": "business_record"}]}
    app = SimpleNamespace(state=SimpleNamespace(graph_reader=graph_reader))
    who = object()
    starts = visible_graph_starts(app, "current", who, date(2025, 7, 1), snapshot(),
                                  "跨文档关联", "adaptive")
    assert starts == ["doc:verified:p"]
    assert calls == [{"case_id": "current", "who": who, "as_of": date(2025, 7, 1)}]
    assert graph_start_ids(snapshot()) == []
    calls.clear()
    visible_graph_starts(app, "current", who, date(2025, 7, 1), snapshot(), "状态", "adaptive")
    assert calls == []


@pytest.mark.parametrize("policy", ["adaptive", "all"])
@pytest.mark.parametrize("question", ["沿供应商和批次说明能找到哪些订单", "订单关联"])
def test_explicit_order_target_survives_planner_mistake(monkeypatch, policy, question):
    def complete(self, messages, tools=None):
        return {"message": {"content": '{"route":"graph","graph_start_id":"product:owned",'
                '"graph_target_kind":"supplier","reason":"核对来源"}'},
                "engine_mode": "fake", "usage": {}, "elapsed_ms": 0}
    monkeypatch.setattr(AgnesClient, "complete", complete)
    result = choose_route(question, snapshot(),
                          Settings(_env_file=None, agnes_api_key=""), policy,
                          graph_starts=["product:owned"])
    assert result["graph_target_kind"] == "order"


@pytest.mark.parametrize("gap", ["conflicts", "missing_fields"])
@pytest.mark.parametrize("has_graph", [True, False])
@pytest.mark.parametrize("planned_route", ["case", "evidence"])
def test_nonempty_incomplete_evidence_escalates_or_stops(
    monkeypatch, gap, has_graph, planned_route
):
    import json
    data = snapshot()
    data["unresolved"][gap] = ["model"]
    def complete(self, messages, tools=None):
        return {"message": {"content": json.dumps({"route": planned_route, "reason": "取证"})},
                "engine_mode": "fake", "usage": {}, "elapsed_ms": 0}
    monkeypatch.setattr(AgnesClient, "complete", complete)
    plan = choose_route("核对证据是否完整", data, Settings(_env_file=None, agnes_api_key=""),
                        graph_starts=["doc:verified:p"] if has_graph else [])
    reads = []
    def paths(request, **kwargs):
        reads.append(request)
        return {"paths": [], "evidence": [], "timing_ms": {"total": 0}}
    app = SimpleNamespace(state=SimpleNamespace(
        search_reader=lambda *args, **kwargs: {"hits": [{"id": "partial-evidence"}],
                                              "timing_ms": {"total": 0}},
        path_reader=paths))
    result = execute_route(app, "current", None, "材料", date(2025, 7, 1), 5, plan)
    assert len(reads) == int(has_graph)
    assert result["route"]["route"] == ("graph" if has_graph else "evidence")
    assert result["evidence_assessment"]["status"] in {"needs_info", "needs_human_review"}
    if not has_graph:
        assert "no_visible_graph_start_needs_info" in result["route"]["clarification"]


def test_probe_requires_expected_order_and_correct_path():
    path = Path(__file__).resolve().parents[1] / "scripts/verify-routing-live.py"
    spec = importlib.util.spec_from_file_location("routing_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    valid = {"nodes": [{"id": "product:sku-hit", "kind": "product"},
                       {"id": "batch:owned", "kind": "batch"},
                       {"id": "order:demo-order-hit", "kind": "order"}],
             "edges": [{"predicate": "has_batch"}, {"predicate": "ordered_from"}], "hops": 2}
    assert module.assess_order_paths([valid])["passed"]
    assert not module.assess_order_paths([])["passed"]
    wrong = {**valid, "nodes": [*valid["nodes"][:-1],
                               {"id": "order:wrong", "kind": "order"}]}
    assert not module.assess_order_paths([wrong])["passed"]
    assert not module.assess_order_paths([{**valid, "edges": []}])["passed"]
    assert not module.assess_order_paths([valid, wrong])["passed"]
