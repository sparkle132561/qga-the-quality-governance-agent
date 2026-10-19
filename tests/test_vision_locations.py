from conftest import headers
from PIL import Image
from quality.models import Material
from quality.vision import VisualReading, canonical_image
from test_vision import fake_model, setup_image


def unit(text, box):
    x0, y0, x1, y1 = box
    return {"text": text, "location": {"kind": "image",
            "box": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}, "confidence": .95}


def field(value, name="brand", quote=None):
    return {"name": name, "value": value, "quote": quote or value,
            "box": [730, 35, 1000, 205], "certainty": "clear"}


def align(fields, units, width=1557, height=1404):
    from quality.vision_locations import locate_fields
    reading = VisualReading.model_validate({"image_quality": "clear", "fields": fields})
    return locate_fields(reading, units, width, height)


def test_r001_brand_uses_spatial_ocr_lines_not_wrong_model_box():
    locations = align([field("Lean Cuisine")], [
        unit("Lean", [986, 62, 1376, 176]),
        unit("SE", [32, 153, 43, 170]),
        unit("ecuisine", [972, 168, 1377, 247]),
    ])
    assert locations[0]["status"] == "ocr_approximate"
    assert locations[0]["unit_indices"] == [0, 2]
    x0, y0, x1, y1 = locations[0]["box"]
    assert x0 * 1557 / 1000 <= 972 and y0 * 1404 / 1000 <= 62
    assert x1 * 1557 / 1000 >= 1377 and y1 * 1404 / 1000 >= 247


def test_exact_multiline_name_excludes_separate_description():
    locations = align([field("TEX-MEX RICE & BLACK BEANS", "product_name")], [
        unit("TEX-MEX RICE", [960, 414, 1255, 477]),
        unit("& BLACK BEANS", [960, 480, 1289, 545]),
        unit("in a spicy enchilada sauce", [957, 550, 1391, 590]),
    ])
    assert locations[0]["status"] == "ocr_exact"
    assert locations[0]["unit_indices"] == [0, 1]
    assert locations[0]["box"][3] * 1404 / 1000 < 550


def test_repeated_text_is_ambiguous_and_cannot_fall_back_to_model_box():
    locations = align([field("Acme")], [unit("Acme", [0, 0, 100, 30]),
                                         unit("Acme", [900, 700, 1000, 730])])
    assert locations[0]["status"] == "needs_manual_location"
    assert locations[0]["box"] is None
    assert locations[0]["reason"] == "ambiguous_ocr_matches"


def test_disjoint_columns_are_not_joined_to_invent_a_brand():
    locations = align([field("Lean Cuisine")], [unit("Lean", [0, 0, 100, 50]),
                                                 unit("Cuisine", [900, 60, 1000, 90])])
    assert locations[0]["box"] is None


def test_no_ocr_match_does_not_trust_model_coordinates():
    assert align([field("Acme")], [])[0]["status"] == "needs_manual_location"


def test_numeric_changes_and_identifier_suffixes_are_not_fuzzy_corrected():
    assert align([field("100g", "net_quantity", "NET WT 100g")],
                 [unit("NET WT 1000g", [0, 0, 300, 30])])[0]["box"] is None
    assert align([field("K170", "model", "MODEL K170")],
                 [unit("MODEL K170S", [0, 0, 300, 30])])[0]["box"] is None
    assert align([field("2.5kg", "net_quantity", "NET WT 2.5kg")],
                 [unit("NET WT 25kg", [0, 0, 300, 30])])[0]["box"] is None
    assert align([field("K-170", "model", "MODEL K-170")],
                 [unit("MODEL K170", [0, 0, 300, 30])])[0]["box"] is None


def test_missing_location_requires_explicit_human_region_and_keeps_original(client, monkeypatch):
    _, item, path, owner = setup_image(client, monkeypatch)
    fake_model(monkeypatch)
    monkeypatch.setattr("quality.vision.parse_material", lambda *args: {
        "engine": "controlled-empty-ocr", "units": []})
    response = client.post(path + "/vision", headers=owner, json={"expected_revision": 2})
    assert response.status_code == 200
    candidate = response.json()["vision"]
    assert candidate["reading"]["fields"][0]["box"] is None
    assert candidate["model_reading"]["fields"][0]["box"] == [20, 20, 700, 200]
    reviewer = headers(client, "reviewer")
    request = {"expected_revision": 3, "candidate_id": candidate["id"],
               "accepted_fields": [0], "note": "对照原图实际框选，不采用模型框"}
    assert client.post(path + "/vision/confirm", headers=reviewer, json=request).status_code == 422
    request["corrected_regions"] = [{"field_index": 0, "box": [30, 40, 800, 300]}]
    assert client.post(path + "/vision/confirm", headers=reviewer, json=request).status_code == 200
    with client.app.state.sessions() as session:
        material = session.get(Material, item["id"])
        location = material.parsed["fields"][-1]["location"]
        assert location["region_source"] == "human_corrected"
        assert location["box"][0] == [6, 4]
        assert material.parsed["vision"]["model_reading"]["fields"][0]["box"] == [20, 20, 700, 200]


def test_ocr_alignment_not_model_box_is_confirmed(client, monkeypatch):
    _, item, path, owner = setup_image(client, monkeypatch)
    fake_model(monkeypatch)
    candidate = client.post(path + "/vision", headers=owner,
                            json={"expected_revision": 2}).json()["vision"]
    assert candidate["field_locations"][0]["status"] == "ocr_exact"
    request = {"expected_revision": 3, "candidate_id": candidate["id"],
               "accepted_fields": [0], "note": "核对OCR定位与原图文字"}
    assert client.post(path + "/vision/confirm", headers=headers(client, "reviewer"),
                       json=request).status_code == 200
    with client.app.state.sessions() as session:
        location = session.get(Material, item["id"]).parsed["fields"][-1]["location"]
        assert location["region_source"] == "canonical_ocr"
        assert location["box"][0] == [4, 2]


def test_human_regions_cannot_reference_unselected_or_duplicate_fields(client, monkeypatch):
    _, _, path, owner = setup_image(client, monkeypatch)
    fake_model(monkeypatch)
    candidate = client.post(path + "/vision", headers=owner,
                            json={"expected_revision": 2}).json()["vision"]
    request = {"expected_revision": 3, "candidate_id": candidate["id"],
               "accepted_fields": [0], "note": "错误索引不允许改变字段区域"}
    reviewer = headers(client, "reviewer")
    for regions in ([{"field_index": 1, "box": [0, 0, 100, 100]}],
                    [{"field_index": 0, "box": [0, 0, 100, 100]}] * 2,
                    [{"field_index": 0, "box": [True, 0, 100, 100]}]):
        response = client.post(path + "/vision/confirm", headers=reviewer,
                               json={**request, "corrected_regions": regions})
        assert response.status_code == 422


def test_canonical_ocr_failure_keeps_usage_and_pending_location(client, monkeypatch):
    _, _, path, owner = setup_image(client, monkeypatch)
    fake_model(monkeypatch)
    def failed(*args):
        raise RuntimeError("controlled OCR failure")
    monkeypatch.setattr("quality.vision.parse_material", failed)
    response = client.post(path + "/vision", headers=owner, json={"expected_revision": 2})
    assert response.status_code == 200
    candidate = response.json()["vision"]
    assert candidate["usage"]["total_tokens"] == 7
    assert candidate["location_ocr"]["status"] == "failed"
    assert candidate["reading"]["fields"][0]["box"] is None


def test_exif_rotation_uses_the_same_canonical_image_for_ocr_and_model(client, monkeypatch):
    import base64
    import io
    _, _, path, owner = setup_image(client, monkeypatch)
    calls = fake_model(monkeypatch)
    with client.app.state.sessions.begin() as session:
        row = session.query(Material).filter_by(id=path.split("/")[-1]).one()
        stored = client.app.state.settings.upload_dir / row.stored_name
        source = io.BytesIO()
        image = Image.new("RGB", (200, 100), "white")
        exif = Image.Exif()
        exif[274] = 6
        image.save(source, format="JPEG", exif=exif)
        raw = source.getvalue()
        import hashlib
        stored.write_bytes(raw)
        row.sha256 = hashlib.sha256(raw).hexdigest()
    observed = []
    def ocr(data, suffix):
        observed.append(data)
        assert Image.open(io.BytesIO(data)).size == (100, 200)
        return {"engine": "controlled-rotated-ocr", "units": [
            unit("MODEL: K170", [5, 10, 90, 40])]}
    monkeypatch.setattr("quality.vision.parse_material", ocr)
    response = client.post(path + "/vision", headers=owner, json={"expected_revision": 2})
    assert response.status_code == 200
    image_url = calls[0][1]["content"][1]["image_url"]["url"]
    assert base64.b64decode(image_url.split(",", 1)[1]) == observed[0] == canonical_image(raw)[0]
    assert response.json()["vision"]["reading"]["fields"][0]["box"] == [50, 50, 900, 200]


def test_unconfirmed_legacy_model_boxes_need_explicit_region_correction(client, monkeypatch):
    _, item, path, owner = setup_image(client, monkeypatch)
    fake_model(monkeypatch)
    candidate = client.post(path + "/vision", headers=owner,
                            json={"expected_revision": 2}).json()["vision"]
    with client.app.state.sessions.begin() as session:
        row = session.get(Material, item["id"])
        row.parsed = {**row.parsed, "vision": {**row.parsed["vision"], "version": "vision-v2"}}
    response = client.post(path + "/vision/confirm", headers=headers(client, "reviewer"), json={
        "expected_revision": 3, "candidate_id": candidate["id"], "accepted_fields": [0],
        "note": "旧模型框范围合法仍不代表文字定位正确"})
    assert response.status_code == 422


def test_structured_function_channel_is_validated_not_executed(client, monkeypatch):
    import json

    from quality.providers import AgnesClient
    from test_vision import reading
    _, _, path, owner = setup_image(client, monkeypatch)
    captured = []
    def complete(self, messages, tools=None):
        captured.append((self.tool_choice, tools))
        return {"message": {"content": None, "tool_calls": [{"id": "readonly-result",
            "type": "function", "function": {"name": "return_visual_reading",
                "arguments": json.dumps(reading())}}]},
            "usage": {"total_tokens": 7}, "elapsed_ms": 1, "engine_mode": "fake"}
    monkeypatch.setattr(AgnesClient, "complete", complete)
    response = client.post(path + "/vision", headers=owner, json={"expected_revision": 2})
    assert response.status_code == 200
    assert response.json()["vision"]["response_channel"] == "function_arguments"
    assert captured[0][0]["function"]["name"] == "return_visual_reading"
    assert captured[0][1][0]["function"]["parameters"]["properties"].get("fields")
    assert response.json()["vision"]["status"] == "candidate"


def test_unexpected_function_cannot_create_visual_evidence(client, monkeypatch):
    from quality.providers import AgnesClient
    _, item, path, owner = setup_image(client, monkeypatch)
    monkeypatch.setattr(AgnesClient, "complete", lambda *args, **kwargs: {
        "message": {"tool_calls": [{"function": {"name": "execute_action", "arguments": "{}"}}]},
        "usage": {}, "elapsed_ms": 1, "engine_mode": "fake"})
    response = client.post(path + "/vision", headers=owner, json={"expected_revision": 2})
    assert response.status_code == 422
    with client.app.state.sessions() as session:
        assert "vision" not in session.get(Material, item["id"]).parsed
