from conftest import headers
from quality.graph_paths import PathRequest, find_paths
from quality.models import GraphExtraction
from test_knowledge_graph import make_chunk


def test_three_hop_business_path_scope_direction_and_budget(client):
    client.post("/api/admin/import-demo", headers=headers(client, "admin"))
    payload = {"as_of": "2025-07-01", "start_id": "supplier:supplier-a", "target_kind": "order"}
    response = client.post("/api/graph/paths", headers=headers(client), json=payload)
    assert response.status_code == 200, response.text
    result = response.json()
    assert {path["nodes"][-1]["id"] for path in result["paths"]} == {
        "order:demo-order-hit",
        "order:demo-order-date-out",
    }
    assert all(path["hops"] == 3 for path in result["paths"])
    assert result["truncated"] is False
    assert result["timing_ms"]["revalidate"] >= 0
    assert (
        client.post(
            "/api/graph/paths", headers=headers(client, "merchant-b"), json=payload
        ).status_code
        == 404
    )
    shallow = client.post(
        "/api/graph/paths", headers=headers(client), json={**payload, "max_hops": 2}
    ).json()
    assert shallow["paths"] == [] and not shallow["truncated"]
    reverse = client.post(
        "/api/graph/paths",
        headers=headers(client),
        json={
            "as_of": "2025-07-01",
            "start_id": "order:demo-order-hit",
            "end_id": "supplier:supplier-a",
            "direction": "in",
        },
    ).json()
    assert len(reverse["paths"]) == 1
    assert all(edge["traversed_reverse"] for edge in reverse["paths"][0]["edges"])


def test_cycles_and_expansion_limit_are_explicit():
    nodes = [{"id": key, "kind": "product"} for key in ("a", "b", "c", "d")]
    edges = [
        {"subject": left, "object": right, "predicate": "has_batch"}
        for left, right in (
            ("a", "b"),
            ("b", "a"),
            ("b", "c"),
            ("c", "d"),
        )
    ]
    request = PathRequest(as_of="2025-07-01", start_id="a", end_id="d", direction="both")
    graph = {"nodes": nodes, "edges": edges}
    result = find_paths(graph, request)
    assert result["paths"]
    assert all(
        len({node["id"] for node in path["nodes"]}) == len(path["nodes"])
        for path in result["paths"]
    )
    bounded = find_paths(graph, request, max_states=1)
    assert bounded["truncated"] and bounded["stop_reason"] == "expansion_budget"
    assert bounded["paths"] == []


def test_reviewed_cross_document_identity_links_have_sources_and_go_stale(client, monkeypatch):
    owner = headers(client)
    reviewer = headers(client, "reviewer")
    case, chunk_id = make_chunk(client, owner)
    with client.app.state.sessions.begin() as session:
        from quality.models import EvidenceChunk
        from quality.retrieval import index_units
        from sqlalchemy import select

        second_document = "case:" + case["id"] + ":second"
        index_units(
            session,
            second_document,
            [{"text": "K170包含批次B01。", "location": {"page": 2}}],
            "c" * 64,
            True,
            case_id=case["id"],
        )
        second_chunk_id = session.scalar(
            select(EvidenceChunk.id).where(EvidenceChunk.document_id == second_document)
        )
        first = GraphExtraction(
            chunk_id=chunk_id,
            source_sha256="a" * 64,
            case_id=case["id"],
            case_revision=1,
            status="confirmed",
            engine_mode="fake",
            is_demo=True,
            created_by="merchant-a",
            reviewed_by="reviewer",
            location={"page": 1},
            body={
                "entities": [
                    {"key": "s", "kind": "supplier", "name": "供应商甲", "qualifiers": {}},
                    {"key": "p", "kind": "product", "name": "K170", "qualifiers": {}},
                ],
                "relations": [
                    {
                        "subject": "s",
                        "predicate": "supplies",
                        "object": "p",
                        "quote": "供应商甲为K170供货",
                    }
                ],
            },
        )
        second = GraphExtraction(
            chunk_id=second_chunk_id,
            source_sha256="c" * 64,
            case_id=case["id"],
            case_revision=1,
            status="confirmed",
            engine_mode="fake",
            is_demo=True,
            created_by="merchant-a",
            reviewed_by="reviewer",
            location={"page": 2},
            body={
                "entities": [
                    {"key": "p", "kind": "product", "name": "K170", "qualifiers": {}},
                    {"key": "b", "kind": "batch", "name": "B01", "qualifiers": {}},
                ],
                "relations": [
                    {
                        "subject": "p",
                        "predicate": "has_batch",
                        "object": "b",
                        "quote": "K170包含批次B01",
                    }
                ],
            },
        )
        session.add_all([first, second])
        session.flush()
        start, left, right, end = (
            f"doc:{first.id}:s",
            f"doc:{first.id}:p",
            f"doc:{second.id}:p",
            f"doc:{second.id}:b",
        )
    path_input = {"as_of": "2025-07-01", "case_id": case["id"], "start_id": start, "end_id": end}
    assert client.post("/api/graph/paths", json=path_input, headers=owner).json()["paths"] == []
    link_input = {
        "as_of": "2025-07-01",
        "case_id": case["id"],
        "subject": left,
        "object": right,
        "note": "模拟身份核验：产品型号及主体相同，非自动名称合并",
    }
    assert (
        client.post("/api/graph/identity-links", json=link_input, headers=owner).status_code == 403
    )
    linked = client.post("/api/graph/identity-links", json=link_input, headers=reviewer)
    assert linked.status_code == 200, linked.text
    assert linked.json()["created"] is True
    repeated = client.post(
        "/api/graph/identity-links",
        json={**link_input, "subject": right, "object": left},
        headers=reviewer,
    )
    assert repeated.json()["created"] is False
    result = client.post("/api/graph/paths", json=path_input, headers=owner).json()
    assert len(result["paths"]) == 1 and result["paths"][0]["hops"] == 3
    assert result["paths"][0]["edges"][1]["predicate"] == "same_entity"
    assert {hit["id"] for hit in result["evidence"]} == {chunk_id, second_chunk_id}
    assert result["paths"][0]["edges"][1]["provenance"]["reviewed_by"] == "reviewer"
    identity_only = client.post(
        "/api/graph/paths",
        json={
            **path_input,
            "start_id": left,
            "end_id": right,
            "max_hops": 1,
        },
        headers=owner,
    ).json()
    assert len(identity_only["paths"]) == 1
    assert {hit["id"] for hit in identity_only["evidence"]} == {chunk_id, second_chunk_id}
    # 没有业务SKU也可以从当前案件已核验文档实体择路，仍遵守原权限/过期过滤。
    import json

    from quality.providers import AgnesClient

    def complete(self, messages, tools=None):
        return {"message": {"content": json.dumps({"route": "graph", "graph_start_id": start,
                "graph_target_kind": "batch", "reason": "跨文档核对批次"})},
                "engine_mode": "fake", "usage": {}, "elapsed_ms": 0}
    monkeypatch.setattr(AgnesClient, "complete", complete)
    route_url = f"/api/cases/{case['id']}/routed-search"
    route_input = {"query": "K170跨文档批次", "as_of": "2025-07-01"}
    routed = client.post(route_url, json=route_input, headers=owner)
    assert routed.status_code == 200, routed.text
    assert any(path["nodes"][-1]["id"] == end for path in routed.json()["paths"])
    assert client.post(route_url, json=route_input,
                       headers=headers(client, "merchant-b")).status_code == 404
    from quality.models import ReviewCase

    with client.app.state.sessions.begin() as session:
        session.get(ReviewCase, case["id"]).revision += 1
    assert client.post("/api/graph/paths", json=path_input, headers=owner).status_code == 404
    stale = client.post(route_url, json=route_input, headers=owner)
    assert stale.status_code == 200, stale.text
    assert stale.json()["route"]["route"] == "evidence" and stale.json()["paths"] == []


def test_independent_business_records_cannot_be_merged(client):
    admin = headers(client, "admin")
    client.post("/api/admin/import-demo", headers=admin)
    response = client.post(
        "/api/graph/identity-links",
        headers=admin,
        json={
            "as_of": "2025-07-01",
            "subject": "product:sku-hit",
            "object": "product:sku-date-out",
            "note": "同名不代表同一SKU",
        },
    )
    assert response.status_code == 422


def test_same_document_reextractions_cannot_be_linked(client):
    owner = headers(client)
    case, chunk_id = make_chunk(client, owner)
    with client.app.state.sessions.begin() as session:
        rows = [
            GraphExtraction(
                chunk_id=chunk_id,
                source_sha256="a" * 64,
                case_id=case["id"],
                case_revision=1,
                status="confirmed",
                engine_mode="fake",
                is_demo=True,
                created_by="merchant-a",
                location={},
                reviewed_by="reviewer",
                body={
                    "entities": [
                        {"key": "p", "kind": "product", "name": "K170", "qualifiers": {}},
                        {"key": "alias", "kind": "product", "name": "K170", "qualifiers": {}},
                    ],
                    "relations": [],
                },
            )
            for _ in range(2)
        ]
        session.add_all(rows)
        session.flush()
        first_id, second_id = [row.id for row in rows]
    for target in (f"doc:{first_id}:alias", f"doc:{second_id}:p"):
        response = client.post(
            "/api/graph/identity-links",
            headers=headers(client, "reviewer"),
            json={
                "as_of": "2025-07-01",
                "case_id": case["id"],
                "subject": f"doc:{first_id}:p",
                "object": target,
                "note": "同来源不能伪装成跨文档关联",
            },
        )
        assert response.status_code == 422


def test_graph_snapshot_change_rejects_path_response(client):
    from copy import deepcopy

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from quality.domain import Actor, Role
    from quality.graph_paths import graph_signature, register_path_routes

    graph = {
        "nodes": [
            {"id": "a", "kind": "product", "label": "A"},
            {"id": "b", "kind": "batch", "label": "B"},
        ],
        "edges": [{"subject": "a", "object": "b", "predicate": "has_batch", "provenance": {}}],
        "elapsed_ms": 0,
    }
    assert graph_signature(graph) == graph_signature(
        {**graph, "nodes": list(reversed(graph["nodes"]))}
    )
    reads = []

    def reader(**kwargs):
        reads.append(1)
        snapshot = deepcopy(graph)
        if len(reads) > 1:
            snapshot["nodes"][1]["label"] = "changed"
        return snapshot

    app = FastAPI()
    register_path_routes(
        app,
        client.app.state.sessions,
        lambda: Actor(user_id="admin", role=Role.ADMIN),
        lambda *args: None,
        reader,
    )
    with TestClient(app) as probe:
        response = probe.post(
            "/api/graph/paths", json={"as_of": "2025-07-01", "start_id": "a", "end_id": "b"}
        )
    assert response.status_code == 409
    assert len(reads) == 2
