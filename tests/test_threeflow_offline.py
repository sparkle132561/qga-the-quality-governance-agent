"""三条业务链路的串联协议回归；所有材料与模型响应均为模拟。"""

from conftest import headers
from test_impact import analyze_incident, fake_impact
from test_qualification import fake_model
from test_remediation import fake_review


def test_qualification_impact_and_remediation_share_the_same_product(client, monkeypatch):
    owner, reviewer = headers(client), headers(client, "reviewer")
    response = client.post("/api/admin/import-demo", headers=headers(client, "admin"))
    assert response.status_code == 200, response.text

    created = client.post(
        "/api/cases",
        headers=owner,
        json={
            "title": "离线三流程串联",
            "workflow": "qualification",
            "payload": {"sku_id": "sku-hit", "as_of": "2025-07-01"},
        },
    )
    assert created.status_code == 201, created.text
    qualification = "/api/cases/" + created.json()["id"]

    def upload(path, name, text):
        result = client.post(
            path + "/materials",
            headers=owner,
            files={"file": (name + ".html", ("<p>" + text + "</p>").encode(), "text/html")},
        )
        assert result.status_code == 201, result.text

    def analyze(path):
        submitted = client.post(path + "/submit", headers=owner)
        assert submitted.status_code == 200, submitted.text
        result = client.post(path + "/analyze", headers=reviewer)
        assert result.status_code == 200, result.text
        analysis = result.json()
        assert analysis["engine_mode"] == "fake"
        assert set(analysis["branches"]) == {"rules", "product", "material", "history"}
        assert not any(branch.get("error_code") for branch in analysis["branches"].values())
        return analysis

    def approve_and_execute(path, analysis):
        assert analysis["recommendation"] == "review_ready", analysis
        version = {"expected_revision": analysis["revision"], "analysis_run_id": analysis["run_id"]}
        decision = {**version, "decision": "approve", "note": "仅验证隔离模拟协议"}
        assert client.post(path + "/decision", headers=owner, json=decision).status_code == 403
        approved = client.post(path + "/decision", headers=reviewer, json=decision)
        assert approved.status_code == 200, approved.text
        action = path + f"/approvals/{approved.json()['id']}/execute"
        executed = client.post(action, headers=reviewer, json=version)
        assert executed.status_code == 200, executed.text
        repeated = client.post(action, headers=reviewer, json=version)
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["idempotent"] is True
        assert repeated.json()["effects"] == executed.json()["effects"]
        return executed.json()["effects"]

    upload(qualification, "certificate", "DEMO CERTIFICATE NUMBER: C2025-001 MODEL: K170")
    upload(qualification, "label", "DEMO LABEL MODEL: K170 POWER: 1800W")
    fake_model(monkeypatch)
    missing = analyze(qualification)
    assert missing["recommendation"] == "needs_info"
    assert "document:test_report" in missing["missing_fields"]
    assert client.get(qualification + "/effects", headers=owner).json() == []

    upload(qualification, "test_report", "DEMO TEST REPORT MODEL: K170 POWER: 1800W")
    ready = analyze(qualification)
    assert ready["revision"] > missing["revision"]
    listing = approve_and_execute(qualification, ready)
    assert [(row["kind"], row["target_id"]) for row in listing] == [("simulate_listing", "sku-hit")]

    fake_impact(monkeypatch)
    incident, _, impact = analyze_incident(client)
    assert impact["impact"]["affected_product_ids"] == ["sku-hit"]
    assert impact["impact"]["excluded_product_ids"] == ["sku-date-out", "sku-near-model"]
    assert impact["impact"]["unknown_product_ids"] == []
    disposed = approve_and_execute(incident, impact)
    assert [row["target_id"] for row in disposed if row["kind"] == "simulate_delist"] == ["sku-hit"]
    work_orders = [row["target_id"] for row in disposed if row["kind"] == "create_work_order"]
    assert len(work_orders) == 1
    order_id = work_orders[0]
    orders = client.get("/api/work-orders", headers=owner).json()
    order = next(row for row in orders if row["id"] == order_id)
    assert order["status"] == "open"
    other = headers(client, "merchant-b")
    assert client.get(incident, headers=other).status_code == 404
    assert client.get("/api/work-orders", headers=other).json() == []

    review = client.post(
        f"/api/work-orders/{order_id}/review-cases",
        headers=owner,
        json={
            "expected_revision": order["revision"],
            "review_kind": "remediation",
            "statement": "模拟整改新材料，待人工复核",
            "as_of": "2025-07-01",
        },
    )
    assert review.status_code == 201, review.text
    remediation = "/api/cases/" + review.json()["case_id"]
    upload(remediation, "remediation", "DEMO REMEDIATION MODEL: K170 POWER: 1800W")
    fake_review(monkeypatch)
    closing = approve_and_execute(remediation, analyze(remediation))
    assert [(row["kind"], row["target_id"]) for row in closing] == [("close_work_order", order_id)]
    closed = next(
        row for row in client.get("/api/work-orders", headers=owner).json() if row["id"] == order_id
    )
    assert closed["status"] == "closed"
