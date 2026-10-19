import json
from datetime import date
from types import SimpleNamespace

import pytest
from conftest import headers
from quality.impact import match_batch
from quality.models import QualityEvent

SCOPE = {
    "brand": "演示品牌",
    "models": ["K170"],
    "production_start": "2025-01-01",
    "production_end": "2025-04-30",
    "date_precision": "day",
}


def fake_impact(monkeypatch, *, invented=False, risk=None):
    def complete(self, messages, tools):
        tool_messages = [message for message in messages if message["role"] == "tool"]
        if not tool_messages:
            return {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "read",
                            "type": "function",
                            "function": {"name": "read_branch_evidence", "arguments": "{}"},
                        }
                    ],
                },
                "engine_mode": "fake",
                "usage": {},
                "elapsed_ms": 0,
            }
        hits = json.loads(tool_messages[-1]["content"])["data"]["hits"]
        advice = {
            "recommendation": "review_ready",
            "explanation": "协议替身，只解释确定性匹配",
            "evidence_ids": ["invented"] if invented else [hit["id"] for hit in hits],
            "missing_fields": [],
        }
        specialty = json.loads(messages[1]["content"])["facts"]["specialty"]
        if risk and specialty == risk[0]:
            advice.update(recommendation=risk[1], missing_fields=risk[2])
        return {
            "message": {"role": "assistant", "content": json.dumps(advice)},
            "engine_mode": "fake",
            "usage": {},
            "elapsed_ms": 0,
        }

    monkeypatch.setattr("quality.providers.AgnesClient.complete", complete)
    return complete


@pytest.mark.parametrize("branch", ["rules", "product", "material"])
@pytest.mark.parametrize("recommendation,missing", [
    ("needs_info", ["供货来源仍需核实"]),
    ("needs_info", []),
    ("out_of_scope", []),
    ("review_ready", ["供货来源仍需核实"]),
])
def test_branch_risk_cannot_be_promoted_to_ready(
    client, monkeypatch, branch, recommendation, missing
):
    client.post("/api/admin/import-demo", headers=headers(client, "admin"))
    fake_impact(monkeypatch, risk=(branch, recommendation, missing))
    _, _, output = analyze_incident(client)
    assert output["recommendation"] == "needs_info"
    if missing:
        assert missing[0] in output["missing_fields"]
    else:
        assert "branch_review:" + branch in output["missing_fields"]


def analyze_incident(client, username="merchant-a"):
    owner = headers(client, username)
    case = client.post(
        "/api/cases",
        headers=owner,
        json={
            "title": "事件影响机制验证",
            "workflow": "incident",
            "payload": {"event_id": "demo-recall-001", "as_of": "2025-07-01"},
        },
    ).json()
    path = f"/api/cases/{case['id']}"
    client.post(path + "/submit", headers=owner)
    response = client.post(path + "/analyze", headers=headers(client, "reviewer"))
    assert response.status_code == 200, response.text
    return path, owner, response.json()


def test_incident_four_branches_exact_scope_orders_paths_and_owner(client, monkeypatch):
    client.post("/api/admin/import-demo", headers=headers(client, "admin"))
    fake_impact(monkeypatch)
    path, owner, output = analyze_incident(client)
    impact = output["impact"]
    assert output["engine_mode"] == "fake"
    assert output["recommendation"] == "review_ready"
    assert impact["affected_product_ids"] == ["sku-hit"]
    assert impact["excluded_product_ids"] == ["sku-date-out", "sku-near-model"]
    assert impact["unknown_product_ids"] == []
    assert impact["affected_order_ids"] == ["demo-order-hit"]
    assert "sku-other-owner" not in json.dumps(impact)
    hit = next(row for row in impact["findings"] if row["status"] == "hit")
    assert len(hit["graph_edges"]) == 3
    assert client.get(path, headers=owner).json()["status"] == "waiting_review"
    assert client.get(path, headers=headers(client, "merchant-b")).status_code == 404
    _, _, other = analyze_incident(client, "merchant-b")
    assert other["impact"]["affected_product_ids"] == ["sku-other-owner"]
    assert "sku-hit" not in json.dumps(other["impact"])


def test_unknown_scope_is_not_all_matches_or_zero_impact(client, monkeypatch):
    client.post("/api/admin/import-demo", headers=headers(client, "admin"))
    with client.app.state.sessions.begin() as session:
        event = session.get(QualityEvent, "demo-recall-001")
        event.scope_json = {**event.scope_json, "models": []}
    fake_impact(monkeypatch)
    _, _, output = analyze_incident(client)
    assert output["recommendation"] == "needs_info"
    assert output["impact"]["affected_product_ids"] == []
    assert len(output["impact"]["unknown_product_ids"]) == 2
    assert output["impact"]["excluded_product_ids"] == ["sku-date-out"]
    assert "candidate_scope_unknown" in output["missing_fields"]


def test_model_cannot_invent_in_scope_ids(client, monkeypatch):
    client.post("/api/admin/import-demo", headers=headers(client, "admin"))
    fake_impact(monkeypatch, invented=True)
    _, _, output = analyze_incident(client)
    assert output["recommendation"] == "needs_info"
    assert output["impact"]["affected_product_ids"] == ["sku-hit"]
    assert "invented" not in json.dumps(output["impact"])


def test_boundaries_month_unknown_serial_and_near_model():
    product = SimpleNamespace(brand="演示品牌", model="K170")
    batch = SimpleNamespace(number="B01", production_date=date(2025, 4, 30))
    assert match_batch(SCOPE, product, batch)[0] == "hit"
    batch.production_date = date(2025, 5, 1)
    assert match_batch(SCOPE, product, batch)[0] == "excluded"
    batch.production_date = None
    assert match_batch(SCOPE, product, batch)[0] == "unknown"
    batch.production_date = date(2025, 4, 15)
    scope = {
        **SCOPE,
        "date_precision": "month",
        "production_start": "2025-01",
        "production_end": "2025-04",
    }
    assert match_batch(scope, product, batch)[0] == "hit"
    assert (
        match_batch(
            {
                **scope,
                "scope_review_required": True,
                "serial_filter_status": "requires_interpretation",
            },
            product,
            batch,
        )[0]
        == "unknown"
    )
    product.model = "K170S"
    assert match_batch(scope, product, batch)[0] == "excluded"
    product.brand = "其他品牌"
    status, reasons = match_batch(scope, product, batch)
    assert status == "excluded" and reasons == ["brand_out_of_scope"]


def test_source_change_invalidates_impact_and_can_retry(client, monkeypatch):
    client.post("/api/admin/import-demo", headers=headers(client, "admin"))
    fake_impact(monkeypatch)
    from quality.providers import AgnesClient

    original = AgnesClient.complete
    changed = []

    def mutate(self, messages, tools):
        if not changed:
            with client.app.state.sessions.begin() as session:
                event = session.get(QualityEvent, "demo-recall-001")
                event.scope_json = {**event.scope_json, "production_end": "2025-03-31"}
            changed.append(1)
        return original(self, messages, tools)

    monkeypatch.setattr(AgnesClient, "complete", mutate)
    owner = headers(client)
    case = client.post(
        "/api/cases",
        headers=owner,
        json={
            "title": "范围变化",
            "workflow": "incident",
            "payload": {"event_id": "demo-recall-001", "as_of": "2025-07-01"},
        },
    ).json()
    path = f"/api/cases/{case['id']}"
    client.post(path + "/submit", headers=owner)
    assert client.post(path + "/analyze", headers=headers(client, "reviewer")).status_code == 409
    assert client.get(path, headers=owner).json()["status"] == "submitted"


def test_dynamic_rule_chunk_deleted_during_analysis_invalidates_result(client, monkeypatch):
    from quality.models import EvidenceChunk
    from quality.retrieval import index_units
    from sqlalchemy import delete

    client.post("/api/admin/import-demo", headers=headers(client, "admin"))
    with client.app.state.sessions.begin() as session:
        index_units(
            session,
            "rule:demo-qualification-v2",
            [{"text": "检测报告规则证据", "location": {"kind": "test"}}],
            "a" * 64,
            True,
            rule_id="demo-qualification-v2",
        )
    normal = fake_impact(monkeypatch)
    removed = []

    def search_then_remove(self, messages, tools):
        specialty = json.loads(messages[1]["content"])["facts"]["specialty"]
        if specialty == "rules":
            tool_messages = [message for message in messages if message["role"] == "tool"]
            if not tool_messages:
                response = normal(self, messages, tools)
                response["message"]["tool_calls"][0]["function"] = {
                    "name": "search_rule_evidence",
                    "arguments": '{"query":"检测报告"}',
                }
                return response
            if not removed:
                hits = json.loads(tool_messages[-1]["content"])["data"]["hits"]
                assert hits
                with client.app.state.sessions.begin() as session:
                    session.execute(delete(EvidenceChunk).where(EvidenceChunk.id == hits[0]["id"]))
                removed.append(1)
        return normal(self, messages, tools)

    monkeypatch.setattr("quality.providers.AgnesClient.complete", search_then_remove)
    owner = headers(client)
    case = client.post(
        "/api/cases",
        headers=owner,
        json={
            "title": "动态引用失效",
            "workflow": "incident",
            "payload": {"event_id": "demo-recall-001", "as_of": "2025-07-01"},
        },
    ).json()
    path = f"/api/cases/{case['id']}"
    client.post(path + "/submit", headers=owner)
    response = client.post(path + "/analyze", headers=headers(client, "reviewer"))
    assert response.status_code == 409, response.text
    assert removed == [1]
    assert client.get(path, headers=owner).json()["status"] == "submitted"


def test_single_sku_scope_does_not_load_unrelated_merchant_graph(client, monkeypatch):
    from quality.models import Product

    client.post("/api/admin/import-demo", headers=headers(client, "admin"))
    with client.app.state.sessions.begin() as session:
        session.add_all(
            [
                Product(
                    id=f"unrelated-{index}",
                    merchant_id="merchant-a",
                    supplier_id="supplier-a",
                    brand="其他品牌",
                    model="OTHER",
                    category="electric_kettle",
                    is_demo=True,
                )
                for index in range(201)
            ]
        )
    fake_impact(monkeypatch)
    owner = headers(client)
    case = client.post(
        "/api/cases",
        headers=owner,
        json={
            "title": "单SKU范围",
            "workflow": "incident",
            "payload": {"event_id": "demo-recall-001", "as_of": "2025-07-01", "sku_id": "sku-hit"},
        },
    ).json()
    path = f"/api/cases/{case['id']}"
    client.post(path + "/submit", headers=owner)
    response = client.post(path + "/analyze", headers=headers(client, "reviewer"))
    assert response.status_code == 200, response.text
    assert response.json()["recommendation"] == "review_ready"
    assert response.json()["impact"]["affected_product_ids"] == ["sku-hit"]
    assert len(response.json()["impact"]["findings"]) == 1


def test_known_date_or_batch_exclusion_precedes_missing_identity():
    product = SimpleNamespace(brand="演示品牌", model="K170")
    batch = SimpleNamespace(number="B02", production_date=date(2025, 6, 1))
    assert match_batch({**SCOPE, "brand": None, "models": []}, product, batch)[0] == "excluded"
    assert (
        match_batch({"brand": None, "models": [], "batches": ["B01"]}, product, batch)[0]
        == "excluded"
    )


def test_inconsistent_supplier_owner_never_appears_in_merchant_impact(client, monkeypatch):
    from quality.models import Product

    client.post("/api/admin/import-demo", headers=headers(client, "admin"))
    with client.app.state.sessions.begin() as session:
        session.get(Product, "sku-hit").supplier_id = "supplier-b"
    fake_impact(monkeypatch)
    _, _, output = analyze_incident(client)
    assert output["recommendation"] == "needs_info"
    assert "supplier_owner_inconsistent" in output["missing_fields"]
    assert "supplier-b" not in json.dumps(output["impact"])


def test_missing_batch_does_not_hide_known_product_exclusion(client, monkeypatch):
    from quality.models import Product

    client.post("/api/admin/import-demo", headers=headers(client, "admin"))
    with client.app.state.sessions.begin() as session:
        session.add(
            Product(
                id="unrelated-no-batch",
                merchant_id="merchant-a",
                supplier_id="supplier-a",
                brand="其他品牌",
                model="OTHER",
                category="electric_kettle",
                is_demo=True,
            )
        )
    fake_impact(monkeypatch)
    _, _, output = analyze_incident(client)
    assert output["recommendation"] == "review_ready"
    row = next(
        row for row in output["impact"]["findings"] if row["product_id"] == "unrelated-no-batch"
    )
    assert row["status"] == "excluded" and row["reasons"] == ["brand_out_of_scope"]
    assert "unrelated-no-batch" not in output["impact"]["unknown_product_ids"]
