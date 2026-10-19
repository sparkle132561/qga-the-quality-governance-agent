import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import headers
from fastapi.testclient import TestClient
from quality.config import Settings
from quality.main import create_app
from quality.models import ReviewCase
from sqlalchemy import text


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="需显式配置独立 PostgreSQL 测试库")
def test_cancel_flags_survive_restart_for_workflow_and_assistant():
    from uuid import uuid4

    from quality.models import AssistantRunControl

    url = os.environ["TEST_DATABASE_URL"]
    if not url.endswith("/quality_test"):
        pytest.fail("集成验证仅使用 quality_test 测试库")
    settings = Settings(
        database_url=url,
        jwt_secret="integration-only-" * 4,
        agnes_api_key="",
        siliconflow_api_key="",
        model_mode="fake",
    )
    app = create_app(settings)
    identifier = str(uuid4())
    with TestClient(app) as first:
        owner = headers(first)
        case = first.post(
            "/api/cases", headers=owner, json={"title": "持久取消恢复", "workflow": "remediation"}
        ).json()
        with app.state.sessions.begin() as session:
            row = session.get(ReviewCase, case["id"])
            row.status = "analyzing"
            row.active_run = {
                "worker_id": app.state.worker_id,
                "run_id": identifier,
                "thread_id": "cancelled:" + identifier,
                "cancel_requested": True,
            }
            session.add(
                AssistantRunControl(
                    id=identifier,
                    case_id=case["id"],
                    actor_id="merchant-a",
                    revision=1,
                    thread_id=case["id"] + ":1:agent:" + identifier,
                    worker_id=app.state.worker_id,
                    cancel_requested=True,
                )
            )
    with TestClient(create_app(settings)) as second:
        path = f"/api/cases/{case['id']}"
        recovered = second.post(path + "/recover", headers=headers(second, "reviewer"))
        assert recovered.status_code == 200 and recovered.json()["status"] == "submitted"
        recovered = second.post(
            path + f"/assistant-requests/{identifier}/recover", headers=headers(second)
        )
        assert recovered.status_code == 200 and recovered.json()["status"] == "cancelled"


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="需显式配置独立 PostgreSQL 测试库")
def test_verified_memory_survives_restart_with_source_checkpoint(monkeypatch):
    from test_impact import analyze_incident, fake_impact
    from test_memory import verified

    url = os.environ["TEST_DATABASE_URL"]
    if not url.endswith("/quality_test"):
        pytest.fail("集成验证仅使用 quality_test 测试库")
    settings = Settings(
        database_url=url,
        jwt_secret="integration-only-" * 4,
        agnes_api_key="",
        siliconflow_api_key="",
        model_mode="fake",
    )
    with TestClient(create_app(settings)) as first:
        memory, _, _, _ = verified(first, monkeypatch)
    with TestClient(create_app(settings)) as second:
        fake_impact(monkeypatch)
        path, owner, analysis = analyze_incident(second)
        memories = second.get(path + "/reference-memories", headers=owner).json()["items"]
        assert memory["id"] in {row["id"] for row in memories}
        assert any(
            memory["id"] in step["context"]["selected_memory_ids"]
            for branch in analysis["branches"].values()
            for step in branch["trace"]
            if step["event"] == "model"
        )
        assert second.get("/api/memories", headers=headers(second, "merchant-b")).json() == []


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="需显式配置独立 PostgreSQL 测试库")
def test_work_order_review_concurrency_and_closure_survive_restart(monkeypatch):
    from quality.models import SimulatedEffect, WorkOrder
    from sqlalchemy import func, select
    from test_remediation import opened_work_order, review_case

    url = os.environ["TEST_DATABASE_URL"]
    if not url.endswith("/quality_test"):
        pytest.fail("集成验证仅使用 quality_test 测试库")
    settings = Settings(
        database_url=url,
        jwt_secret="integration-only-" * 4,
        agnes_api_key="",
        siliconflow_api_key="",
        model_mode="fake",
    )
    app = create_app(settings)
    with TestClient(app) as first:
        identifier, owner, _ = opened_work_order(first, monkeypatch)
        body = {
            "expected_revision": 1,
            "review_kind": "appeal",
            "statement": "模拟并发申诉",
            "as_of": "2025-07-01",
        }
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _: first.post(
                        f"/api/work-orders/{identifier}/review-cases",
                        headers=owner,
                        json=body,
                    ),
                    range(2),
                )
            )
        assert sorted(r.status_code for r in results) == [201, 409]
        identifier, path, _, reviewer, analysis = review_case(first, monkeypatch)
        assert app.state.remediation_graph.get_state(
            {"configurable": {"thread_id": analysis["thread_id"]}}
        ).next == ("human_gate",)
    second_app = create_app(settings)
    with TestClient(second_app) as second:
        reviewer = headers(second, "reviewer")
        decided = second.post(
            path + "/decision",
            headers=reviewer,
            json={
                "expected_revision": analysis["revision"],
                "analysis_run_id": analysis["run_id"],
                "decision": "approve",
                "note": "模拟重启后复核关闭工单",
            },
        )
        assert decided.status_code == 200, decided.text
        approval_id = decided.json()["id"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _: second.post(
                        path + f"/approvals/{approval_id}/execute",
                        headers=reviewer,
                        json={
                            "expected_revision": analysis["revision"],
                            "analysis_run_id": analysis["run_id"],
                        },
                    ),
                    range(2),
                )
            )
        assert all(r.status_code == 200 for r in results), [r.text for r in results]
        assert sorted(r.json()["idempotent"] for r in results) == [False, True]
        with second_app.state.sessions() as session:
            order = session.get(WorkOrder, identifier)
            assert order.status == "closed" and order.revision == 3
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(SimulatedEffect)
                    .where(
                        SimulatedEffect.approval_id == approval_id,
                    )
                )
                == 1
            )


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="需显式配置独立 PostgreSQL 测试库")
def test_postgres_checkpoint_survives_app_restart():
    url = os.environ["TEST_DATABASE_URL"]
    if not url.endswith("/quality_test"):
        pytest.fail("集成验证仅使用 quality_test 测试库")
    settings = Settings(
        database_url=url,
        jwt_secret="integration-only-" * 4,
        agnes_api_key="",
        siliconflow_api_key="",
        model_mode="fake",
    )
    first_app = create_app(settings)
    with TestClient(first_app) as first:
        merchant = headers(first)
        reviewer = headers(first, "reviewer")
        case = first.post(
            "/api/cases",
            headers=merchant,
            json={"title": "PostgreSQL恢复验证", "workflow": "remediation"},
        ).json()
        path = f"/api/cases/{case['id']}"
        assert first.post(path + "/submit", headers=merchant).status_code == 200
        analysis = first.post(path + "/analyze", headers=reviewer)
        assert analysis.status_code == 200, analysis.text
        output = analysis.json()
        with first_app.state.sessions() as session:
            distance = session.execute(
                text("SELECT '[1,0,0]'::vector <-> '[1,0,0]'::vector")
            ).scalar_one()
            assert distance == 0
        interrupted = first.post(
            "/api/cases",
            headers=merchant,
            json={"title": "进程中断恢复", "workflow": "remediation"},
        ).json()
        with first_app.state.sessions.begin() as session:
            abandoned = session.get(ReviewCase, interrupted["id"])
            abandoned.status = "analyzing"
            abandoned.active_run = {
                "run_id": "abandoned-run",
                "thread_id": "abandoned-thread",
                "worker_id": first_app.state.worker_id,
                "started_at": 1,
                "revision": 1,
            }
        assert (
            first.post(f"/api/cases/{interrupted['id']}/recover", headers=reviewer).status_code
            == 409
        )
    second_app = create_app(settings)
    with TestClient(second_app) as second:
        fetched = second.get(path, headers=headers(second)).json()
        assert fetched["latest_analysis"]["run_id"] == output["run_id"]
        snapshot = second_app.state.graph.get_state(
            {"configurable": {"thread_id": output["thread_id"]}}
        )
        assert snapshot.values["case_id"] == case["id"]
        assert set(snapshot.values["summary"]["branches"]) == {"rules", "product", "material"}
        assert second.get(path, headers=headers(second, "merchant-b")).status_code == 404
        interrupted_path = f"/api/cases/{interrupted['id']}"
        assert (
            second.post(interrupted_path + "/recover", headers=headers(second)).status_code == 403
        )
        recovered = second.post(interrupted_path + "/recover", headers=headers(second, "reviewer"))
        assert recovered.status_code == 200
        assert recovered.json()["status"] == "submitted"
        assert (
            second.post(
                interrupted_path + "/analyze", headers=headers(second, "reviewer")
            ).status_code
            == 200
        )


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="需显式配置独立 PostgreSQL 测试库")
def test_agent_error_checkpoint_survives_app_restart():
    url = os.environ["TEST_DATABASE_URL"]
    if not url.endswith("/quality_test"):
        pytest.fail("集成验证仅使用 quality_test 测试库")
    settings = Settings(
        database_url=url,
        jwt_secret="integration-only-" * 4,
        agnes_api_key="",
        siliconflow_api_key="",
        model_mode="fake",
    )
    with TestClient(create_app(settings)) as first:
        merchant = headers(first)
        case = first.post(
            "/api/cases",
            headers=merchant,
            json={"title": "工具图失败记录恢复", "workflow": "qualification"},
        ).json()
        response = first.post(
            f"/api/cases/{case['id']}/assistant",
            headers=merchant,
            json={"question": "证书资料是否足够", "as_of": "2025-07-01"},
        )
        assert response.status_code == 503
        thread_id = response.json()["detail"]["thread_id"]
    with TestClient(create_app(settings)) as second:
        path = f"/api/cases/{case['id']}/agent-runs/{thread_id}"
        response = second.get(path, headers=headers(second))
        assert response.status_code == 200
        saved = response.json()
        assert saved["status"] == "failed"
        assert saved["error_code"] == "missing_agnes_key"
        assert saved["engine_mode"] == "not_called"
        assert saved["trace"] == [
            {
                "event": "model_error",
                "code": "missing_agnes_key",
                "request_meta": {"attempts": 0, "retries": 0, "queue_ms": 0},
            }
        ]
        assert second.get(path, headers=headers(second, "merchant-b")).status_code == 404


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="需显式配置独立 PostgreSQL 测试库")
def test_document_graph_review_persists_on_postgres(monkeypatch):
    from quality.models import EvidenceChunk
    from quality.retrieval import index_units
    from sqlalchemy import select

    url = os.environ["TEST_DATABASE_URL"]
    if not url.endswith("/quality_test"):
        pytest.fail("集成验证仅使用 quality_test 测试库")
    settings = Settings(
        database_url=url,
        jwt_secret="integration-only-" * 4,
        agnes_api_key="",
        siliconflow_api_key="",
        model_mode="fake",
    )
    body = {
        "entities": [
            {"key": "s", "kind": "supplier", "name": "供应商甲", "qualifiers": {}},
            {"key": "p", "kind": "product", "name": "K170", "qualifiers": {}},
        ],
        "relations": [
            {"subject": "s", "predicate": "supplies", "object": "p", "quote": "供应商甲为K170供货"}
        ],
    }
    monkeypatch.setattr(
        "quality.knowledge_graph.AgnesClient.complete",
        lambda *args: {
            "message": {"role": "assistant", "content": json.dumps(body)},
            "engine_mode": "fake",
            "usage": {},
            "elapsed_ms": 0,
        },
    )
    app = create_app(settings)
    with TestClient(app) as first:
        owner = headers(first)
        case = first.post(
            "/api/cases",
            headers=owner,
            json={"title": "图关系数据库恢复", "workflow": "qualification"},
        ).json()
        with app.state.sessions.begin() as session:
            index_units(
                session,
                "case:" + case["id"],
                [{"text": "供应商甲为K170供货", "location": {"page": 1}}],
                "f" * 64,
                True,
                case_id=case["id"],
            )
            chunk_id = session.scalar(
                select(EvidenceChunk.id).where(EvidenceChunk.case_id == case["id"])
            )
        extracted = first.post(
            "/api/graph/extractions",
            headers=owner,
            json={"chunk_id": chunk_id, "case_id": case["id"], "as_of": "2025-07-01"},
        )
        assert extracted.status_code == 200, extracted.text
        extraction_id = extracted.json()["id"]
        reviewed = first.post(
            f"/api/graph/extractions/{extraction_id}/review",
            headers=headers(first, "reviewer"),
            json={"decision": "confirmed", "note": "模拟协议核验", "as_of": "2025-07-01"},
        )
        assert reviewed.status_code == 200, reviewed.text
    with TestClient(create_app(settings)) as second:
        graph_path = f"/api/graph?as_of=2025-07-01&case_id={case['id']}"
        graph = second.get(graph_path, headers=headers(second)).json()
        edges = [edge for edge in graph["edges"] if edge["origin"] == "reviewed_document"]
        assert len(edges) == 1
        assert edges[0]["provenance"]["extraction_id"] == extraction_id
        assert edges[0]["provenance"]["engine_mode"] == "fake"
        assert edges[0]["provenance"]["reviewed_by"] == "reviewer"
        paths = second.post(
            "/api/graph/paths",
            headers=headers(second),
            json={
                "as_of": "2025-07-01",
                "case_id": case["id"],
                "start_id": f"doc:{extraction_id}:s",
                "end_id": f"doc:{extraction_id}:p",
            },
        )
        assert paths.status_code == 200, paths.text
        assert len(paths.json()["paths"]) == 1
        assert {hit["id"] for hit in paths.json()["evidence"]} == {chunk_id}
        assert second.get(graph_path, headers=headers(second, "merchant-b")).status_code == 404


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="需显式配置独立 PostgreSQL 测试库")
def test_qualification_parent_and_branch_checkpoints_survive_restart():
    url = os.environ["TEST_DATABASE_URL"]
    if not url.endswith("/quality_test"):
        pytest.fail("集成验证仅使用 quality_test 测试库")
    settings = Settings(
        database_url=url,
        jwt_secret="integration-only-" * 4,
        agnes_api_key="",
        siliconflow_api_key="",
        model_mode="fake",
    )
    app = create_app(settings)
    with TestClient(app) as first:
        first.post("/api/admin/import-demo", headers=headers(first, "admin"))
        owner = headers(first)
        case = first.post(
            "/api/cases",
            headers=owner,
            json={
                "title": "准入父子图恢复",
                "workflow": "qualification",
                "payload": {"sku_id": "sku-hit", "as_of": "2025-07-01"},
            },
        ).json()
        path = f"/api/cases/{case['id']}"
        first.post(path + "/submit", headers=owner)
        response = first.post(path + "/analyze", headers=headers(first, "reviewer"))
        assert response.status_code == 200, response.text
        output = response.json()
        assert output["engine_mode"] == "partial"
        # 模拟父图完成后，业务写回前进程退出。
        with app.state.sessions.begin() as session:
            found = session.get(ReviewCase, case["id"])
            found.status = "analyzing"
            found.latest_analysis = None
            found.active_run = {
                "run_id": output["run_id"],
                "thread_id": output["thread_id"],
                "worker_id": app.state.worker_id,
                "revision": 1,
                "graph_kind": "qualification",
                "started_at": 1,
            }
    next_app = create_app(settings)
    with TestClient(next_app) as second:
        recovered = second.post(path + "/recover", headers=headers(second, "reviewer"))
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["status"] == "needs_info"
        analysis = recovered.json()["latest_analysis"]
        assert analysis["engine_mode"] == "partial"
        assert set(analysis["branches"]) == {"rules", "product", "material", "history"}
        from quality.agent_core import ToolExecutor
        from quality.retrieval_agent import build_retrieval_agent

        graph = build_retrieval_agent(None, ToolExecutor([]), next_app.state.checkpointer)
        branch_thread = analysis["branches"]["rules"]["thread_id"]
        saved = graph.get_state({"configurable": {"thread_id": branch_thread}}).values
        assert saved["error_code"] == "missing_agnes_key"
        assert saved["engine_mode"] == "not_called"
        assert second.get(path, headers=headers(second, "merchant-b")).status_code == 404


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="需显式配置独立 PostgreSQL 测试库")
def test_incident_recovery_and_concurrent_confirmation_persist_on_postgres():
    from concurrent.futures import ThreadPoolExecutor

    from quality.models import IncidentDraft, QualityEvent
    from sqlalchemy import func, select
    from test_incidents import PROPOSAL, TEXT

    url = os.environ["TEST_DATABASE_URL"]
    if not url.endswith("/quality_test"):
        pytest.fail("集成验证仅使用 quality_test 测试库")
    settings = Settings(
        database_url=url,
        jwt_secret="integration-only-" * 4,
        agnes_api_key="",
        siliconflow_api_key="",
        model_mode="fake",
    )
    first_app = create_app(settings)
    with TestClient(first_app) as first:
        first.post("/api/admin/import-demo", headers=headers(first, "admin"))
        created = first.post(
            "/api/incidents/drafts",
            headers=headers(first, "reviewer"),
            json={
                "source_id": "demo-platform-policy",
                "title": "PG公告范围",
                "raw_text": TEXT,
            },
        ).json()
        identifier = created["id"]
        with first_app.state.sessions.begin() as session:
            row = session.get(IncidentDraft, identifier)
            row.status = "extracting"
            row.active_run = {"worker_id": first_app.state.worker_id, "run_id": "abandoned"}
    second_app = create_app(settings)
    with TestClient(second_app) as second:
        reviewer = headers(second, "reviewer")
        path = f"/api/incidents/drafts/{identifier}"
        assert second.post(path + "/recover", headers=reviewer).status_code == 200
        decision = {
            "expected_revision": 1,
            "proposal": PROPOSAL,
            "note": "模拟字段的并发确认机制检查",
        }
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(
                pool.map(
                    lambda _: second.post(path + "/confirm", headers=reviewer, json=decision),
                    range(2),
                )
            )
        assert sorted(response.status_code for response in responses) == [200, 409]
        with second_app.state.sessions() as session:
            count = session.scalar(
                select(func.count())
                .select_from(QualityEvent)
                .where(QualityEvent.id == "imported:" + identifier)
            )
            assert count == 1
    with TestClient(create_app(settings)) as third:
        saved = next(
            row
            for row in third.get("/api/incidents/drafts", headers=headers(third, "reviewer")).json()
            if row["id"] == identifier
        )
        assert saved["status"] == "confirmed" and saved["event_id"] == "imported:" + identifier
        assert saved["proposal"]["date_precision"] == "day"


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="需显式配置独立 PostgreSQL 测试库")
def test_impact_checkpoint_and_exact_sets_recover_on_postgres(monkeypatch):
    from test_impact import fake_impact

    url = os.environ["TEST_DATABASE_URL"]
    if not url.endswith("/quality_test"):
        pytest.fail("集成验证仅使用 quality_test 测试库")
    settings = Settings(
        database_url=url,
        jwt_secret="integration-only-" * 4,
        agnes_api_key="",
        siliconflow_api_key="",
        model_mode="fake",
    )
    fake_impact(monkeypatch)
    app = create_app(settings)
    with TestClient(app) as first:
        first.post("/api/admin/import-demo", headers=headers(first, "admin"))
        owner = headers(first)
        case = first.post(
            "/api/cases",
            headers=owner,
            json={
                "title": "影响集合与父图恢复",
                "workflow": "incident",
                "payload": {"event_id": "demo-recall-001", "as_of": "2025-07-01"},
            },
        ).json()
        path = f"/api/cases/{case['id']}"
        first.post(path + "/submit", headers=owner)
        analyzed = first.post(path + "/analyze", headers=headers(first, "reviewer"))
        assert analyzed.status_code == 200, analyzed.text
        output = analyzed.json()
        assert output["engine_mode"] == "fake"
        assert output["impact"]["affected_product_ids"] == ["sku-hit"]
        with app.state.sessions.begin() as session:
            found = session.get(ReviewCase, case["id"])
            found.status = "analyzing"
            found.latest_analysis = None
            found.active_run = {
                "worker_id": app.state.worker_id,
                "graph_kind": "impact",
                "run_id": output["run_id"],
                "thread_id": output["thread_id"],
                "revision": 1,
                "started_at": 1,
            }
    with TestClient(create_app(settings)) as second:
        recovered = second.post(path + "/recover", headers=headers(second, "reviewer"))
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["status"] == "waiting_review"
        impact = recovered.json()["latest_analysis"]["impact"]
        assert impact["affected_order_ids"] == ["demo-order-hit"]
        assert impact["excluded_product_ids"] == ["sku-date-out", "sku-near-model"]
        assert second.get(path, headers=headers(second, "merchant-b")).status_code == 404


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="需显式配置独立 PostgreSQL 测试库")
def test_human_interrupt_and_concurrent_effects_recover_on_postgres(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from quality.models import ApprovalRecord, SimulatedEffect
    from sqlalchemy import func, select
    from test_impact import fake_impact

    url = os.environ["TEST_DATABASE_URL"]
    if not url.endswith("/quality_test"):
        pytest.fail("集成验证仅使用 quality_test 测试库")
    settings = Settings(
        database_url=url,
        jwt_secret="integration-only-" * 4,
        agnes_api_key="",
        siliconflow_api_key="",
        model_mode="fake",
    )
    fake_impact(monkeypatch)
    first_app = create_app(settings)
    with TestClient(first_app) as first:
        first.post("/api/admin/import-demo", headers=headers(first, "admin"))
        owner = headers(first)
        case = first.post(
            "/api/cases",
            headers=owner,
            json={
                "title": "持久审批与并发处置",
                "workflow": "incident",
                "payload": {"event_id": "demo-recall-001", "as_of": "2025-07-01"},
            },
        ).json()
        path = f"/api/cases/{case['id']}"
        first.post(path + "/submit", headers=owner)
        analyzed = first.post(path + "/analyze", headers=headers(first, "reviewer"))
        assert analyzed.status_code == 200, analyzed.text
        analysis = analyzed.json()
        state = first_app.state.impact_graph.get_state(
            {"configurable": {"thread_id": analysis["thread_id"]}}
        )
        assert state.next == ("human_gate",)
    second_app = create_app(settings)
    with TestClient(second_app) as second:
        reviewer = headers(second, "reviewer")
        body = {
            "expected_revision": analysis["revision"],
            "analysis_run_id": analysis["run_id"],
            "decision": "approve",
            "note": "模拟PG人工中断恢复与并发幂等",
        }
        with ThreadPoolExecutor(max_workers=2) as pool:
            decided = list(
                pool.map(
                    lambda _: second.post(path + "/decision", headers=reviewer, json=body), range(2)
                )
            )
        assert all(response.status_code == 200 for response in decided), [r.text for r in decided]
        assert sorted(r.json()["idempotent"] for r in decided) == [False, True]
        identifier = decided[0].json()["id"]
        execute_body = {
            "expected_revision": analysis["revision"],
            "analysis_run_id": analysis["run_id"],
        }
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(
                pool.map(
                    lambda _: second.post(
                        path + f"/approvals/{identifier}/execute",
                        headers=reviewer,
                        json=execute_body,
                    ),
                    range(2),
                )
            )
        assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
        assert sorted(r.json()["idempotent"] for r in responses) == [False, True]
        with second_app.state.sessions() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(ApprovalRecord)
                    .where(ApprovalRecord.case_id == case["id"])
                )
                == 1
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(SimulatedEffect)
                    .where(SimulatedEffect.approval_id == identifier)
                )
                == 3
            )
        assert (
            second.get(path + "/effects", headers=headers(second, "merchant-b")).status_code == 404
        )
