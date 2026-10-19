import io
import json

import pytest
from conftest import headers
from fastapi import HTTPException
from PIL import Image
from pydantic import ValidationError
from quality.models import EvidenceChunk, Material, ReviewCase
from quality.providers import AgnesClient
from quality.vision import VisualReading, canonical_image
from sqlalchemy import select


def reading(value="K170", certainty="clear", quality="clear"):
    return {"image_quality": quality, "fields": [{"name": "model", "value": value,
            "quote": "MODEL: " + (value or "unreadable"), "box": [20, 20, 700, 200],
            "certainty": certainty}], "warnings": []}


def setup_image(client, monkeypatch, ocr_model="K170"):
    monkeypatch.setattr("quality.vision.parse_material", lambda *args: {
        "engine": "controlled-canonical-ocr", "units": [{"text": "MODEL: K170",
            "location": {"kind": "image", "box": [[4, 2], [140, 2], [140, 20], [4, 20]]},
            "confidence": .95}],
    })
    def parse(data, suffix):
        return {"status": "parsed", "engine": "controlled-ocr", "parser_version": "test",
                "warning": None, "units": [{"text": "MODEL: " + ocr_model,
                                             "location": {"kind": "image", "box": []},
                                             "confidence": 0.8}],
                "fields": [{"name": "model", "value": ocr_model, "unit_index": 0,
                            "location": {"kind": "image", "box": []}, "confidence": 0.8}]}
    monkeypatch.setattr("quality.materials.parse_material", parse)
    auth = headers(client)
    case = client.post("/api/cases", headers=auth, json={
        "title": "视觉图片", "workflow": "qualification", "payload": {"as_of": "2025-07-01"},
    }).json()
    image = io.BytesIO()
    Image.new("RGB", (200, 100), "white").save(image, format="PNG")
    item = client.post(f"/api/cases/{case['id']}/materials", headers=auth,
                       files={"file": ("label.png", image.getvalue(), "image/png")}).json()
    path = f"/api/cases/{case['id']}/materials/{item['id']}"
    return case, item, path, auth


def fake_model(monkeypatch, body=None, callback=None):
    calls = []
    def complete(self, messages, tools=None):
        calls.append(messages)
        assert messages[1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        if callback:
            callback()
        return {"message": {"role": "assistant", "content": json.dumps(body or reading())},
                "engine_mode": "fake", "usage": {"total_tokens": 7}, "elapsed_ms": 1}
    monkeypatch.setattr(AgnesClient, "complete", complete)
    return calls


def test_candidate_is_separate_then_human_confirms_with_ocr_conflict_preserved(client, monkeypatch):
    case, item, path, auth = setup_image(client, monkeypatch, ocr_model="K170S")
    fake_model(monkeypatch)
    proposed = client.post(path + "/vision", headers=auth, json={"expected_revision": 2})
    assert proposed.status_code == 200
    candidate = proposed.json()["vision"]
    with client.app.state.sessions() as session:
        row = session.get(Material, item["id"])
        assert [f["value"] for f in row.parsed["fields"]] == ["K170S"]
        assert row.sha256 == item["sha256"]
    request = {"expected_revision": 3, "candidate_id": candidate["id"],
               "accepted_fields": [0], "note": "核对实际图片中的字段与区域"}
    assert client.post(path + "/vision/confirm", headers=auth, json=request).status_code == 403
    reviewer = headers(client, "reviewer")
    confirmed = client.post(path + "/vision/confirm", headers=reviewer, json=request)
    assert confirmed.status_code == 200 and confirmed.json()["revision"] == 4
    with client.app.state.sessions() as session:
        row = session.get(Material, item["id"])
        assert [f["value"] for f in row.parsed["fields"]] == ["K170S", "K170"]
        assert row.parsed["units"][0] == item["parsed"]["units"][0]
        assert row.parsed["fields"][1]["location"]["source"] == "human_confirmed_vision"
        assert len(row.parsed["cleaning"]["units"]) == len(row.parsed["units"])
        chunks = list(session.scalars(
            select(EvidenceChunk).where(EvidenceChunk.material_id == row.id)
        ))
        assert any(c.location.get("vision_id") == candidate["id"] for c in chunks)
    assert client.post(path + "/vision/confirm", headers=reviewer, json=request).status_code == 409
    repeated = client.post(path + "/vision", headers=auth, json={"expected_revision": 4})
    assert repeated.status_code == 409


def test_owner_role_status_and_revision_are_checked_before_external_call(client, monkeypatch):
    case, item, path, auth = setup_image(client, monkeypatch)
    calls = fake_model(monkeypatch)
    assert client.post(path + "/vision", headers=headers(client, "merchant-b"),
                       json={"expected_revision": 2}).status_code == 404
    assert client.post(path + "/vision", headers=headers(client, "admin"),
                       json={"expected_revision": 2}).status_code == 403
    assert client.post(path + "/vision", headers=auth,
                       json={"expected_revision": 1}).status_code == 409
    client.post(f"/api/cases/{case['id']}/submit", headers=auth)
    assert client.post(path + "/vision", headers=auth,
                       json={"expected_revision": 2}).status_code == 409
    assert calls == []


def test_concurrent_edit_discards_inflight_vision_result(client, monkeypatch):
    case, item, path, auth = setup_image(client, monkeypatch)
    def changed():
        with client.app.state.sessions.begin() as session:
            session.get(ReviewCase, case["id"]).revision += 1
    fake_model(monkeypatch, callback=changed)
    assert client.post(path + "/vision", headers=auth,
                       json={"expected_revision": 2}).status_code == 409
    with client.app.state.sessions() as session:
        assert "vision" not in session.get(Material, item["id"]).parsed


@pytest.mark.parametrize("quality,certainty", [("blurred", "clear"), ("clear", "uncertain")])
def test_uncertain_reading_never_becomes_evidence(client, monkeypatch, quality, certainty):
    case, item, path, auth = setup_image(client, monkeypatch)
    fake_model(monkeypatch, reading(quality=quality, certainty=certainty))
    result = client.post(path + "/vision", headers=auth, json={"expected_revision": 2}).json()
    confirmed = client.post(path + "/vision/confirm", headers=headers(client, "reviewer"), json={
        "expected_revision": 3, "candidate_id": result["vision"]["id"],
        "accepted_fields": [0], "note": "不能忽略不确定标记",
    })
    assert confirmed.status_code == 422
    with client.app.state.sessions() as session:
        assert len(session.get(Material, item["id"]).parsed["fields"]) == 1


def test_source_parser_change_or_original_file_change_rejects_confirmation(client, monkeypatch):
    case, item, path, auth = setup_image(client, monkeypatch)
    fake_model(monkeypatch)
    result = client.post(path + "/vision", headers=auth, json={"expected_revision": 2}).json()
    with client.app.state.sessions.begin() as session:
        row = session.get(Material, item["id"])
        row.parsed = {**row.parsed, "parser_version": "changed"}
    assert client.post(path + "/vision/confirm", headers=headers(client, "reviewer"), json={
        "expected_revision": 3, "candidate_id": result["vision"]["id"],
        "accepted_fields": [0], "note": "旧解析不应继续确认",
    }).status_code == 409
    with client.app.state.sessions.begin() as session:
        session.get(Material, item["id"]).sha256 = "f" * 64
    assert client.get(path + "/vision-image", headers=auth).status_code == 409
    denied = client.get(path + "/vision-image", headers=headers(client, "merchant-b"))
    assert denied.status_code == 404


def test_bad_box_or_truncated_identifier_is_rejected():
    for box in ([0, 0, 1001, 100], [100, 100, 0, 0], [True, 0, 10, 10]):
        value = reading()
        value["fields"][0]["box"] = box
        with pytest.raises(ValidationError):
            VisualReading.model_validate(value)
    value = reading()
    value["fields"][0]["quote"] = "MODEL K170S"
    with pytest.raises(ValidationError):
        VisualReading.model_validate(value)


def test_conversion_is_deterministic_and_invalid_payload_is_not_forwarded():
    image = io.BytesIO()
    Image.new("RGBA", (20, 10), (0, 0, 0, 0)).save(image, format="PNG")
    first, info = canonical_image(image.getvalue())
    assert canonical_image(image.getvalue()) == (first, info)
    assert Image.open(io.BytesIO(first)).getpixel((0, 0)) == (255, 255, 255)
    with pytest.raises(HTTPException):
        canonical_image(b"not a picture")


@pytest.mark.parametrize("mode", ["P", "RGB"])
def test_png_transparent_palette_and_rgb_color_are_composited_on_white(mode):
    image = Image.new(mode, (8, 8), 0)
    if mode == "P":
        image.putpalette([0, 0, 0] * 256)
        image.info["transparency"] = 0
    else:
        image.info["transparency"] = (0, 0, 0)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    data, _ = canonical_image(stream.getvalue())
    assert Image.open(io.BytesIO(data)).getpixel((0, 0)) == (255, 255, 255)


def test_missing_key_does_not_create_vision_candidate(client, monkeypatch):
    case, item, path, auth = setup_image(client, monkeypatch)
    response = client.post(path + "/vision", headers=auth, json={"expected_revision": 2})
    assert response.status_code == 503
    with client.app.state.sessions() as session:
        assert session.get(ReviewCase, case["id"]).revision == 2
        assert "vision" not in session.get(Material, item["id"]).parsed
