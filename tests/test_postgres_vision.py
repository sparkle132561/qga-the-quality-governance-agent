import io
import json
import os
from uuid import uuid4

import pytest
from conftest import headers
from fastapi.testclient import TestClient
from PIL import Image
from quality.config import Settings
from quality.main import create_app
from quality.providers import AgnesClient


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="需独立PostgreSQL测试库")
def test_vision_candidate_and_confirmed_source_survive_restart(monkeypatch, tmp_path):
    url = os.environ["TEST_DATABASE_URL"]
    if not url.endswith("/quality_test"):
        pytest.fail("仅允许quality_test独立库")
    settings = Settings(database_url=url, jwt_secret="vision-pg-only-" * 4,
                        agnes_api_key="", siliconflow_api_key="", upload_dir=tmp_path)
    monkeypatch.setattr("quality.materials.parse_material", lambda *args: {
        "status": "parsed", "engine": "controlled-ocr", "units": [], "fields": [],
    })
    monkeypatch.setattr("quality.vision.parse_material", lambda *args: {
        "engine": "controlled-canonical-ocr", "units": [{"text": "MODEL: K170",
            "location": {"kind": "image", "box": [[0, 0], [20, 0], [20, 10], [0, 10]]},
            "confidence": .95}],
    })
    monkeypatch.setattr(AgnesClient, "complete", lambda *args, **kwargs: {
        "message": {"content": json.dumps({"image_quality": "clear", "warnings": [],
                                            "fields": [{"name": "model", "value": "K170",
                                                        "quote": "MODEL: K170",
                                                        "box": [0, 0, 1000, 1000],
                                                        "certainty": "clear"}]})},
        "engine_mode": "fake", "usage": {}, "elapsed_ms": 0,
    })
    stream = io.BytesIO()
    Image.new("RGB", (20, 10), "white").save(stream, format="PNG")
    with TestClient(create_app(settings)) as first:
        auth = headers(first)
        case = first.post("/api/cases", headers=auth, json={
            "title": "TASK4 PG vision " + str(uuid4()), "workflow": "qualification",
        }).json()
        image = first.post(f"/api/cases/{case['id']}/materials", headers=auth, files={
            "file": ("label.png", stream.getvalue(), "image/png"),
        }).json()
        path = f"/api/cases/{case['id']}/materials/{image['id']}"
        proposed = first.post(path + "/vision", headers=auth,
                              json={"expected_revision": 2})
        assert proposed.status_code == 200
        candidate = proposed.json()["vision"]
    with TestClient(create_app(settings)) as second:
        result = second.post(path + "/vision/confirm", headers=headers(second, "reviewer"), json={
            "expected_revision": 3, "candidate_id": candidate["id"], "accepted_fields": [0],
            "note": "程序合成回归，不代表用户验收",
        })
        assert result.status_code == 200
    with TestClient(create_app(settings)) as third:
        auth = headers(third)
        rows = third.get(f"/api/cases/{case['id']}/materials", headers=auth).json()
        current = next(row for row in rows if row["id"] == image["id"])
        assert current["parsed"]["vision"]["status"] == "confirmed"
        assert current["parsed"]["fields"][0]["value"] == "K170"
        assert third.get(path + "/vision-image", headers=auth).status_code == 200
