"""真实品牌/生产者与品类/型号混淆：文字存在不等于字段身份成立。"""
import json

import pytest
from conftest import headers
from pydantic import ValidationError
from quality.models import Material
from quality.vision import VisualField, parse_visual_reading
from test_vision import fake_model, setup_image


def field(name, value, quote=None):
    return {"name": name, "value": value, "quote": quote or value, "certainty": "clear"}


def parse(fields):
    return parse_visual_reading(json.dumps({"image_quality": "clear", "fields": fields,
                                           "warnings": []}))


def test_real_r001_brand_cannot_also_be_manufacturer_without_role_evidence():
    reading, rejected = parse([field("brand", "Lean Cuisine"),
                               field("manufacturer", "Lean Cuisine")])
    assert [f.name for f in reading.fields] == ["brand"]
    assert rejected[0]["proposal"]["name"] == "manufacturer"


def test_real_r010_category_cannot_be_model_without_model_role():
    reading, rejected = parse([field("product_name", "PIE CRUST"), field("model", "PIE CRUST")])
    assert [f.name for f in reading.fields] == ["product_name"]
    assert rejected[0]["proposal"]["name"] == "model"


@pytest.mark.parametrize("quote", ["Manufactured by ACME", "Manufacturer: ACME",
                                  "Produced by ACME", "生产商：ACME", "制造商 ACME",
                                  "Produs de ACME", "Prodotto e confezionato da ACME",
                                  "PRODUCED AND BOTTLED BY ACME"])
def test_explicit_manufacturer_role_is_retained(quote):
    assert VisualField.model_validate(field("manufacturer", "ACME", quote)).value == "ACME"


def test_same_brand_and_manufacturer_is_allowed_with_distinct_role_evidence():
    reading, rejected = parse([field("brand", "ACME"),
                               field("manufacturer", "ACME", "Manufactured by ACME")])
    assert len(reading.fields) == 2 and not rejected


@pytest.mark.parametrize("quote", ["ACME", "Distributed by ACME", "Manufactured for ACME",
                                  "Not manufactured by ACME", "非生产商：ACME",
                                  "Brand: ACME; Manufacturer: OTHER"])
def test_other_roles_negation_and_unrelated_role_cannot_prove_manufacturer(quote):
    with pytest.raises(ValidationError):
        VisualField.model_validate(field("manufacturer", "ACME", quote))


@pytest.mark.parametrize("quote", ["MODEL: PIE CRUST", "Model No. PIE CRUST", "型号：PIE CRUST"])
def test_explicit_model_role_does_not_require_digits_or_change_value(quote):
    assert VisualField.model_validate(field("model", "PIE CRUST", quote)).value == "PIE CRUST"


def test_api_keeps_valid_fields_and_cannot_confirm_rejected_classification(client, monkeypatch):
    _, _, path, owner = setup_image(client, monkeypatch)
    fake_model(monkeypatch, {"image_quality": "clear", "warnings": [], "fields": [
        field("brand", "Lean Cuisine"), field("manufacturer", "Lean Cuisine"),
        field("model", "PIE CRUST")]})
    response = client.post(path + "/vision", headers=owner, json={"expected_revision": 2})
    assert response.status_code == 200
    candidate = response.json()["vision"]
    assert [f["name"] for f in candidate["reading"]["fields"]] == ["brand"]
    assert len(candidate["rejected_fields"]) == 2
    confirmed = client.post(path + "/vision/confirm", headers=headers(client, "reviewer"), json={
        "expected_revision": 3, "candidate_id": candidate["id"], "accepted_fields": [1],
        "corrected_regions": [{"field_index": 1, "box": [0, 0, 500, 500]}],
        "note": "错误类别不能通过人工修正位置绕过"})
    assert confirmed.status_code == 422


def test_stored_legacy_semantic_error_returns_actionable_422_not_500(client, monkeypatch):
    _, material, path, owner = setup_image(client, monkeypatch)
    fake_model(monkeypatch)
    candidate = client.post(path + "/vision", headers=owner,
                            json={"expected_revision": 2}).json()["vision"]
    with client.app.state.sessions.begin() as session:
        row = session.get(Material, material["id"])
        stored = {**row.parsed["vision"], "reading": {
            **row.parsed["vision"]["reading"], "fields": [field("manufacturer", "Lean Cuisine")]}}
        row.parsed = {**row.parsed, "vision": stored}
    response = client.post(path + "/vision/confirm", headers=headers(client, "reviewer"), json={
        "expected_revision": 3, "candidate_id": candidate["id"], "accepted_fields": [0],
        "corrected_regions": [{"field_index": 0, "box": [0, 0, 500, 500]}],
        "note": "旧候选未证明生产者身份"})
    assert response.status_code == 422
