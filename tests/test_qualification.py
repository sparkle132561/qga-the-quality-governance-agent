import json
import threading

from conftest import headers


def create_qualification(client):
    client.post("/api/admin/import-demo", headers=headers(client, "admin"))
    owner = headers(client)
    case = client.post(
        "/api/cases",
        headers=owner,
        json={
            "title": "四分支准入协议验证",
            "workflow": "qualification",
            "payload": {"sku_id": "sku-hit", "model": "K170", "as_of": "2025-07-01"},
        },
    ).json()
    path = f"/api/cases/{case['id']}"
    for name, text in [
        ("certificate", "DEMO CERTIFICATE NUMBER: C2025-001 MODEL: K170"),
        ("label", "DEMO LABEL MODEL: K170 POWER: 1800W"),
        ("test_report", "DEMO TEST REPORT MODEL: K170 POWER: 1800W"),
    ]:
        response = client.post(
            path + "/materials",
            headers=owner,
            files={"file": (name + ".html", ("<p>" + text + "</p>").encode(), "text/html")},
        )
        assert response.status_code == 201, response.text
    return owner, path


def fake_model(monkeypatch, *, invalid_material=False, barrier=None):
    def complete(self, messages, tools):
        branch = json.loads(messages[1]["content"])["facts"]["specialty"]
        results = [message for message in messages if message["role"] == "tool"]
        if not results:
            if barrier:
                barrier.wait(timeout=15)
            return {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": branch + "-call",
                            "type": "function",
                            "function": {"name": "read_branch_evidence", "arguments": "{}"},
                        }
                    ],
                },
                "engine_mode": "fake",
                "usage": {},
                "elapsed_ms": 0,
            }
        hits = json.loads(results[-1]["content"])["data"]["hits"]
        assessments = []
        if branch == "material":
            for hit in hits:
                text = hit["text"]
                kind = (
                    "certificate"
                    if "CERTIFICATE" in text
                    else "label"
                    if "LABEL" in text
                    else "test_report"
                )
                assessments.append(
                    {
                        "source_id": "invented" if invalid_material else hit["id"],
                        "document_type": kind,
                        "quote": text,
                    }
                )
        advice = {
            "recommendation": "review_ready",
            "explanation": "协议替身，不是模型判断质量",
            "evidence_ids": [hit["id"] for hit in hits],
            "missing_fields": [],
            "material_assessments": assessments,
        }
        return {
            "message": {"role": "assistant", "content": json.dumps(advice)},
            "engine_mode": "fake",
            "usage": {},
            "elapsed_ms": 0,
        }

    monkeypatch.setattr("quality.providers.AgnesClient.complete", complete)
    return complete


def test_missing_key_preserves_four_branches_and_requires_information(client):
    owner, path = create_qualification(client)
    client.post(path + "/submit", headers=owner)
    response = client.post(path + "/analyze", headers=headers(client, "reviewer"))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["engine_mode"] == "partial"
    assert set(result["branches"]) == {"rules", "product", "material", "history"}
    assert result["branches"]["material"]["error_code"] == "missing_agnes_key"
    assert result["recommendation"] == "needs_info"
    assert client.get(path, headers=owner).json()["status"] == "needs_info"


def test_parallel_fake_evidence_only_enters_human_queue(client, monkeypatch):
    owner, path = create_qualification(client)
    fake_model(monkeypatch, barrier=threading.Barrier(3))
    client.post(path + "/submit", headers=owner)
    response = client.post(path + "/analyze", headers=headers(client, "reviewer"))
    assert response.status_code == 200, response.text
    output = response.json()
    assert output["engine_mode"] == "fake"
    assert output["recommendation"] == "review_ready"
    assert output["missing_fields"] == [] and output["conflicts"] == []
    assert client.get(path, headers=owner).json()["status"] == "waiting_review"
    branches = [output["branches"][name] for name in ("rules", "product", "material")]
    assert max(branch["started"] for branch in branches) < min(
        branch["finished"] for branch in branches
    )
    assert all(
        [event["event"] for event in branch["trace"]] == ["model", "tool", "model"]
        for branch in branches
    )
    assert client.post(path + "/approve", headers=headers(client, "reviewer")).status_code == 404


def test_model_cannot_bypass_missing_material_basis(client, monkeypatch):
    owner, path = create_qualification(client)
    fake_model(monkeypatch, invalid_material=True)
    client.post(path + "/submit", headers=owner)
    output = client.post(path + "/analyze", headers=headers(client, "reviewer")).json()
    assert output["recommendation"] == "needs_info"
    assert "unverified_material_classification" in output["missing_fields"]
    assert "document:certificate" in output["missing_fields"]


def test_payload_edit_is_owned_versioned_and_locked_after_submit(client):
    owner = headers(client)
    case = client.post(
        "/api/cases", headers=owner, json={"title": "槽位补充", "workflow": "qualification"}
    ).json()
    path = f"/api/cases/{case['id']}"
    body = {"expected_revision": 1, "payload": {"sku_id": "sku-hit", "as_of": "2025-07-01"}}
    assert (
        client.put(path + "/payload", headers=headers(client, "merchant-b"), json=body).status_code
        == 404
    )
    updated = client.put(path + "/payload", headers=owner, json=body)
    assert updated.status_code == 200 and updated.json()["revision"] == 2
    assert client.put(path + "/payload", headers=owner, json=body).status_code == 409
    client.post(path + "/submit", headers=owner)
    assert (
        client.put(
            path + "/payload", headers=owner, json={**body, "expected_revision": 2}
        ).status_code
        == 409
    )


def test_failed_branch_does_not_discard_other_evidence(client, monkeypatch):
    owner, path = create_qualification(client)
    normal = fake_model(monkeypatch)

    def fail_product(self, messages, tools):
        branch = json.loads(messages[1]["content"])["facts"]["specialty"]
        if branch == "product":
            raise RuntimeError("protocol crash")
        return normal(self, messages, tools)

    monkeypatch.setattr("quality.providers.AgnesClient.complete", fail_product)
    client.post(path + "/submit", headers=owner)
    response = client.post(path + "/analyze", headers=headers(client, "reviewer"))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["branches"]["product"]["status"] == "failed"
    assert result["branches"]["product"]["engine_mode"] == "unknown"
    assert result["branches"]["rules"]["evidence"]
    assert result["branches"]["material"]["evidence"]
    assert result["recommendation"] == "needs_info"


def test_source_changed_during_analysis_invalidates_and_returns_to_submitted(client, monkeypatch):
    from quality.models import RuleVersion

    owner, path = create_qualification(client)
    normal = fake_model(monkeypatch)
    changed = threading.Event()

    def mutate_rule(self, messages, tools):
        branch = json.loads(messages[1]["content"])["facts"]["specialty"]
        if branch == "rules" and not changed.is_set():
            with client.app.state.sessions.begin() as session:
                session.get(RuleVersion, "demo-qualification-v2").content += " 新来源快照"
            changed.set()
        return normal(self, messages, tools)

    monkeypatch.setattr("quality.providers.AgnesClient.complete", mutate_rule)
    client.post(path + "/submit", headers=owner)
    response = client.post(path + "/analyze", headers=headers(client, "reviewer"))
    assert response.status_code == 409, response.text
    assert client.get(path, headers=owner).json()["status"] == "submitted"
    assert client.get(path, headers=owner).json()["latest_analysis"] is None


def test_material_model_conflict_cannot_enter_queue(client, monkeypatch):
    owner, path = create_qualification(client)
    client.post(
        path + "/materials",
        headers=owner,
        files={
            "file": ("other.html", b"<p>MODEL: K180 POWER: 1500W</p>", "text/html"),
        },
    )
    fake_model(monkeypatch)
    client.post(path + "/submit", headers=owner)
    response = client.post(path + "/analyze", headers=headers(client, "reviewer"))
    assert response.status_code == 200, response.text
    assert response.json()["recommendation"] == "needs_info"
    assert any(conflict["field"] == "model" for conflict in response.json()["conflicts"])


def test_reject_proposal_still_requires_human_decision(client, monkeypatch):
    owner, path = create_qualification(client)
    normal = fake_model(monkeypatch)

    def reject_rule(self, messages, tools):
        response = normal(self, messages, tools)
        branch = json.loads(messages[1]["content"])["facts"]["specialty"]
        if branch == "rules" and response["message"].get("content"):
            advice = json.loads(response["message"]["content"])
            advice["recommendation"] = "out_of_scope"
            response["message"]["content"] = json.dumps(advice)
        return response

    monkeypatch.setattr("quality.providers.AgnesClient.complete", reject_rule)
    client.post(path + "/submit", headers=owner)
    response = client.post(path + "/analyze", headers=headers(client, "reviewer"))
    assert response.status_code == 200, response.text
    assert response.json()["recommendation"] == "reject_proposed"
    assert client.get(path, headers=owner).json()["status"] == "waiting_review"


def test_same_paragraph_certificate_conflict_cannot_enter_queue(client, monkeypatch):
    owner, path = create_qualification(client)
    response = client.post(
        path + "/materials",
        headers=owner,
        files={
            "file": (
                "conflicting-certificate.html",
                b"<p>CERTIFICATE NUMBER: C2025-001 CERTIFICATE NUMBER: C2025-002 MODEL: K170</p>",
                "text/html",
            ),
        },
    )
    assert response.status_code == 201
    fake_model(monkeypatch)
    client.post(path + "/submit", headers=owner)
    result = client.post(path + "/analyze", headers=headers(client, "reviewer")).json()
    assert result["recommendation"] == "needs_info"
    assert any(item["field"] == "certificate_number" for item in result["conflicts"])


def test_repeated_same_sku_history_stays_bounded_and_nonrecursive(client, monkeypatch):
    fake_model(monkeypatch)
    for _ in range(7):
        owner, path = create_qualification(client)
        client.post(path + "/submit", headers=owner)
        response = client.post(path + "/analyze", headers=headers(client, "reviewer"))
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["recommendation"] == "review_ready", result["missing_fields"]
        for hit in result["branches"]["history"]["evidence"]:
            history = json.loads(hit["text"])
            digest = history["analysis_digest"]
            assert "branches" not in digest and "trace" not in digest
            assert "analysis" not in digest and "analysis_digest" not in digest
            assert len(hit["text"]) < 6000
        assert len(json.dumps(result)) < 60000
