"""任务C事实结构：合成边界用例与真实公告回归分开。"""

from pathlib import Path

import pytest
from quality.fact_candidates import FactItems, resolve_facts


def product(text, **attributes):
    return {"product_record": text, "product_record_attributes": attributes}


def supply(text, **attributes):
    return {"supply_claim": text, "supply_claim_attributes": attributes}


def resolve(text, *items):
    return resolve_facts(FactItems.model_validate({"extractions": list(items)}), text)


def test_real_midea_preserves_four_disjoint_ranges_and_article_number():
    path = Path("tests/fixtures/task05-c-midea-range-excerpt.txt")
    text = path.read_text(encoding="utf-8")
    excerpt = text[text.index("产品名称\n") : text.index("生产批号/批次\n")]
    dates = [
        ("2016年11月15日", "2016年12月6日"),
        ("2017年8月10日", "2017年9月3日"),
        ("2017年11月21日", "2017年12月14日"),
        ("2018年4月5日", "2018年5月22日"),
    ]
    result = resolve(
        text,
        product(
            excerpt,
            name="电热水壶",
            brand="演示家电",
            models=["DEMO-H517E2(货号ITEM517E2b)"],
            production_ranges=[{"start_text": a, "end_text": b} for a, b in dates],
        ),
    )
    row = result["products"][0]
    assert len(row["production_ranges"]) == 4
    assert row["production_ranges"][0]["start"]["normalized"] == "2016-11-15"
    assert row["production_ranges"][-1]["end"]["normalized"] == "2018-05-22"
    assert row["models"][0]["text"] == "DEMO-H517E2(货号ITEM517E2b)"
    assert result["status"] == "pending_quality_review"
    assert result["semantic_verified"] is False


def test_multiple_products_cannot_borrow_other_row_fields():
    one = "甲牌A1，生产日期2024年3月至2024年4月，合格。"
    two = "乙牌B2，生产日期2025年1月至2025年2月，不合格。"
    text = one + "\n" + two
    result = resolve(
        text,
        product(
            one,
            brand="甲牌",
            models=["A1"],
            conclusion="合格",
            production_ranges=[{"start_text": "2024年3月", "end_text": "2024年4月"}],
        ),
        product(
            two,
            brand="乙牌",
            models=["B2"],
            conclusion="不合格",
            production_ranges=[{"start_text": "2025年1月", "end_text": "2025年2月"}],
        ),
    )
    assert [r["brand"]["text"] for r in result["products"]] == ["甲牌", "乙牌"]
    assert result["products"][1]["production_ranges"][0]["start"]["precision"] == "month"
    with pytest.raises(ValueError, match="记录证据"):
        resolve(text, product(one, brand="乙牌", models=["A1"]))


def test_future_supply_keeps_recipient_condition_and_is_not_business_edge():
    text = "甲公司与乙公司约定，若完成认证，将于2027年供应动力电池。"
    result = resolve(
        text,
        supply(
            text,
            supplier="甲公司",
            product="动力电池",
            recipient="乙公司",
            status="commitment",
            valid_time="2027年",
            condition="若完成认证",
        ),
    )
    claim = result["supply_claims"][0]
    assert claim["status"] == "commitment"
    assert claim["recipient"]["text"] == "乙公司"
    assert claim["condition"]["text"] == "若完成认证"
    assert "business_edges" not in result


@pytest.mark.parametrize("status", ["reported_delivery", "commitment", "negated", "unknown"])
def test_source_claim_status_does_not_mean_confirmed_fact(status):
    text = "甲公司向乙公司供应电池。"
    result = resolve(text, supply(text, supplier="甲公司", product="电池", status=status))
    assert result["semantic_verified"] is False
    assert result["human_verified_count"] == 0


def test_ambiguous_quote_requires_alignment_instead_of_first_match():
    row = "甲牌A1。"
    text = row + "\n" + row
    item = product(row, brand="甲牌", models=["A1"])
    with pytest.raises(ValueError, match="位置不唯一"):
        resolve(text, item)
    result = resolve_facts(
        FactItems.model_validate({"extractions": [item]}),
        text,
        intervals=[(len(row) + 1, len(text))],
    )
    assert result["products"][0]["evidence"]["char_start"] == len(row) + 1


@pytest.mark.parametrize(
    "start,end",
    [
        ("2024年3月", "2024年2月"),
        ("2024年3月", "2024年3月1日"),
        ("2024年13月", "2024年14月"),
    ],
)
def test_invalid_date_range_is_not_silently_repaired(start, end):
    text = f"甲牌A1生产日期{start}至{end}。"
    with pytest.raises(ValueError):
        resolve(
            text,
            product(
                text,
                brand="甲牌",
                models=["A1"],
                production_ranges=[{"start_text": start, "end_text": end}],
            ),
        )


def test_all_duplicate_records_fail_instead_of_inflating_recall():
    text = "甲牌A1。"
    item = product(text, brand="甲牌", models=["A1"])
    with pytest.raises(ValueError, match="重复"):
        resolve(text, item, item)


def test_empty_candidates_are_explicit_abstention():
    result = resolve("未披露任何产品或供货情况。")
    assert result["status"] == "needs_info"
    assert not result["products"] and not result["supply_claims"]


def test_input_string_for_array_is_not_coerced():
    with pytest.raises(ValueError):
        FactItems.model_validate({"extractions": "[]"})


def test_date_prefix_cannot_turn_partial_month_into_whole_month():
    text = "甲牌A1生产日期2024年3月15日至2024年3月20日。"
    with pytest.raises(ValueError, match="完整日期"):
        resolve(
            text,
            product(
                text,
                brand="甲牌",
                models=["A1"],
                production_ranges=[
                    {"start_text": "2024年3月", "end_text": "2024年3月"},
                ],
            ),
        )


def test_direct_route_uses_version2_contract_with_same_source():
    import json
    from types import SimpleNamespace

    from quality.fact_extraction import direct_facts

    text = "甲牌A1。"
    requests = []

    def complete(messages):
        requests.append(messages)
        return {
            "message": {
                "content": json.dumps(
                    {
                        "extractions": [product(text, brand="甲牌", models=["A1"])],
                    }
                )
            }
        }

    result = direct_facts(SimpleNamespace(complete=complete), text)
    assert result["products"][0]["brand"]["text"] == "甲牌"
    assert requests[0][-1]["content"] == text


def test_langextract_route_runs_real_library_and_common_validator():
    import json
    from types import SimpleNamespace

    pytest.importorskip("langextract")
    from quality.fact_extraction import direct_facts, langextract_facts

    text = "甲牌A1。"
    requests = []

    def complete(messages):
        requests.append(messages)
        return {
            "message": {
                "content": json.dumps(
                    {
                        "extractions": [product(text, brand="甲牌", models=["A1"])],
                    }
                )
            }
        }

    model = SimpleNamespace(complete=complete)
    direct = direct_facts(model, text)
    aligned = langextract_facts(model, text)
    assert aligned.pop("alignment_counts") == {"exact": 1, "non_exact": 0}
    assert aligned == direct
    assert len(requests) == 2
