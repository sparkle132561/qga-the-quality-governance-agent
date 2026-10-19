import json

import pytest
from conftest import headers
from quality.knowledge_graph import extract_graph
from quality.retrieval import index_units

TEXT = "供应商甲为K170供货，K170包含批次B01。"
BODY = {
    "entities": [
        {"key": "supplier", "kind": "supplier", "name": "供应商甲", "qualifiers": {}},
        {"key": "product", "kind": "product", "name": "K170", "qualifiers": {}},
        {"key": "batch", "kind": "batch", "name": "B01", "qualifiers": {}},
    ],
    "relations": [
        {
            "subject": "supplier",
            "predicate": "supplies",
            "object": "product",
            "quote": "供应商甲为K170供货",
        },
        {
            "subject": "product",
            "predicate": "has_batch",
            "object": "batch",
            "quote": "K170包含批次B01",
        },
    ],
}


def test_graph_uses_selected_source_lines_without_rewriting_quote():
    from types import SimpleNamespace

    body = {
        "entities": BODY["entities"],
        "relations": [
            {
                "subject": "supplier",
                "predicate": "supplies",
                "object": "product",
                "evidence": {"line_start": 1, "line_end": 1},
            }
        ],
    }
    result, _ = extract_graph(SimpleNamespace(complete=lambda *args: model_response(body)), TEXT)
    assert result["relations"][0]["quote"] == TEXT


def model_response(body=BODY):
    return {
        "message": {"role": "assistant", "content": json.dumps(body)},
        "engine_mode": "fake",
        "elapsed_ms": 0,
        "usage": {},
    }


@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("already_cleaned", [False, True])
def test_confirmation_persists_cleaned_future_supply_relations(client, mixed, already_cleaned):
    from quality.knowledge_graph import ExtractedGraph, validate_graph
    from quality.models import GraphExtraction

    text = "供应商甲将为K170供货，K170包含批次B01。"
    owner, reviewer = headers(client), headers(client, "reviewer")
    case, chunk_id = make_chunk(client, owner, text)
    body = json.loads(json.dumps(BODY))
    body["relations"][0]["quote"] = "供应商甲将为K170供货"
    if not mixed:
        body["relations"] = body["relations"][:1]
    if already_cleaned:
        body = validate_graph(ExtractedGraph.model_validate(body), text)
    with client.app.state.sessions.begin() as session:
        row = GraphExtraction(
            chunk_id=chunk_id, source_sha256="a" * 64, case_id=case["id"],
            case_revision=case["revision"], body=body, location={}, is_demo=True,
            engine_mode="fake", created_by="merchant-a",
        )
        session.add(row)
        session.flush()
        identifier = row.id
    response = client.post(
        f"/api/graph/extractions/{identifier}/review", headers=reviewer,
        json={"decision": "confirmed", "as_of": "2025-07-01", "note": "核验清理后的关系"},
    )
    assert response.status_code == 200, response.text
    cleaned = response.json()["body"]
    assert [r["predicate"] for r in cleaned["relations"]] == (["has_batch"] if mixed else [])
    assert cleaned["dropped_relations"][0]["reason"] == "supply_not_reported_for_object"
    with client.app.state.sessions() as session:
        assert session.get(GraphExtraction, identifier).body == cleaned
    graph = client.get(
        f"/api/graph?as_of=2025-07-01&case_id={case['id']}", headers=owner
    ).json()
    assert all(edge["predicate"] != "supplies" for edge in graph["edges"])


def test_graph_accepts_complete_json_fence_without_relaxing_origin_checks():
    from types import SimpleNamespace

    response = model_response()
    response["message"]["content"] = "```json\n" + json.dumps(BODY) + "\n```"
    body, _ = extract_graph(SimpleNamespace(complete=lambda *args: response), TEXT)
    assert body["relations"] == BODY["relations"]


def test_fenced_graph_still_rejects_invalid_key_and_unsupported_quote():
    import copy
    from types import SimpleNamespace

    for kind in ("key", "quote"):
        body = copy.deepcopy(BODY)
        if kind == "key":
            body["entities"][0]["key"] = "供应商甲"
        else:
            body["relations"][0]["quote"] = "不存在于原文的供货关系"
        response = model_response()
        response["message"]["content"] = "```json\n" + json.dumps(body) + "\n```"
        with pytest.raises(ValueError):
            extract_graph(SimpleNamespace(complete=lambda *args: response), TEXT)


def make_chunk(client, owner, text=TEXT):
    case = client.post(
        "/api/cases", headers=owner, json={"title": "文档关系核验", "workflow": "qualification"}
    ).json()
    with client.app.state.sessions.begin() as session:
        index_units(
            session,
            "case:" + case["id"],
            [{"text": text, "location": {"page": 1}}],
            "a" * 64,
            True,
            case_id=case["id"],
        )
    hits = client.post(
        "/api/search",
        headers=owner,
        json={"query": "K170", "as_of": "2025-07-01", "case_id": case["id"]},
    ).json()
    return case, hits["hits"][0]["id"]


def test_business_graph_does_not_merge_same_model_or_leak_other_owner(client):
    client.post("/api/admin/import-demo", headers=headers(client, "admin"))
    graph = client.get("/api/graph?as_of=2025-07-01", headers=headers(client)).json()
    ids = {node["id"] for node in graph["nodes"]}
    assert "product:sku-hit" in ids
    assert "product:sku-other-owner" not in ids
    assert "supplier:supplier-b" not in ids
    assert "rule:demo-qualification-v1" not in ids
    assert all(edge["subject"] in ids and edge["object"] in ids for edge in graph["edges"])
    admin_graph = client.get("/api/graph?as_of=2025-07-01", headers=headers(client, "admin")).json()
    products = [node for node in admin_graph["nodes"] if node["kind"] == "product"]
    assert len(products) == 4
    assert len({node["id"] for node in products}) == 4
    assert all(node["is_demo"] for node in products)


def test_document_relations_require_review_and_keep_origin_then_go_stale(client, monkeypatch):
    owner = headers(client)
    reviewer = headers(client, "reviewer")
    case, chunk_id = make_chunk(client, owner)
    monkeypatch.setattr(
        "quality.knowledge_graph.AgnesClient.complete", lambda *args: model_response()
    )
    payload = {"chunk_id": chunk_id, "case_id": case["id"], "as_of": "2025-07-01"}
    assert (
        client.post(
            "/api/graph/extractions", json=payload, headers=headers(client, "merchant-b")
        ).status_code
        == 404
    )
    response = client.post("/api/graph/extractions", json=payload, headers=owner)
    assert response.status_code == 200, response.text
    row = response.json()
    assert row["status"] == "pending" and row["engine_mode"] == "fake"
    graph_path = f"/api/graph?as_of=2025-07-01&case_id={case['id']}"
    assert client.get(graph_path, headers=owner).json()["edges"] == []
    review_path = f"/api/graph/extractions/{row['id']}/review"
    decision = {
        "decision": "confirmed",
        "note": "协议替身，逐字核对模拟文本",
        "as_of": "2025-07-01",
    }
    assert client.post(review_path, json=decision, headers=owner).status_code == 403
    assert client.post(review_path, json=decision, headers=reviewer).status_code == 200
    # 重复核验不覆盖首个核验人和备注。
    assert (
        client.post(review_path, json={**decision, "note": "覆盖尝试"}, headers=reviewer).json()[
            "review_note"
        ]
        == decision["note"]
    )
    assert (
        client.post(
            review_path, json={**decision, "decision": "rejected"}, headers=reviewer
        ).status_code
        == 409
    )
    graph = client.get(graph_path, headers=owner).json()
    assert len(graph["edges"]) == 2
    assert graph["edges"][0]["provenance"]["chunk_id"] == chunk_id
    assert graph["edges"][0]["provenance"]["engine_mode"] == "fake"
    assert all(node["id"].startswith("doc:" + row["id"] + ":") for node in graph["nodes"])
    with client.app.state.sessions.begin() as session:
        index_units(
            session,
            "case:" + case["id"],
            [{"text": "新资料 K170", "location": {"page": 1}}],
            "b" * 64,
            True,
            case_id=case["id"],
        )
    assert client.get(graph_path, headers=owner).json()["edges"] == []
    rows = client.get(
        f"/api/graph/extractions?as_of=2025-07-01&case_id={case['id']}", headers=owner
    ).json()
    assert rows[0]["is_current"] is False and rows[0]["status"] == "confirmed"
    assert client.post(review_path, json=decision, headers=reviewer).status_code == 409


@pytest.mark.parametrize(
    "change",
    [
        "invented_quote",
        "unknown_endpoint",
        "duplicate_key",
        "invented_entity",
        "invented_qualifier",
    ],
)
def test_extraction_rejects_unsupported_schema_and_evidence(change):
    body = json.loads(json.dumps(BODY))
    if change == "invented_quote":
        body["relations"][0]["quote"] = "这不是原文"
    elif change == "unknown_endpoint":
        body["relations"][0]["object"] = "other"
    elif change == "duplicate_key":
        body["entities"][1]["key"] = "supplier"
    elif change == "invented_entity":
        body["entities"][0]["name"] = "供应商乙"
    else:
        body["entities"][0]["qualifiers"] = {"owner": "另一个企业"}

    class FixtureModel:
        def complete(self, messages):
            return model_response(body)

    with pytest.raises(ValueError):
        extract_graph(FixtureModel(), TEXT)


def test_missing_key_and_revision_change_do_not_store_valid_candidates(client, monkeypatch):
    from quality.models import ReviewCase

    owner = headers(client)
    case, chunk_id = make_chunk(client, owner)
    payload = {"chunk_id": chunk_id, "case_id": case["id"], "as_of": "2025-07-01"}
    response = client.post("/api/graph/extractions", json=payload, headers=owner)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "missing_agnes_key"

    def mutate(*args):
        with client.app.state.sessions.begin() as session:
            session.get(ReviewCase, case["id"]).revision += 1
        return model_response()

    monkeypatch.setattr("quality.knowledge_graph.AgnesClient.complete", mutate)
    assert client.post("/api/graph/extractions", json=payload, headers=owner).status_code == 409
    rows = client.get(
        f"/api/graph/extractions?as_of=2025-07-01&case_id={case['id']}", headers=owner
    ).json()
    assert rows == []


def test_more_than_200_stale_extractions_do_not_block_current_graph(client, monkeypatch):
    from quality.models import GraphExtraction

    with client.app.state.sessions.begin() as session:
        for index in range(201):
            session.add(
                GraphExtraction(
                    id=f"stale-{index:03}",
                    chunk_id="missing",
                    source_sha256="0" * 64,
                    body=BODY,
                    location={},
                    status="confirmed",
                    engine_mode="fake",
                    is_demo=True,
                    created_by="admin",
                    reviewed_by="reviewer",
                )
            )
    owner = headers(client)
    path = "/api/graph?as_of=2025-07-01"
    response = client.get(path, headers=owner)
    assert response.status_code == 200
    assert response.json()["edges"] == []
    case, chunk_id = make_chunk(client, owner)
    monkeypatch.setattr(
        "quality.knowledge_graph.AgnesClient.complete", lambda *args: model_response()
    )
    row = client.post(
        "/api/graph/extractions",
        headers=owner,
        json={
            "chunk_id": chunk_id,
            "case_id": case["id"],
            "as_of": "2025-07-01",
        },
    ).json()
    response = client.post(
        f"/api/graph/extractions/{row['id']}/review",
        headers=headers(client, "reviewer"),
        json={
            "decision": "confirmed",
            "note": "新来源仍应可用",
            "as_of": "2025-07-01",
        },
    )
    assert response.status_code == 200
    response = client.get(path + "&case_id=" + case["id"], headers=owner)
    assert response.status_code == 200
    assert len(response.json()["edges"]) == 2
