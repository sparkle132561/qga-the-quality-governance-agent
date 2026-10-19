from concurrent.futures import ThreadPoolExecutor

from conftest import headers


def test_demo_import_is_admin_only_and_idempotent(client):
    assert client.post("/api/admin/import-demo", headers=headers(client)).status_code == 403
    admin = headers(client, "admin")
    first = client.post("/api/admin/import-demo", headers=admin)
    assert first.status_code == 200
    second = client.post("/api/admin/import-demo", headers=admin).json()
    assert all(count == 0 for count in second["added"].values())
    own = client.get("/api/products", headers=headers(client)).json()
    assert len(own) == 3
    assert all(product["merchant_id"] == "merchant-a" and product["is_demo"] for product in own)
    assert all(len(product["batches"]) == 1 for product in own)
    other = client.get("/api/products", headers=headers(client, "merchant-b")).json()
    assert [product["id"] for product in other] == ["sku-other-owner"]


def test_rule_date_boundary_and_public_demo_source_separation(client):
    admin = headers(client, "admin")
    client.post("/api/admin/import-demo", headers=admin)
    old = client.get(
        "/api/rules?as_of=2025-06-30&rule_key=DEMO-QUALIFICATION", headers=admin
    ).json()
    new = client.get(
        "/api/rules?as_of=2025-07-01&rule_key=DEMO-QUALIFICATION", headers=admin
    ).json()
    assert [rule["version"] for rule in old] == ["1"]
    assert [rule["version"] for rule in new] == ["2"]
    assert "test_report" not in old[0]["requirements"]["documents"]
    assert "test_report" in new[0]["requirements"]["documents"]
    future = client.get("/api/rules?as_of=2024-06-30&rule_key=CNCA-C07-01", headers=admin).json()
    assert future == []
    official = client.get("/api/rules?as_of=2024-07-01&rule_key=CNCA-C07-01", headers=admin).json()
    assert official[0]["is_demo"] is False
    assert official[0]["requirements"] == {}
    sources = client.get("/api/sources", headers=admin).json()
    assert all(
        not source["redistribution_allowed"]
        for source in sources
        if source["source_kind"].startswith("official")
    )
    public = next(
        event for event in client.get("/api/events", headers=admin).json() if not event["is_demo"]
    )
    assert public["scope"]["date_precision"] == "month"
    assert public["scope"]["scope_review_required"] is True


def test_concurrent_first_imports_return_success_without_duplicates(client):
    admin = headers(client, "admin")
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(lambda _: client.post("/api/admin/import-demo", headers=admin), range(4))
        )
    assert all(response.status_code == 200 for response in responses)
    assert sum(sum(response.json()["added"].values()) for response in responses) == 22
    assert len(client.get("/api/sources", headers=admin).json()) == 3
    assert len(client.get("/api/products", headers=admin).json()) == 4
