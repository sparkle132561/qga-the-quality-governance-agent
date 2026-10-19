from conftest import headers


def test_case_ownership_and_role_permissions(client):
    merchant = headers(client)
    case = client.post(
        "/api/cases", headers=merchant, json={"title": "模拟水壶准入", "workflow": "qualification"}
    ).json()
    assert case["status"] == "draft"
    assert case["is_demo"] is True
    other = headers(client, "merchant-b")
    assert client.get(f"/api/cases/{case['id']}", headers=other).status_code == 404
    assert client.get("/api/cases", headers=other).json() == []
    assert (
        client.post(
            "/api/cases",
            headers=merchant,
            json={"title": "越权", "workflow": "incident", "merchant_id": "merchant-b"},
        ).status_code
        == 403
    )
    assert client.post(f"/api/cases/{case['id']}/analyze", headers=merchant).status_code == 403
    assert client.get("/api/cases").status_code == 401


def test_submit_parallel_stub_and_audit(client):
    merchant = headers(client)
    reviewer = headers(client, "reviewer")
    case = client.post(
        "/api/cases", headers=merchant, json={"title": "整改骨架分析", "workflow": "remediation"}
    ).json()
    path = f"/api/cases/{case['id']}"
    assert client.post(path + "/analyze", headers=reviewer).status_code == 409
    assert client.post(path + "/submit", headers=merchant).status_code == 200
    assert client.post(path + "/submit", headers=merchant).status_code == 409
    result = client.post(path + "/analyze", headers=reviewer)
    assert result.status_code == 200
    output = result.json()
    assert output["engine_mode"] == "stub"
    assert set(output["branches"]) == {"rules", "product", "material"}
    assert all(branch["status"] == "stub" for branch in output["branches"].values())
    assert client.get(path, headers=merchant).json()["status"] == "needs_info"
    assert [item["action"] for item in client.get(path + "/audit", headers=merchant).json()] == [
        "create",
        "submit",
        "analysis_started",
        "analyze",
    ]


def test_invalid_credentials_and_blank_title(client):
    assert (
        client.post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code
        == 401
    )
    assert (
        client.post(
            "/api/auth/login", json={"username": "admin", "password": "错误密码"}
        ).status_code
        == 401
    )
    assert client.get("/api/cases", headers={"Authorization": "Bearer forged"}).status_code == 401
    assert (
        client.post(
            "/api/cases",
            headers=headers(client),
            json={"title": "   ", "workflow": "qualification"},
        ).status_code
        == 422
    )


def test_failed_analysis_returns_to_submitted(client, monkeypatch):
    owner, reviewer = headers(client), headers(client, "reviewer")
    case = client.post(
        "/api/cases", headers=owner, json={"title": "故障恢复", "workflow": "remediation"}
    ).json()
    path = f"/api/cases/{case['id']}"
    client.post(path + "/submit", headers=owner)

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(client.app.state.graph, "invoke", fail)
    assert client.post(path + "/analyze", headers=reviewer).status_code == 503
    assert client.get(path, headers=owner).json()["status"] == "submitted"
