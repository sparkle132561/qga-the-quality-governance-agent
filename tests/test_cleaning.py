from copy import deepcopy

import pytest
from quality.cleaning import clean_units, normalize_quantity
from quality.parsing import parse_material


def test_cleaning_keeps_raw_text_and_repeated_values_with_audit():
    units = [{"text": "ＭＯＤＥＬ：　K170\t 1800W", "location": {"page": 1},
              "confidence": 0.5},
             {"text": "ＭＯＤＥＬ：　K170\t 1800W", "location": {"page": 2},
              "confidence": 0.95}]
    original = deepcopy(units)
    result = clean_units(units)
    assert units == original
    assert len(result["units"]) == 2
    assert result["units"][0]["text"] == "MODEL: K170 1800W"
    assert result["units"][0]["operations"] == ["unicode_nfkc", "whitespace"]
    assert result["issues"][0]["unit_index"] == 0 and result["review_required"]


@pytest.mark.parametrize("raw,amount,unit", [
    ("2 x 125 g", "250", "g"), ("1,5 L", "1500", "ml"),
    ("５０ cl", "500", "ml"), ("250mg", "0.25", "g"),
])
def test_quantity_metric_conversion(raw, amount, unit):
    result = normalize_quantity(raw)
    assert result["status"] == "normalized"
    assert (result["value"], result["unit"]) == (amount, unit)
    assert result["raw"] == raw


@pytest.mark.parametrize("raw", ["1,000g", "1.000g", "16 oz", "100", "0 g", "-1g"])
def test_ambiguous_or_unsupported_quantities_are_not_guessed(raw):
    assert normalize_quantity(raw)["status"] == "needs_review"


def test_material_quantity_preserves_raw_field_and_attaches_normalization():
    result = parse_material(b"<p>NET WEIGHT: 2 x 125 g</p>", ".html")
    field = result["fields"][0]
    assert field["value"] == "2 x 125 g"
    assert field["normalization"]["value"] == "250"
    assert field["normalization"]["unit"] == "g"
    assert result["units"][0]["text"] == "NET WEIGHT: 2 x 125 g"
