from uuid import NAMESPACE_URL, uuid5

from conftest import headers
from langgraph.types import Command
from quality.models import ProductListing, QualityEvent, WorkOrder
from test_impact import analyze_incident, fake_impact


def ready_case(client, monkeypatch):
    client.post("/api/admin/import-demo", headers=headers(client, "admin"))
    fake_impact(monkeypatch)
    path, owner, analysis = analyze_incident(client)
    body = {
        "expected_revision": analysis["revision"],
        "analysis_run_id": analysis["run_id"],
        "decision": "approve",
        "note": "仅批准本地模拟范围内目标，协议验证",
    }
    return path, owner, analysis, body


def test_interrupt_requires_reviewer_then_simulates_exact_targets_once(client, monkeypatch):
    path, owner, analysis, body = ready_case(client, monkeypatch)
    config = {"configurable": {"thread_id": analysis["thread_id"]}}
    assert client.app.state.impact_graph.get_state(config).next == ("human_gate",)
    assert client.get(path + "/effects", headers=owner).json() == []
    assert client.post(path + "/decision", headers=owner, json=body).status_code == 403
    assert (
        client.post(path + "/decision", headers=headers(client, "admin"), json=body).status_code
        == 403
    )
    reviewer = headers(client, "reviewer")
    decided = client.post(path + "/decision", headers=reviewer, json=body)
    assert decided.status_code == 200, decided.text
    approval = decided.json()
    assert approval["idempotent"] is False
    assert client.app.state.impact_graph.get_state(config).next == ()
    assert client.post(path + "/decision", headers=reviewer, json=body).json()["idempotent"] is True
    assert client.get(path, headers=owner).json()["status"] == "confirmed"
    assert client.get(path + "/effects", headers=owner).json() == []
    execute_body = {
        "expected_revision": body["expected_revision"],
        "analysis_run_id": body["analysis_run_id"],
    }
    action = path + f"/approvals/{approval['id']}/execute"
    assert client.post(action, headers=owner, json=execute_body).status_code == 403
    first = client.post(action, headers=reviewer, json=execute_body)
    assert first.status_code == 200, first.text
    effects = first.json()["effects"]
    assert {effect["kind"] for effect in effects} == {
        "simulate_delist",
        "simulate_notification",
        "create_work_order",
    }
    assert [effect["target_id"] for effect in effects if effect["kind"] == "simulate_delist"] == [
        "sku-hit"
    ]
    second = client.post(action, headers=reviewer, json=execute_body).json()
    assert second["idempotent"] is True and {effect["id"] for effect in second["effects"]} == {
        effect["id"] for effect in effects
    }
    with client.app.state.sessions() as session:
        assert session.get(ProductListing, "sku-hit").status == "delisted"
        assert session.get(ProductListing, "sku-date-out") is None
        from sqlalchemy import select

        assert len(list(session.scalars(select(WorkOrder)))) == 1


def test_source_changed_after_approval_blocks_every_effect(client, monkeypatch):
    path, owner, analysis, body = ready_case(client, monkeypatch)
    reviewer = headers(client, "reviewer")
    approval = client.post(path + "/decision", headers=reviewer, json=body).json()
    with client.app.state.sessions.begin() as session:
        event = session.get(QualityEvent, "demo-recall-001")
        event.scope_json = {**event.scope_json, "production_end": "2025-03-31"}
    response = client.post(
        path + f"/approvals/{approval['id']}/execute",
        headers=reviewer,
        json={"expected_revision": analysis["revision"], "analysis_run_id": analysis["run_id"]},
    )
    assert response.status_code == 409
    assert client.get(path + "/effects", headers=owner).json() == []


def test_reject_and_revoke_never_execute_old_approval(client, monkeypatch):
    path, owner, analysis, body = ready_case(client, monkeypatch)
    reviewer = headers(client, "reviewer")
    approval = client.post(
        path + "/decision", headers=reviewer, json={**body, "decision": "reject"}
    ).json()
    assert client.get(path, headers=owner).json()["status"] == "rejected"
    assert (
        client.post(
            path + f"/approvals/{approval['id']}/execute",
            headers=reviewer,
            json={"expected_revision": analysis["revision"], "analysis_run_id": analysis["run_id"]},
        ).status_code
        == 409
    )
    path, owner, analysis, body = ready_case(client, monkeypatch)
    approval = client.post(path + "/decision", headers=reviewer, json=body).json()
    assert (
        client.post(path + f"/approvals/{approval['id']}/revoke", headers=reviewer).status_code
        == 200
    )
    assert client.get(path, headers=owner).json()["status"] == "submitted"
    assert (
        client.post(
            path + f"/approvals/{approval['id']}/execute",
            headers=reviewer,
            json={"expected_revision": analysis["revision"], "analysis_run_id": analysis["run_id"]},
        ).status_code
        == 409
    )
    assert client.get(path + "/effects", headers=owner).json() == []


def test_checkpoint_decision_can_reconcile_missing_business_write(client, monkeypatch):
    path, _, analysis, body = ready_case(client, monkeypatch)
    case_id = path.rsplit("/", 1)[1]
    identifier = str(uuid5(NAMESPACE_URL, case_id + ":decision:" + analysis["run_id"]))
    envelope = {
        "id": identifier,
        "case_id": case_id,
        "revision": analysis["revision"],
        "run_id": analysis["run_id"],
        "decision": "approve",
        "note": body["note"],
        "actor": {"user_id": "reviewer", "role": "reviewer", "merchant_id": None},
        "destination_versions": {
            "sku-hit": {"version": 0, "status": None, "last_approval_id": None}
        },
    }
    config = {"configurable": {"thread_id": analysis["thread_id"]}}
    client.app.state.impact_graph.invoke(Command(resume=envelope), config)
    response = client.post(path + "/decision", headers=headers(client, "reviewer"), json=body)
    assert response.status_code == 200, response.text
    assert response.json()["id"] == identifier


def test_stale_waiting_analysis_can_be_withdrawn_without_approval(client, monkeypatch):
    path, owner, analysis, body = ready_case(client, monkeypatch)
    with client.app.state.sessions.begin() as session:
        event = session.get(QualityEvent, "demo-recall-001")
        event.scope_json = {**event.scope_json, "production_end": "2025-03-31"}
    reviewer = headers(client, "reviewer")
    assert client.post(path + "/decision", headers=reviewer, json=body).status_code == 409
    bound = {"expected_revision": analysis["revision"], "analysis_run_id": analysis["run_id"]}
    assert client.post(path + "/withdraw-analysis", headers=owner, json=bound).status_code == 403
    assert client.post(path + "/withdraw-analysis", headers=reviewer, json=bound).status_code == 200
    assert client.get(path, headers=owner).json()["status"] == "submitted"
    assert client.post(path + "/analyze", headers=reviewer).status_code == 200


def test_old_qualification_approval_cannot_overwrite_later_delist(client, monkeypatch):
    from test_qualification import create_qualification, fake_model

    owner, qual_path = create_qualification(client)
    fake_model(monkeypatch)
    client.post(qual_path + "/submit", headers=owner)
    reviewer = headers(client, "reviewer")
    analysis = client.post(qual_path + "/analyze", headers=reviewer).json()
    decision = {
        "expected_revision": analysis["revision"],
        "analysis_run_id": analysis["run_id"],
        "decision": "approve",
        "note": "先批准模拟准入，不执行",
    }
    old = client.post(qual_path + "/decision", headers=reviewer, json=decision)
    assert old.status_code == 200, old.text
    path, _, impact, body = ready_case(client, monkeypatch)
    current = client.post(path + "/decision", headers=reviewer, json=body).json()
    executed = client.post(
        path + f"/approvals/{current['id']}/execute",
        headers=reviewer,
        json={"expected_revision": impact["revision"], "analysis_run_id": impact["run_id"]},
    )
    assert executed.status_code == 200, executed.text
    response = client.post(
        qual_path + f"/approvals/{old.json()['id']}/execute",
        headers=reviewer,
        json={"expected_revision": analysis["revision"], "analysis_run_id": analysis["run_id"]},
    )
    assert response.status_code == 409
    with client.app.state.sessions() as session:
        assert session.get(ProductListing, "sku-hit").status == "delisted"


def test_non_demo_batch_or_order_cannot_enter_simulated_effects(client, monkeypatch):
    from quality.models import Batch, DemoOrder

    client.post("/api/admin/import-demo", headers=headers(client, "admin"))
    with client.app.state.sessions.begin() as session:
        session.get(Batch, "batch-sku-hit").is_demo = False
        session.get(DemoOrder, "demo-order-hit").is_demo = False
    path, owner, analysis, body = ready_case(client, monkeypatch)
    reviewer = headers(client, "reviewer")
    response = client.post(path + "/decision", headers=reviewer, json=body)
    assert response.status_code == 200, response.text
    response = client.post(
        path + f"/approvals/{response.json()['id']}/execute",
        headers=reviewer,
        json={"expected_revision": analysis["revision"], "analysis_run_id": analysis["run_id"]},
    )
    assert response.status_code == 409
    assert client.get(path + "/effects", headers=owner).json() == []
