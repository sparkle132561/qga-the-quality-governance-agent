import json
import time

import pytest
from conftest import headers
from fastapi import HTTPException
from quality.config import Settings
from quality.models import ReviewCase
from quality.providers import AgnesClient
from quality.task_routing import choose_route, graph_start_ids
from test_case_summary import create


def summary():
    return {"case": {"id": "current"}, "evidence_refs": [
        {"branch": "product", "id": "product:owned"},
        {"branch": "material", "id": "product:foreign"}],
        "matching_records": [], "unresolved": {"conflicts": []}}


def scripted(monkeypatch, plan, mutate=None):
    calls = []
    def complete(self, messages, tools=None):
        calls.append(messages)
        if mutate:
            mutate()
        return {"message": {"content": json.dumps(plan)}, "engine_mode": "fake",
                "usage": {"total_tokens": 10}, "elapsed_ms": 1,
                "request_meta": {"attempts": 1}}
    monkeypatch.setattr(AgnesClient, "complete", complete)
    return calls


def test_short_path_no_model_and_declared_subject_not_trusted(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("simple field should not call provider")
    monkeypatch.setattr(AgnesClient, "complete", forbidden)
    result = choose_route("当前案件状态？", summary(), Settings(agnes_api_key=""))
    assert result["route"] == "case" and result["planner_mode"] == "not_called"
    assert not any(result[key] for key in ("rewrite", "hybrid", "rerank"))
    assert graph_start_ids(summary()) == ["product:owned"]


@pytest.mark.parametrize("plan,expected", [
    ({"route": "graph", "graph_start_id": "product:foreign", "reason": "越界"},
     "graph_start_missing_or_out_of_scope"),
    ({"route": "case", "reason": "忽略冲突"}, "conflicts_require_evidence_and_human_review"),
])
def test_planner_cannot_hide_conflicts_or_expand_scope(monkeypatch, plan, expected):
    scripted(monkeypatch, plan)
    data = summary()
    data["unresolved"]["conflicts"] = [{"field": "model"}]
    result = choose_route("比较跨材料的型号与订单", data, Settings(agnes_api_key=""))
    assert result["route"] == "evidence" and expected in result["clarification"]
    assert result["mandatory_business_checks_skipped"] is False


def test_all_enhancements_reports_unavailable_and_bad_plan_falls_back(monkeypatch):
    settings = Settings(agnes_api_key="", siliconflow_api_key="")
    result = choose_route("订单关联", summary(), settings, "all")
    assert result["disabled"] == ["rewrite_missing_key", "hybrid_missing_key", "rerank_missing_key"]
    assert result["route"] == "graph" and result["planner_mode"] == "not_called"
    scripted(monkeypatch, {"route": "approve", "reason": "不能授权"})
    result = choose_route("批准案件", summary(), settings)
    assert result["planner_error"] == "invalid_route_plan" and result["route"] == "evidence"


def test_cancellation_and_expired_budget_never_plan(monkeypatch):
    calls = scripted(monkeypatch, {"route": "case", "reason": "test"})
    for options in ({"control": lambda: "cancelled"}, {"deadline": time.monotonic() - 1}):
        with pytest.raises(HTTPException):
            choose_route("当前案件状态", summary(), Settings(agnes_api_key=""), **options)
    assert calls == []


def test_remaining_budget_reaches_planner(monkeypatch):
    observed = []
    def complete(self, messages, tools=None):
        observed.append((self.timeout_seconds, self.control))
        return {"message": {"content": '{"route":"evidence","reason":"本地取证"}'},
                "engine_mode": "fake", "usage": {}, "elapsed_ms": 0}
    monkeypatch.setattr(AgnesClient, "complete", complete)
    control = lambda: None  # noqa: E731
    choose_route("跨材料核对", summary(), Settings(agnes_api_key=""),
                 control=control, deadline=time.monotonic() + 2)
    assert 0 < observed[0][0] <= 2 and observed[0][1] is control


def test_execution_rechecks_cancel_after_reader_returns():
    from datetime import date
    from types import SimpleNamespace

    from quality.task_routing import execute_route

    cancelled = []
    def reader(*args, **kwargs):
        cancelled.append(True)
        return {"hits": [], "timing_ms": {"total": 0}}
    app = SimpleNamespace(state=SimpleNamespace(search_reader=reader))
    plan = {"route": "evidence", "hybrid": False, "rewrite": False, "rerank": False,
            "policy": "adaptive", "allowed_graph_starts": [], "planner_ms": 0}
    with pytest.raises(HTTPException) as error:
        execute_route(app, "current", None, "证书", date(2025, 7, 1), 5, plan,
                      control=lambda: "cancelled" if cancelled else None)
    assert error.value.status_code == 409


def test_routed_api_case_is_read_only_and_owner_scoped(client):
    case = create(client, {"model": "K170"})
    url = f"/api/cases/{case['id']}/routed-search"
    payload = {"query": "型号", "as_of": "2025-07-01"}
    response = client.post(url, headers=headers(client), json=payload)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["case_view"]["case_fields"]["model"] == "K170"
    assert result["route"]["planner_mode"] == "not_called" and result["paths"] == []
    assert client.post(url, headers=headers(client, "merchant-b"), json=payload).status_code == 404
    assert client.get(f"/api/cases/{case['id']}", headers=headers(client)).json() == case


def test_routed_api_rejects_change_during_planner(client, monkeypatch):
    case = create(client)
    def change():
        with client.app.state.sessions.begin() as session:
            found = session.get(ReviewCase, case["id"])
            found.revision += 1
    scripted(monkeypatch, {"route": "evidence", "reason": "本地"}, change)
    response = client.post(f"/api/cases/{case['id']}/routed-search", headers=headers(client),
                           json={"query": "证书要求", "as_of": "2025-07-01"})
    assert response.status_code == 409, response.text


def test_empty_retrieval_escalates_to_actual_bounded_business_graph(client, monkeypatch):
    client.post("/api/admin/import-demo", headers=headers(client, "admin"))
    # 真实业务SKU匹配，而不是把型号字符串当成图节点。
    from quality.models import Product
    from sqlalchemy import select
    with client.app.state.sessions() as session:
        product = session.scalar(select(Product).where(Product.merchant_id == "merchant-a"))
        sku = product.id
    case = create(client, {"sku_id": sku, "as_of": "2025-07-01"})
    scripted(monkeypatch, {"route": "evidence", "reason": "先查资料"})
    result = client.post(f"/api/cases/{case['id']}/routed-search", headers=headers(client),
                         json={"query": "zzzz-no-document", "as_of": "2025-07-01"})
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["route"]["route"] == "graph" and body["paths"]
    assert "empty_retrieval_bounded_graph_fallback" in body["route"]["clarification"]
    assert all(path["hops"] <= 3 for path in body["paths"])


@pytest.mark.parametrize("change_path", [False, True])
def test_assistant_uses_current_graph_tool_and_revalidates_path(client, monkeypatch, change_path):
    client.post("/api/admin/import-demo", headers=headers(client, "admin"))
    case = create(client, {"sku_id": "sku-hit", "as_of": "2025-07-01"})
    calls = []
    def complete(self, messages, tools=None):
        calls.append(tools)
        if tools is None:
            content = {"route": "graph", "graph_start_id": "product:sku-hit",
                       "graph_target_kind": "order", "reason": "核对订单来源"}
            message = {"role": "assistant", "content": json.dumps(content)}
        elif len(calls) == 2:
            assert any(tool["function"]["name"] == "read_graph_evidence" for tool in tools)
            from test_retrieval_agent import tool_call
            message = tool_call("read_graph_evidence", "{}")
        else:
            payload = json.loads(next(message["content"] for message in messages
                                      if message["role"] == "tool"))
            identifiers = payload["evidence_refs"]
            assert any(identifier.startswith("graph-path:") for identifier in identifiers)
            if change_path:
                from quality.models import DemoOrder
                with client.app.state.sessions.begin() as session:
                    session.get(DemoOrder, "demo-order-hit").quantity += 1
            message = {"role": "assistant", "content": json.dumps({
                "recommendation": "needs_info", "explanation": "模拟模型核对真实SQL路径",
                "evidence_ids": identifiers, "missing_fields": [],
            })}
        return {"message": message, "engine_mode": "fake", "usage": {}, "elapsed_ms": 0,
                "request_meta": {"attempts": 1}}
    monkeypatch.setattr(AgnesClient, "complete", complete)
    response = client.post(f"/api/cases/{case['id']}/assistant", headers=headers(client),
                           json={"question": "核对SKU与订单关系", "as_of": "2025-07-01",
                                 "routing": "adaptive"})
    assert response.status_code == (409 if change_path else 200), response.text
    if change_path:
        assert response.json()["detail"]["code"] == "snapshot_changed"
    else:
        assert any(identifier.startswith("graph-path:")
                   for identifier in response.json()["advice"]["evidence_ids"])
    assert len(calls) == 3


def test_routed_api_rechecks_graph_after_first_read(client, monkeypatch):
    client.post("/api/admin/import-demo", headers=headers(client, "admin"))
    case = create(client, {"sku_id": "sku-hit", "as_of": "2025-07-01"})
    original = client.app.state.path_reader
    reads = []
    def changing_reader(*args, **kwargs):
        result = original(*args, **kwargs)
        reads.append(result)
        if len(reads) == 1:
            from quality.models import DemoOrder
            with client.app.state.sessions.begin() as session:
                session.get(DemoOrder, "demo-order-hit").quantity += 1
        return result
    monkeypatch.setattr(client.app.state, "path_reader", changing_reader)
    response = client.post(f"/api/cases/{case['id']}/routed-search", headers=headers(client),
                           json={"query": "订单关联", "policy": "all", "as_of": "2025-07-01"})
    assert response.status_code == 409, response.text
    assert len(reads) == 2
