"""从OFF development划分发现的数量表达；真实图片测试划分不用于选择通过样例。"""
import pytest
from conftest import headers
from quality.cleaning import normalize_quantity
from quality.models import ReviewCase
from quality.qualification import qualification_snapshot


@pytest.mark.parametrize("raw,value,unit", [
    ("1pcs", "1", "piece"),  # OFF 42033950，development。
    ("4pcs", "4", "piece"),  # OFF 69088018，development。
    ("16 oz (454 g)", "454", "g"),  # OFF 0041570144107：采用明示公制值，不猜换算。
])
def test_real_declared_quantity_formats(raw, value, unit):
    normalized = normalize_quantity(raw)
    assert normalized["status"] == "normalized"
    assert (normalized["value"], normalized["unit"]) == (value, unit)
    assert normalized["raw"] == raw


def test_real_uncertainty_sign_and_fractional_piece_are_not_erased():
    # OFF 5400141888362，development；±不是估计重量符号℮，不能误删。
    assert normalize_quantity("±660g")["status"] == "needs_review"
    assert normalize_quantity("0.5pcs")["status"] == "needs_review"


@pytest.mark.parametrize("second,expected_conflict", [
    ("500 g", False), ("600 g", True), ("500 ml", True),
])
def test_equivalent_quantities_do_not_create_false_material_conflicts(
        client, second, expected_conflict):
    owner = headers(client)
    case = client.post("/api/cases", headers=owner,
                       json={"title": "数量等价核对", "workflow": "qualification"}).json()
    path = f"/api/cases/{case['id']}/materials"
    client.post(path, headers=owner, files={
        "file": ("quantity.csv", ("quantity,brand\n0.5 kg,Demo\n" + second
                                   + ",Demo\n").encode(), "text/csv")
    }).raise_for_status()
    with client.app.state.sessions() as session:
        snapshot = qualification_snapshot(session, session.get(ReviewCase, case["id"]))
    assert any(row["field"] == "net_quantity"
               for row in snapshot["checks"]["conflicts"]) is expected_conflict
