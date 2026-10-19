from conftest import headers
from quality.models import Product


def test_platform_owner_filters_apply_before_limit_and_merchants_cannot_widen(client):
    admin, reviewer = headers(client, "admin"), headers(client, "reviewer")
    client.post("/api/admin/import-demo", headers=admin)
    with client.app.state.sessions.begin() as session:
        session.add_all(
            [
                Product(
                    id=f"a-{index}",
                    merchant_id="merchant-a",
                    supplier_id="supplier-a",
                    brand="演示",
                    model="K170",
                    category="electric_kettle",
                )
                for index in range(5)
            ]
        )
    result = client.get("/api/products?limit=1&merchant_id=merchant-b", headers=reviewer)
    assert result.status_code == 200 and len(result.json()) == 1
    assert result.json()[0]["merchant_id"] == "merchant-b"
    merchant = headers(client)
    assert client.get("/api/products?merchant_id=merchant-b", headers=merchant).status_code == 403
    created_b = client.post(
        "/api/cases",
        headers=headers(client, "merchant-b"),
        json={"title": "商家乙案件", "workflow": "qualification"},
    ).json()
    for index in range(3):
        client.post(
            "/api/cases",
            headers=merchant,
            json={"title": f"商家甲案件{index}", "workflow": "qualification"},
        )
    rows = client.get("/api/cases?limit=1&merchant_id=merchant-b", headers=reviewer).json()
    assert [row["id"] for row in rows] == [created_b["id"]]
    assert client.get("/api/cases?merchant_id=merchant-b", headers=merchant).status_code == 403
