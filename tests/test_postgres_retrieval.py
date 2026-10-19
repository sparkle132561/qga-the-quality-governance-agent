import hashlib
import os
from datetime import date
from uuid import uuid4

import pytest
from conftest import headers
from fastapi.testclient import TestClient
from quality.config import Settings
from quality.main import create_app
from quality.models import EvidenceChunk
from quality.retrieval import vector_search


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="需独立PostgreSQL测试库")
def test_real_pgvector_ordering_and_private_scope_without_claiming_model_quality():
    url = os.environ["TEST_DATABASE_URL"]
    if not url.endswith("/quality_test"):
        pytest.fail("仅使用quality_test测试库")
    app = create_app(Settings(database_url=url, jwt_secret="test-vector-only-" * 4))
    with TestClient(app) as client:
        first = client.post(
            "/api/cases",
            headers=headers(client),
            json={"title": "测试向量甲", "workflow": "qualification"},
        ).json()
        other = client.post(
            "/api/cases",
            headers=headers(client, "merchant-b"),
            json={"title": "测试向量乙", "workflow": "qualification"},
        ).json()
        expected_ids = []
        with app.state.sessions.begin() as session:
            for case, direction in [(first, 0), (first, 1), (other, 0)]:
                chunk_id = str(uuid4())
                vector = [0.0] * 1024
                vector[direction] = 1.0
                session.add(
                    EvidenceChunk(
                        id=chunk_id,
                        document_id="test:" + chunk_id,
                        case_id=case["id"],
                        text="测试数学向量，不是模型Embedding",
                        location={"kind": "test_fixture"},
                        source_sha256=hashlib.sha256(chunk_id.encode()).hexdigest(),
                        embedding=vector,
                        embedding_model="test-fixture-vector",
                        is_demo=True,
                    )
                )
                if case is first:
                    expected_ids.append(chunk_id)
        query_vector = [1.0] + [0.0] * 1023
        with app.state.sessions() as session:
            hits = vector_search(
                session,
                query_vector,
                date(2025, 7, 1),
                10,
                first["id"],
                model="test-fixture-vector",
            )
            assert [hit["id"] for hit in hits] == expected_ids
            assert hits[0]["score"] == pytest.approx(1)
            assert hits[1]["score"] == pytest.approx(0)
            assert all(hit["case_id"] == first["id"] for hit in hits)
