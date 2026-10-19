import pytest
from conftest import headers
from pydantic import ValidationError
from quality.models import Material
from quality.vision import VisualField
from test_vision import fake_model, reading, setup_image


@pytest.mark.parametrize("name,value,quote", [
    ("brand", "Nut Some", "NUT SOME"),
    ("product_name", "Frozen Dairy Dessert", "FROZEN DAIRY DESSERT"),
    ("manufacturer", "Acme Ltd", "Manufacturer: ACME LTD"),
])
def test_name_casing_does_not_invent_or_remove_visible_letters(name, value, quote):
    assert VisualField(name=name, value=value, quote=quote,
                       box=[0, 0, 1000, 1000], certainty="clear").quote == quote


def test_identifiers_remain_case_sensitive():
    with pytest.raises(ValidationError):
        VisualField(name="model", value="k170", quote="MODEL K170",
                    box=[0, 0, 1000, 1000], certainty="clear")


def test_partial_reading_keeps_valid_candidate_and_rejections_out_of_evidence(client, monkeypatch):
    case, item, path, owner = setup_image(client, monkeypatch)
    proposal = reading()
    proposal["fields"].append({"name": "power", "value": "12%", "quote": "Alc. 12%",
                               "box": [0, 0, 1000, 1000], "certainty": "clear"})
    fake_model(monkeypatch, proposal)
    response = client.post(path + "/vision", headers=owner, json={"expected_revision": 2})
    assert response.status_code == 200
    candidate = response.json()["vision"]
    assert candidate["reading_validation"] == "partial"
    assert [field["name"] for field in candidate["reading"]["fields"]] == ["model"]
    assert candidate["rejected_fields"][0]["proposal"]["value"] == "12%"
    confirmed = client.post(path + "/vision/confirm", headers=headers(client, "reviewer"), json={
        "expected_revision": 3, "candidate_id": candidate["id"], "accepted_fields": [0],
        "note": "仅核验有效型号；拒绝的功率仍需复核",
    })
    assert confirmed.status_code == 200
    with client.app.state.sessions() as session:
        material = session.get(Material, item["id"])
        assert material.status == "needs_info"
        assert all(field["value"] != "12%" for field in material.parsed["fields"])


def test_no_valid_fields_cannot_be_promoted_to_partial_success(client, monkeypatch):
    _, _, path, owner = setup_image(client, monkeypatch)
    proposal = reading(value="K170")
    proposal["fields"][0]["quote"] = "MODEL K170S"
    fake_model(monkeypatch, proposal)
    assert client.post(path + "/vision", headers=owner,
                       json={"expected_revision": 2}).status_code == 422
