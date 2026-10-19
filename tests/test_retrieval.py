from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import headers
from quality.retrieval import split_text, tokenize


def test_chunks_keep_original_offsets_and_overlap_progress():
    source = "型号K170。\n" * 15
    chunks = list(split_text(source, max_chars=20, overlap=19))
    assert chunks[0][1] == 0
    assert chunks[-1][2] == len(source)
    assert all(text == source[start:end] for text, start, end in chunks)
    assert all(chunks[index][1] < chunks[index + 1][1] for index in range(len(chunks) - 1))
    assert "k170s" in tokenize("核对型号 K170S")
    assert "k170" not in tokenize("K170S")
    with pytest.raises(ValueError):
        list(split_text("x", 20, 20))


def test_keyword_rule_version_no_match_and_missing_embedding(client):
    admin = headers(client, "admin")
    client.post("/api/admin/import-demo", headers=admin)
    first = client.post("/api/admin/index-rules", headers=admin)
    assert first.status_code == 200
    assert first.json()["added_chunks"] >= 3
    assert client.post("/api/admin/index-rules", headers=admin).json()["added_chunks"] == 0
    for as_of, version in [("2025-06-30", "1"), ("2025-07-01", "2")]:
        response = client.post(
            "/api/search",
            headers=admin,
            json={"query": "模拟平台准入 型号证书", "as_of": as_of, "method": "bm25", "limit": 20},
        )
        assert response.status_code == 200
        hits = response.json()["hits"]
        assert any(hit["rule_id"] == "demo-qualification-v" + version for hit in hits)
        other = "2" if version == "1" else "1"
        assert all(hit["rule_id"] != "demo-qualification-v" + other for hit in hits)
        assert response.json()["timing_ms"]["total"] >= 0
    empty = client.post(
        "/api/search", headers=admin, json={"query": "zxqnomatchingtoken", "as_of": "2025-07-01"}
    )
    assert empty.json()["hits"] == []
    live = client.post(
        "/api/search",
        headers=admin,
        json={"query": "证书", "as_of": "2025-07-01", "method": "vector"},
    )
    assert live.status_code == 503
    assert live.json()["detail"]["code"] == "missing_key"
    rewrite = client.post(
        "/api/search", headers=admin, json={"query": "证书", "as_of": "2025-07-01", "rewrite": True}
    )
    assert rewrite.status_code == 503
    assert rewrite.json()["detail"]["code"] == "missing_agnes_key"
    rerank = client.post(
        "/api/search", headers=admin, json={"query": "证书", "as_of": "2025-07-01", "rerank": True}
    )
    assert rerank.status_code == 503
    assert rerank.json()["detail"]["code"] == "missing_key"
    for method in ("bm25", "vector"):
        assert (
            client.post(
                "/api/search",
                headers=admin,
                json={"query": "   ", "as_of": "2025-07-01", "method": method},
            ).status_code
            == 422
        )


def test_private_materials_are_only_searchable_in_authorized_case(client):
    owner = headers(client)
    case = client.post(
        "/api/cases", headers=owner, json={"title": "私有证据", "workflow": "qualification"}
    ).json()
    path = f"/api/cases/{case['id']}"
    client.post(
        path + "/materials",
        headers=owner,
        files={"file": ("source.html", b"<p>privateuniquetoken K170</p>", "text/html")},
    )
    assert client.post(path + "/index-materials", headers=owner).status_code == 200
    request = {"query": "privateuniquetoken", "as_of": "2025-07-01", "case_id": case["id"]}
    hits = client.post("/api/search", headers=owner, json=request).json()["hits"]
    assert len(hits) == 1
    assert hits[0]["location"]["fragment"] == 1
    other = headers(client, "merchant-b")
    assert client.post("/api/search", headers=other, json=request).status_code == 404
    assert client.post(path + "/index-materials", headers=other).status_code == 404
    request.pop("case_id")
    assert client.post("/api/search", headers=owner, json=request).json()["hits"] == []


def test_concurrent_rule_and_material_indexing_do_not_duplicate_chunks(client):
    admin = headers(client, "admin")
    client.post("/api/admin/import-demo", headers=admin)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(lambda _: client.post("/api/admin/index-rules", headers=admin), range(4))
        )
    assert all(response.status_code == 200 for response in responses)
    assert sum(response.json()["added_chunks"] > 0 for response in responses) == 1
    owner = headers(client)
    case = client.post(
        "/api/cases", headers=owner, json={"title": "并发索引", "workflow": "qualification"}
    ).json()
    path = f"/api/cases/{case['id']}"
    client.post(
        path + "/materials",
        headers=owner,
        files={"file": ("source.html", b"<p>MODEL: K170</p>", "text/html")},
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(lambda _: client.post(path + "/index-materials", headers=owner), range(4))
        )
    assert all(response.status_code == 200 for response in responses)
    assert sum(response.json()["added_chunks"] for response in responses) == 1
