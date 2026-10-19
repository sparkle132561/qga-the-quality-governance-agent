import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest
from conftest import headers
from fastapi import HTTPException
from langgraph.checkpoint.memory import InMemorySaver
from quality.agent_core import Tool, ToolExecutor
from quality.domain import ToolResult
from quality.models import AssistantRunControl, ReviewCase
from quality.providers import AgnesClient, ProviderError
from quality.request_gate import GateUnavailable, RequestGate
from quality.retrieval_agent import EvidenceSearch, build_retrieval_agent
from test_impact import fake_impact
from test_retrieval_agent import ScriptedModel, final, initial, tool_call


def test_shared_gate_bounds_active_requests_and_start_window_without_live_calls():
    clock = [0.0]
    gate = RequestGate(concurrent=2, starts=2, window=10, clock=lambda: clock[0])
    assert gate.try_acquire() and gate.try_acquire()
    assert not gate.try_acquire()
    gate.release()
    with pytest.raises(GateUnavailable, match="local_rate_limited"):
        with gate.permit(wait_seconds=0):
            pytest.fail("频率窗口已用尽")
    clock[0] = 10.0
    with gate.permit(wait_seconds=0):
        assert gate.active == 2
    gate.release()
    assert gate.active == 0
    with pytest.raises(GateUnavailable, match="analysis_cancelled"):
        with gate.permit(control=lambda: True):
            pytest.fail("取消不能获得请求许可")


@pytest.mark.parametrize(
    "first_code,retry_after,attempts",
    [(503, None, 2), (401, None, 1), (429, "120", 1), (429, "0", 2)],
)
def test_provider_read_retry_is_bounded_and_respects_retry_after(first_code, retry_after, attempts):
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                first_code, headers={"Retry-After": retry_after} if retry_after else {}
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "fixture answer"}}]}
        )

    model = AgnesClient("fixture-key", transport=httpx.MockTransport(respond))
    if attempts == 1:
        with pytest.raises(ProviderError) as failure:
            model.complete([{"role": "user", "content": "test"}])
        assert failure.value.request_meta["attempts"] == 1
    else:
        result = model.complete([{"role": "user", "content": "test"}])
        assert result["request_meta"]["retries"] == 1
        assert result["engine_mode"] == "contract_fixture"
    assert len(calls) == attempts


def test_cancel_between_tools_stops_next_tool_and_model_with_paired_results():
    stopped, executions = [False], []

    def search(arguments, actor):
        executions.append(arguments.query)
        stopped[0] = True
        return ToolResult(status="not_found")

    reply = tool_call()
    reply["tool_calls"].extend(tool_call(args='{"query":"second"}', call_id="call-2")["tool_calls"])
    model = ScriptedModel([reply])
    executor = ToolExecutor(
        [Tool("search_evidence", "test", EvidenceSearch, frozenset({"rules"}), search)]
    )
    result = build_retrieval_agent(
        model,
        executor,
        InMemorySaver(),
        control=lambda: "analysis_cancelled" if stopped[0] else None,
    ).invoke(
        initial(),
        {"configurable": {"thread_id": "cancel-tools"}},
    )
    assert result["status"] == "cancelled" and model.calls == 1
    assert executions == ["证书"]
    assert [
        message["tool_call_id"] for message in result["messages"] if message["role"] == "tool"
    ] == ["call-1", "call-2"]


def test_workflow_cancel_is_revision_bound_persisted_and_never_writes_advice(client, monkeypatch):
    client.post("/api/admin/import-demo", headers=headers(client, "admin"))
    owner, reviewer = headers(client), headers(client, "reviewer")
    case = client.post(
        "/api/cases",
        headers=owner,
        json={
            "title": "取消取证",
            "workflow": "incident",
            "payload": {"event_id": "demo-recall-001", "as_of": "2025-07-01"},
        },
    ).json()
    path = "/api/cases/" + case["id"]
    client.post(path + "/submit", headers=owner)
    fake_impact(monkeypatch)
    original = AgnesClient.complete
    entered, release = threading.Event(), threading.Event()

    def slow(self, messages, tools):
        entered.set()
        assert release.wait(5)
        return original(self, messages, tools)

    monkeypatch.setattr(AgnesClient, "complete", slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(lambda: client.post(path + "/analyze", headers=reviewer))
        assert entered.wait(5)
        try:
            status = client.get(path + "/analysis-status", headers=owner).json()
            body = {
                "expected_revision": status["revision"],
                "analysis_run_id": status["run_id"],
                "note": "人工取消本次运行",
            }
            assert (
                client.post(path + "/cancel-analysis", headers=owner, json=body).status_code == 403
            )
            assert (
                client.post(
                    path + "/cancel-analysis",
                    headers=reviewer,
                    json={**body, "analysis_run_id": "old-run"},
                ).status_code
                == 409
            )
            cancelled = client.post(path + "/cancel-analysis", headers=reviewer, json=body)
            assert cancelled.status_code == 202, cancelled.text
            assert (
                client.post(path + "/cancel-analysis", headers=reviewer, json=body).json()[
                    "idempotent"
                ]
                is True
            )
            assert (
                client.get(path + "/analysis-status", headers=owner).json()["cancel_requested"]
                is True
            )
        finally:
            release.set()
        response = pending.result(timeout=10)
    assert response.status_code == 409 and response.json()["detail"]["code"] == "analysis_cancelled"
    case = client.get(path, headers=owner).json()
    assert case["status"] == "submitted" and case["latest_analysis"] is None
    assert client.get(path + "/effects", headers=owner).json() == []
    actions = [event["action"] for event in client.get(path + "/audit", headers=owner).json()]
    assert actions.count("analysis_cancel_requested") == 1 and "analysis_cancelled" in actions


def test_recovery_of_cancelled_old_worker_clears_stale_result(client):
    owner, reviewer = headers(client), headers(client, "reviewer")
    case = client.post(
        "/api/cases", headers=owner, json={"title": "取消后进程退出", "workflow": "remediation"}
    ).json()
    with client.app.state.sessions.begin() as session:
        row = session.get(ReviewCase, case["id"])
        row.status = "analyzing"
        row.latest_analysis = {"recommendation": "review_ready"}
        row.active_run = {
            "worker_id": "previous-worker",
            "run_id": "cancelled-run",
            "thread_id": "cancelled-thread",
            "cancel_requested": True,
        }
    response = client.post(f"/api/cases/{case['id']}/recover", headers=reviewer)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "submitted" and response.json()["latest_analysis"] is None


def test_assistant_cancel_is_separate_from_case_and_request_id_cannot_replay(client, monkeypatch):
    owner = headers(client)
    case = client.post(
        "/api/cases", headers=owner, json={"title": "独立助手取消", "workflow": "qualification"}
    ).json()
    path = "/api/cases/" + case["id"]
    identifier = str(uuid4())
    started, release = threading.Event(), threading.Event()

    def slow(self, messages, tools):
        started.set()
        assert release.wait(5)
        return {"message": final("needs_info"), "usage": {}, "elapsed_ms": 0, "engine_mode": "fake"}

    monkeypatch.setattr(AgnesClient, "complete", slow)
    body = {"question": "模拟取证", "as_of": "2025-07-01", "client_request_id": identifier}
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(lambda: client.post(path + "/assistant", headers=owner, json=body))
        assert started.wait(5)
        try:
            control_path = path + f"/assistant-requests/{identifier}"
            assert client.get(control_path, headers=owner).json()["status"] == "running"
            assert client.post(path + "/assistant", headers=owner, json=body).status_code == 409
            assert (
                client.post(
                    control_path + "/cancel", headers=headers(client, "merchant-b")
                ).status_code
                == 404
            )
            assert client.post(control_path + "/cancel", headers=owner).status_code == 202
        finally:
            release.set()
        response = pending.result(timeout=10)
    assert response.status_code == 409 and response.json()["detail"]["code"] == "analysis_cancelled"
    assert client.get(control_path, headers=owner).json()["status"] == "cancelled"
    case = client.get(path, headers=owner).json()
    assert case["status"] == "draft" and case["latest_analysis"] is None


def test_assistant_old_worker_recovery_is_not_resume_of_old_advice(client):
    owner = headers(client)
    case = client.post(
        "/api/cases", headers=owner, json={"title": "助手旧进程", "workflow": "qualification"}
    ).json()
    identifier = str(uuid4())
    with client.app.state.sessions.begin() as session:
        session.add(
            AssistantRunControl(
                id=identifier,
                case_id=case["id"],
                actor_id="merchant-a",
                revision=1,
                thread_id=case["id"] + ":1:agent:" + identifier,
                worker_id="previous-worker",
            )
        )
    path = f"/api/cases/{case['id']}/assistant-requests/{identifier}"
    recovered = client.post(path + "/recover", headers=owner)
    assert recovered.status_code == 200 and recovered.json()["status"] == "interrupted"
    assert client.post(path + "/cancel", headers=owner).status_code == 409


@pytest.mark.parametrize("exception_kind", ["runtime", "http"])
def test_post_model_validation_failure_closes_request_and_invalidates_checkpoint(
    client, monkeypatch, exception_kind
):
    owner = headers(client)
    case = client.post(
        "/api/cases", headers=owner, json={"title": "最终校验失败", "workflow": "qualification"}
    ).json()
    identifier = str(uuid4())

    def response(self, messages, tools):
        def broken(*args):
            if exception_kind == "http":
                raise HTTPException(409, "simulated unverified source")
            raise RuntimeError("simulated validation failure")

        monkeypatch.setattr(client.app.state, "workflow_snapshot", broken)
        return {"message": final("needs_info"), "usage": {}, "elapsed_ms": 0, "engine_mode": "fake"}

    monkeypatch.setattr(AgnesClient, "complete", response)
    path = "/api/cases/" + case["id"]
    failed = client.post(
        path + "/assistant",
        headers=owner,
        json={"question": "最终校验", "as_of": "2025-07-01", "client_request_id": identifier},
    )
    if exception_kind == "http":
        assert (
            failed.status_code == 409 and failed.json()["detail"] == "simulated unverified source"
        )
    else:
        assert (
            failed.status_code == 503
            and failed.json()["detail"]["code"] == "assistant_validation_failed"
        )
    assert (
        client.get(path + f"/assistant-requests/{identifier}", headers=owner).json()["status"]
        == "failed"
    )
    thread = client.get(path + f"/assistant-requests/{identifier}", headers=owner).json()[
        "thread_id"
    ]
    state = client.get(path + "/agent-runs/" + thread, headers=owner).json()
    assert state["status"] == "failed" and state["final"] == {}


def test_success_after_503_persists_real_attempt_count_in_checkpoint():
    count = [0]

    def respond(request):
        count[0] += 1
        if count[0] == 1:
            return httpx.Response(503)
        return httpx.Response(
            200, json={"choices": [{"message": final("needs_info")}], "usage": {"total_tokens": 12}}
        )

    model = AgnesClient("fixture-key", transport=httpx.MockTransport(respond))
    saver = InMemorySaver()
    graph = build_retrieval_agent(model, ToolExecutor([]), saver)
    config = {"configurable": {"thread_id": "http-retry-trace"}}
    graph.invoke(initial(), config)
    trace = graph.get_state(config).values["trace"]
    event = next(step for step in trace if step["event"] == "model")
    assert event["request_meta"]["attempts"] == 2 and event["request_meta"]["retries"] == 1
    assert event["usage"]["total_tokens"] == 12
