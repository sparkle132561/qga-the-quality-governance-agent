import pytest
from pydantic import ValidationError
from quality.vision import VisualField


@pytest.mark.parametrize("name,value,quote", [
    ("model", "K170", "MODEL K170"),
    ("net_quantity", "250g", "NET WT 250 g"),
    ("power", "1800W", "POWER 1800 W"),
    ("barcode", "1234567890123", "BARCODE 1234567890123"),
])
def test_whitespace_between_label_and_value_remains_a_token_boundary(name, value, quote):
    field = VisualField(name=name, value=value, quote=quote,
                        box=[0, 0, 1000, 1000], certainty="clear")
    assert field.quote == quote


@pytest.mark.parametrize("name,value,quote", [
    ("model", "K170", "MODEL K170S"),
    ("power", "1800W", "POWER 11800W"),
    ("barcode", "1234567890123", "BARCODE 12345678901234"),
])
def test_partial_identifiers_and_values_are_still_rejected(name, value, quote):
    with pytest.raises(ValidationError):
        VisualField(name=name, value=value, quote=quote,
                    box=[0, 0, 1000, 1000], certainty="clear")
