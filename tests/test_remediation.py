import json
from datetime import date

import pytest
from conftest import headers
from quality.models import Batch, ProductListing, QualityEvent, RuleVersion, WorkOrder
from test_approvals import ready_case


def opened_work_order(client, monkeypatch):
    path, owner, analysis, body = ready_case(client, monkeypatch)
    reviewer = headers(client, "reviewer")
    approval = client.post(path + "/decision", headers=reviewer, json=body).json()
    executed = client.post(
        path + f"/approvals/{approval['id']}/execute",
        headers=reviewer,
        json={"expected_revision": analysis["revision"], "analysis_run_id": analysis["run_id"]},
    )
    assert executed.status_code == 200, executed.text
    order_id = next(
        effect["target_id"]
        for effect in executed.json()["effects"]
        if effect["kind"] == "create_work_order"
    )
    return order_id, owner, reviewer


def fake_review(monkeypatch, invalid=False, risk=None):
    def complete(self, messages, tools):
        tool_messages = [message for message in messages if message["role"] == "tool"]
        if not tool_messages:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "review-read",
                        "type": "function",
                        "function": {"name": "read_branch_evidence", "arguments": "{}"},
                    }
                ],
            }
        else:
            hits = json.loads(tool_messages[-1]["content"])["data"]["hits"]
            specialty = json.loads(messages[1]["content"])["facts"]["specialty"]
            advice = {
                "recommendation": "review_ready",
                "explanation": "协议复核，不是整改有效性判断",
                "evidence_ids": [hit["id"] for hit in hits],
                "missing_fields": [],
                "assessments": [
                    {"source_id": "invented" if invalid else hit["id"], "quote": hit["text"]}
                    for hit in hits
                ]
                if specialty == "material"
                else [],
            }
            if risk and specialty == risk[0]:
                advice.update(recommendation=risk[1], missing_fields=risk[2])
            message = {"role": "assistant", "content": json.dumps(advice)}
        return {"message": message, "engine_mode": "fake", "usage": {}, "elapsed_ms": 0}

    monkeypatch.setattr("quality.providers.AgnesClient.complete", complete)


def review_case(client, monkeypatch, kind="remediation", invalid=False, risk=None):
    identifier, owner, reviewer = opened_work_order(client, monkeypatch)
    body = {
        "expected_revision": 1,
        "review_kind": kind,
        "statement": "模拟商家提交新材料供人工复核",
        "as_of": "2025-07-01",
    }
    created = client.post(f"/api/work-orders/{identifier}/review-cases", headers=owner, json=body)
    assert created.status_code == 201, created.text
    path = "/api/cases/" + created.json()["case_id"]
    word = "REMEDIATION" if kind == "remediation" else "APPEAL"
    uploaded = client.post(
        path + "/materials",
        headers=owner,
        files={
            "file": (
                "review.html",
                f"<p>DEMO {word} evidence MODEL: K170 POWER: 1800W</p>".encode(),
                "text/html",
            ),
        },
    )
    assert uploaded.status_code == 201
    fake_review(monkeypatch, invalid, risk)
    client.post(path + "/submit", headers=owner)
    analyzed = client.post(path + "/analyze", headers=reviewer)
    assert analyzed.status_code == 200, analyzed.text
    return identifier, path, owner, reviewer, analyzed.json()


@pytest.mark.parametrize("branch", ["rules", "product", "material", "history"])
@pytest.mark.parametrize("recommendation,missing", [
    ("needs_info", ["整改措施有效性仍需核实"]),
    ("needs_info", []),
    ("out_of_scope", []),
    ("review_ready", ["整改措施有效性仍需核实"]),
])
def test_branch_risk_cannot_be_promoted_to_ready(
    client, monkeypatch, branch, recommendation, missing
):
    _, _, _, _, output = review_case(
        client, monkeypatch, risk=(branch, recommendation, missing)
    )
    assert output["recommendation"] == "needs_info"
    if missing:
        assert missing[0] in output["missing_fields"]
    else:
        assert "branch_review:" + branch in output["missing_fields"]


def test_remediation_review_closes_work_order_without_restoring_listing(client, monkeypatch):
    identifier, path, owner, reviewer, analysis = review_case(client, monkeypatch)
    assert analysis["recommendation"] == "review_ready"
    with client.app.state.sessions() as session:
        assert session.get(WorkOrder, identifier).status == "waiting_review"
    body = {
        "expected_revision": analysis["revision"],
        "analysis_run_id": analysis["run_id"],
        "decision": "approve",
        "note": "模拟复核确认关闭，未恢复上架",
    }
    approval = client.post(path + "/decision", headers=reviewer, json=body)
    assert approval.status_code == 200, approval.text
    action = path + f"/approvals/{approval.json()['id']}/execute"
    executed = client.post(
        action,
        headers=reviewer,
        json={"expected_revision": analysis["revision"], "analysis_run_id": analysis["run_id"]},
    )
    assert executed.status_code == 200, executed.text
    assert {effect["kind"] for effect in executed.json()["effects"]} == {"close_work_order"}
    repeated = client.post(
        action,
        headers=reviewer,
        json={"expected_revision": analysis["revision"], "analysis_run_id": analysis["run_id"]},
    )
    assert repeated.status_code == 200 and repeated.json()["idempotent"] is True
    assert repeated.json()["effects"] == executed.json()["effects"]
    with client.app.state.sessions() as session:
        assert session.get(WorkOrder, identifier).status == "closed"
        assert session.get(ProductListing, "sku-hit").status == "delisted"
    assert client.get("/api/work-orders", headers=headers(client, "merchant-b")).json() == []


def test_appeal_requires_new_material_basis_and_review_owner(client, monkeypatch):
    identifier, path, _, reviewer, analysis = review_case(
        client, monkeypatch, "appeal", invalid=True
    )
    assert analysis["recommendation"] == "needs_info"
    assert "review_evidence_basis" in analysis["missing_fields"]
    assert (
        client.post(
            f"/api/work-orders/{identifier}/review-cases",
            headers=headers(client, "merchant-b"),
            json={
                "expected_revision": 1,
                "review_kind": "appeal",
                "statement": "越权申诉",
                "as_of": "2025-07-01",
            },
        ).status_code
        == 404
    )
    assert (
        client.post(
            path + "/decision",
            headers=reviewer,
            json={
                "expected_revision": analysis["revision"],
                "analysis_run_id": analysis["run_id"],
                "decision": "approve",
                "note": "缺证据不能通过",
            },
        ).status_code
        == 409
    )


def test_rejected_review_escalates_work_order(client, monkeypatch):
    identifier, path, _, reviewer, analysis = review_case(client, monkeypatch)
    decided = client.post(
        path + "/decision",
        headers=reviewer,
        json={
            "expected_revision": analysis["revision"],
            "analysis_run_id": analysis["run_id"],
            "decision": "reject",
            "note": "模拟复核不通过，升级人工跟进",
        },
    )
    assert decided.status_code == 200, decided.text
    with client.app.state.sessions() as session:
        assert session.get(WorkOrder, identifier).status == "escalated"


def test_review_creation_rejects_stale_version_and_immutable_binding(client, monkeypatch):
    identifier, owner, _ = opened_work_order(client, monkeypatch)
    body = {
        "expected_revision": 2,
        "review_kind": "appeal",
        "statement": "模拟申诉",
        "as_of": "2025-07-01",
    }
    route = f"/api/work-orders/{identifier}/review-cases"
    assert client.post(route, headers=owner, json=body).status_code == 409
    body["expected_revision"] = 1
    created = client.post(route, headers=owner, json=body)
    assert created.status_code == 201
    assert client.post(route, headers=owner, json=body).status_code == 409
    path = "/api/cases/" + created.json()["case_id"]
    item = client.get(path, headers=owner).json()
    payload = {**item["payload"], "work_order_revision": 1}
    assert (
        client.put(
            path + "/payload", headers=owner, json={"expected_revision": 1, "payload": payload}
        ).status_code
        == 409
    )
    with client.app.state.sessions() as session:
        assert session.get(WorkOrder, identifier).revision == 2


def test_changed_original_event_blocks_approval_of_remediation(client, monkeypatch):
    _, path, _, reviewer, analysis = review_case(client, monkeypatch)
    with client.app.state.sessions.begin() as session:
        event = session.get(QualityEvent, "demo-recall-001")
        event.scope_json = {**event.scope_json, "production_end": "2025-12-31"}
    response = client.post(
        path + "/decision",
        headers=reviewer,
        json={
            "expected_revision": analysis["revision"],
            "analysis_run_id": analysis["run_id"],
            "decision": "approve",
            "note": "不得以旧事件来源批准复核",
        },
    )
    assert response.status_code == 409
    assert client.get(path + "/effects", headers=reviewer).json() == []


def test_changed_remediation_policy_invalidates_waiting_review(client, monkeypatch):
    _, path, _, reviewer, analysis = review_case(client, monkeypatch)
    with client.app.state.sessions.begin() as session:
        rule = session.get(RuleVersion, "demo-remediation-v1")
        rule.content = "模拟整改规则变更：需要重新检测与补充资料"
        rule.requirements = {"documents": ["test_report"]}
    response = client.post(
        path + "/decision",
        headers=reviewer,
        json={
            "expected_revision": analysis["revision"],
            "analysis_run_id": analysis["run_id"],
            "decision": "approve",
            "note": "旧规则不能批准当前复核",
        },
    )
    assert response.status_code == 409


@pytest.mark.parametrize("mutation", ["foreign_product", "production_date", "not_demo"])
def test_changed_batch_prevents_closure_and_writes_no_effect(client, monkeypatch, mutation):
    identifier, path, _, reviewer, analysis = review_case(client, monkeypatch)
    body = {"expected_revision": analysis["revision"], "analysis_run_id": analysis["run_id"]}
    approved = client.post(
        path + "/decision",
        headers=reviewer,
        json={**body, "decision": "approve", "note": "模拟工单批准"},
    )
    assert approved.status_code == 200, approved.text
    with client.app.state.sessions.begin() as session:
        order = session.get(WorkOrder, identifier)
        batch = session.get(Batch, order.batch_ids[0])
        if mutation == "foreign_product":
            batch.product_id = "sku-other-owner"
        elif mutation == "production_date":
            batch.production_date = date(2026, 1, 1)
        else:
            batch.is_demo = False
    response = client.post(
        path + f"/approvals/{approved.json()['id']}/execute", headers=reviewer, json=body
    )
    assert response.status_code == 409, response.text
    assert client.get(path + "/effects", headers=reviewer).json() == []
    with client.app.state.sessions() as session:
        assert session.get(WorkOrder, identifier).status != "closed"
