"""从已归档真实回答最小化的回归；不调用模型，不依赖私有原件缓存。"""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from quality.incidents import NormalizedIncident, normalize_incident
from quality.knowledge_graph import extract_graph

REPORT = Path(__file__).parents[1] / "tests/fixtures/extraction-boundaries.json"
RECORDED_RUNS = json.loads(REPORT.read_bytes())["runs"]


def recorded(case_id, repetition=0):
    return next(
        r for r in RECORDED_RUNS if r["case_id"] == case_id and r["repetition"] == repetition
    )["body"]


def replay_graph(body, text):
    return extract_graph(
        SimpleNamespace(
            complete=lambda *args: {
                "message": {"content": json.dumps(body)},
                "engine_mode": "captured_replay",
            }
        ),
        text,
    )[0]


def test_real_baseus_brand_designator_is_not_part_of_normalized_brand():
    original = recorded("power-brand")["proposal"]
    text = original["field_evidence"]["brand"]["quote"]
    body = NormalizedIncident(
        brand=original["brand"],
        models=original["models"],
        field_evidence={key: {"quote": text} for key in ("brand", "models")},
    )
    result = normalize_incident(body, text)
    assert result["brand"] == "演示电源"
    assert result["field_normalization"]["brand"]["original"] == "演示电源牌"
    assert result["field_evidence"]["brand"]["quote"] == text
    from quality.impact import product_exclusion

    assert product_exclusion(
        result, SimpleNamespace(brand="演示电源", model="DEMO-30KP365")
    ) is None
    canonical = {key: result[key] for key in NormalizedIncident.model_fields}
    second = normalize_incident(NormalizedIncident.model_validate(canonical), text)
    assert second["field_normalization"] == result["field_normalization"]


def test_real_midea_article_number_is_separate_from_model():
    original = recorded("appliance-model")["proposal"]
    text = original["field_evidence"]["models"]["quote"]
    result = normalize_incident(
        NormalizedIncident(models=original["models"], field_evidence={"models": {"quote": text}}),
        text,
    )
    assert result["models"] == ["DEMO-H517E2"]
    assert result["field_normalization"]["models"][0]["article_number"] == "ITEM517E2b"
    assert result["field_evidence"]["models"]["quote"] == text
    canonical = {key: result[key] for key in NormalizedIncident.model_fields}
    second = normalize_incident(NormalizedIncident.model_validate(canonical), text)
    assert second["field_normalization"] == result["field_normalization"]


def test_real_tesla_agreement_is_not_an_actual_order():
    original = recorded("future-agreement")["body"]
    relation = next(r for r in original["relations"] if r["predicate"] == "ordered_from")
    keys = {relation["subject"], relation["object"]}
    body = {
        "entities": [e for e in original["entities"] if e["key"] in keys],
        "relations": [relation],
    }
    with pytest.raises(ValueError):
        replay_graph(body, relation["quote"])


def test_real_battery_supplier_is_not_a_vehicle_supplier():
    original = recorded("battery-supply", 2)["body"]
    vehicle = next(e for e in original["entities"] if e["name"] == "环卫车")
    relation = next(r for r in original["relations"] if r["object"] == vehicle["key"])
    keys = {relation["subject"], relation["object"]}
    body = {
        "entities": [e for e in original["entities"] if e["key"] in keys],
        "relations": [relation],
    }
    result = replay_graph(body, relation["quote"])
    entities = {e["key"]: e for e in result["entities"]}
    assert all(entities[r["object"]]["name"] == "动力电池" for r in result["relations"])


def test_real_empty_relation_candidate_cannot_be_confirmable_success():
    body = copy.deepcopy(recorded("empty-relations")["body"])
    # 此坏例只有实体、无关系；原文用已保存的品牌／型号事实最小化。
    text = "演示制造商甲公司\n电热水壶\n演示壶具\nBM-251\n2023年02月08日\n800台"
    with pytest.raises(ValueError):
        replay_graph(body, text)


def test_multiple_ranges_clear_expanded_endpoints_instead_of_only_flagging():
    text = "生产起止日期\n2016年11月15日至2016年12月6日、\n2018年4月5日至2018年5月22日"
    body = NormalizedIncident(
        production_scope="range",
        production_start="2016-11-15",
        production_end="2018-05-22",
        field_evidence={
            "production_start": {"quote": "2016年11月15日至2016年12月6日、"},
            "production_end": {"quote": "2018年4月5日至2018年5月22日"},
        },
    )
    result = normalize_incident(body, text)
    assert result["production_scope"] == "unknown"
    assert result["production_start"] is None and result["production_end"] is None
    assert result["date_precision"] == "unknown" and result["scope_review_required"]


def test_single_production_range_uses_proven_endpoints_to_resolve_unknown_scope():
    text = "生产起止日期\n2018年12月-2019年6月"
    body = NormalizedIncident(
        production_scope="unknown",
        production_start="2018-12",
        production_end="2019-06",
        field_evidence={
            key: {"quote": "2018年12月-2019年6月"} for key in ("production_start", "production_end")
        },
    )
    result = normalize_incident(body, text)
    assert result["production_scope"] == "range" and result["date_precision"] == "month"


def test_explicit_delivered_supply_sentence_does_not_require_a_model_call():
    text = recorded("battery-supply", 1)["body"]["relations"][0]["quote"]

    def forbidden(*args):
        raise AssertionError("明确句式不应调用模型")

    result, response = extract_graph(SimpleNamespace(complete=forbidden), text)
    assert response["engine_mode"] == "source_rule"
    assert [r["predicate"] for r in result["relations"]] == ["supplies"]
    assert {e["name"] for e in result["entities"]} == {"演示电池公司", "动力电池"}


def test_single_product_batch_table_does_not_invent_a_supplier_or_require_model():
    text = (
        "生产者名称\n演示制造商甲公司\n产品名称\n电热水壶\n品牌\n演示壶具\n"
        "型号/规格\nBM-251\n生产批号/批次\n第一批次"
    )

    def forbidden(*args):
        raise AssertionError("单产品明确批次不应调用模型")

    result, response = extract_graph(SimpleNamespace(complete=forbidden), text)
    assert response["engine_mode"] == "source_rule"
    assert [r["predicate"] for r in result["relations"]] == ["has_batch"]
    assert not any(e["kind"] == "supplier" for e in result["entities"])


@pytest.mark.parametrize(
    "text",
    [
        "甲公司将向乙公司配套19GWh动力电池。",
        "无法确认甲公司已向乙公司配套19GWh动力电池。",
        "产品名称\n电热水壶\n批次\nB01\n产品名称\n电饭锅\n批次\nB02",
        "产品名称\n电热水壶\n生产批号/批次\n/",
    ],
)
def test_explicit_rules_abstain_on_future_uncertainty_multiple_records_or_missing_batch(text):
    from quality.explicit_graph import explicit_graph

    assert explicit_graph(text) is None


def test_publication_range_is_not_used_to_resolve_production_scope():
    text = "发布日期\n2018年12月-2019年6月"
    body = NormalizedIncident(
        production_scope="unknown",
        production_start="2018-12",
        production_end="2019-06",
        field_evidence={
            key: {"quote": "2018年12月-2019年6月"} for key in ("production_start", "production_end")
        },
    )
    assert normalize_incident(body, text)["production_scope"] == "unknown"


def test_recipient_named_in_supply_sentence_is_dropped_as_supplier():
    from quality.knowledge_graph import ExtractedGraph, validate_graph

    text = "演示电池公司已向演示车辆公司配套19GWh动力电池。"
    body = {
        "entities": [
            {"key": "s", "kind": "supplier", "name": "演示车辆公司"},
            {"key": "p", "kind": "product", "name": "动力电池"},
        ],
        "relations": [{"subject": "s", "predicate": "supplies", "object": "p", "quote": text}],
    }
    result = validate_graph(ExtractedGraph.model_validate(body), text)
    assert result["relations"] == []
    assert result["dropped_relations"][0]["reason"] == "supply_not_reported_for_object"


def test_brand_name_ending_in_pai_in_explicit_brand_field_is_preserved():
    text = "品牌：扑克牌\n型号：P1"
    result = normalize_incident(
        NormalizedIncident(
            brand="扑克牌",
            models=["P1"],
            field_evidence={key: {"quote": text} for key in ("brand", "models")},
        ),
        text,
    )
    assert result["brand"] == "扑克牌"


def test_brand_name_itself_containing_pinpai_is_not_truncated():
    text = "召回演示品牌K170"
    result = normalize_incident(
        NormalizedIncident(
            brand="演示品牌",
            models=["K170"],
            field_evidence={key: {"quote": text} for key in ("brand", "models")},
        ),
        text,
    )
    assert result["brand"] == "演示品牌"


def test_model_parenthesized_version_is_not_mistaken_for_article_number():
    text = "型号X1-Pro(2023)"
    result = normalize_incident(
        NormalizedIncident(models=["X1-Pro(2023)"], field_evidence={"models": {"quote": text}}),
        text,
    )
    assert result["models"] == ["X1-Pro(2023)"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("甲公司已向乙公司供应P1。", True),
        ("甲公司生产P1。", False),
        ("甲公司没有向乙公司供应P1。", False),
        ("甲公司拟与乙公司签订协议，将供应P1。", False),
    ],
)
def test_supplies_requires_positive_object_specific_fulfillment_evidence(text, expected):
    body = {
        "entities": [
            {"key": "s", "kind": "supplier", "name": "甲公司"},
            {"key": "p", "kind": "product", "name": "P1"},
        ],
        "relations": [{"subject": "s", "predicate": "supplies", "object": "p", "quote": text}],
    }
    if expected:
        assert replay_graph(body, text)["relations"]
    else:
        result = replay_graph(body, text)
        assert result["relations"] == []
        assert result["dropped_relations"][0]["reason"] == "supply_not_reported_for_object"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("批次B01对应订单O01。", True),
        ("批次B01计划生成订单O01。", False),
    ],
)
def test_order_edge_uses_existing_batch_to_order_direction_and_actual_order(text, expected):
    body = {
        "entities": [
            {"key": "b", "kind": "batch", "name": "B01"},
            {"key": "o", "kind": "order", "name": "O01"},
        ],
        "relations": [{"subject": "b", "predicate": "ordered_from", "object": "o", "quote": text}],
    }
    if expected:
        assert replay_graph(body, text)["relations"]
    else:
        result = replay_graph(body, text)
        assert result["relations"] == []
        assert result["dropped_relations"][0]["reason"] == "order_not_actual"


@pytest.mark.parametrize("product", ["不二家饼干", "将军香烟"])
def test_words_inside_product_name_are_not_treated_as_negation_or_future(product):
    text = "甲公司已供应" + product + "。"
    body = {
        "entities": [
            {"key": "s", "kind": "supplier", "name": "甲公司"},
            {"key": "p", "kind": "product", "name": product},
        ],
        "relations": [{"subject": "s", "predicate": "supplies", "object": "p", "quote": text}],
    }
    assert replay_graph(body, text)["relations"]


def test_legacy_pending_wrong_order_cannot_be_confirmed_but_can_be_rejected(client):
    import hashlib

    from conftest import headers
    from quality.models import EvidenceChunk, GraphExtraction
    from quality.retrieval import index_units
    from sqlalchemy import select

    original = recorded("future-agreement")["body"]
    relation = next(r for r in original["relations"] if r["predicate"] == "ordered_from")
    text = relation["quote"]
    keys = {relation["subject"], relation["object"]}
    original = {
        "entities": [e for e in original["entities"] if e["key"] in keys],
        "relations": [relation],
    }
    owner, reviewer = headers(client), headers(client, "reviewer")
    case = client.post(
        "/api/cases", headers=owner, json={"title": "旧协议候选", "workflow": "qualification"}
    ).json()
    sha = hashlib.sha256(text.encode()).hexdigest()
    with client.app.state.sessions.begin() as session:
        index_units(
            session,
            "case:" + case["id"],
            [{"text": text, "location": {}}],
            sha,
            False,
            case_id=case["id"],
        )
        chunk_id = session.scalar(
            select(EvidenceChunk.id).where(EvidenceChunk.case_id == case["id"])
        )
        record = GraphExtraction(
            chunk_id=chunk_id,
            source_sha256=sha,
            case_id=case["id"],
            case_revision=case["revision"],
            body=original,
            location={},
            is_demo=False,
            engine_mode="captured_replay",
            created_by="merchant-a",
        )
        session.add(record)
        session.flush()
        identifier = record.id
    path = f"/api/graph/extractions/{identifier}/review"
    payload = {"decision": "confirmed", "as_of": "2025-07-01", "note": "核对真实协议旧候选"}
    response = client.post(path, headers=reviewer, json=payload)
    assert response.status_code == 422, response.text
    assert "端点类型" in response.json()["detail"]
    with client.app.state.sessions() as session:
        assert session.get(GraphExtraction, identifier).status == "pending"
    response = client.post(path, headers=reviewer, json={**payload, "decision": "rejected"})
    assert response.status_code == 200, response.text


AGREEMENT_TEXT = (
    "演示电池公司与演示客户公司签署合作框架协议。\n"
    "演示电池公司将向演示客户公司供应锂离子动力电池。\n"
    "双方约定供货细节另行签订订单。"
)


def test_model_chinese_date_literals_are_normalized_to_iso():
    from quality.incidents import parse_incident

    text = "佛山市顺德区唯铭德电器有限公司召回2018年12月至2019年6月期间生产的UMD-L520型号电热水壶。"
    content = json.dumps(
        {
            "event_type": "recall",
            "brand": "唯铭德",
            "models": ["UMD-L520"],
            "production_scope": "range",
            "production_start": "2018年12月",
            "production_end": "2019年6月",
            "field_evidence": {
                "event_type": {"quote": "召回"},
                "brand": {"quote": "唯铭德"},
                "models": {"quote": "UMD-L520型号电热水壶"},
                "production_scope": {"quote": "2018年12月至2019年6月"},
                "production_start": {"quote": "2018年12月至2019年6月"},
                "production_end": {"quote": "2018年12月至2019年6月"},
            },
        },
        ensure_ascii=False,
    )
    result = parse_incident(content, text)
    assert result["production_start"] == "2018-12"
    assert result["production_end"] == "2019-06"


def test_model_string_null_literals_become_null():
    from quality.field_normalization import normalize_output_literals

    value = normalize_output_literals(
        {"brand": "None", "production_start": " null ", "defect_summary": "NONE"}
    )
    assert value["brand"] is None
    assert value["production_start"] is None
    assert value["defect_summary"] is None
    kept = normalize_output_literals({"brand": "演示家电", "production_start": "2020-01"})
    assert kept == {"brand": "演示家电", "production_start": "2020-01"}


def test_agreement_supply_relation_is_dropped_not_fatal():
    body = {
        "entities": [
            {"key": "supplier", "kind": "supplier", "name": "演示电池公司"},
            {"key": "battery", "kind": "product", "name": "锂离子动力电池"},
        ],
        "relations": [
            {
                "subject": "supplier",
                "predicate": "supplies",
                "object": "battery",
                "evidence": {"line_start": 2, "line_end": 2},
            }
        ],
    }
    result = replay_graph(body, AGREEMENT_TEXT)
    assert result["relations"] == []
    assert result["dropped_relations"][0]["reason"] == "supply_not_reported_for_object"
    assert "待人工复核" in result["extraction_notes"]


def test_relation_endpoint_name_is_resolved_to_unique_key():
    body = {
        "entities": [
            {"key": "supplier", "kind": "supplier", "name": "演示电池公司"},
            {"key": "battery", "kind": "product", "name": "锂离子动力电池"},
        ],
        "relations": [
            {
                "subject": "演示电池公司",
                "predicate": "supplies",
                "object": "锂离子动力电池",
                "evidence": {"line_start": 2, "line_end": 2},
            }
        ],
    }
    result = replay_graph(body, AGREEMENT_TEXT)
    assert result["relations"] == []
    assert result["dropped_relations"][0]["reason"] == "supply_not_reported_for_object"


def test_endpoint_type_violation_still_rejects_whole_graph():
    from quality.graph_semantics import GraphSemanticError

    body = {
        "entities": [
            {"key": "buyer", "kind": "organization", "name": "演示电池公司"},
            {"key": "battery", "kind": "product", "name": "锂离子动力电池"},
        ],
        "relations": [
            {
                "subject": "buyer",
                "predicate": "supplies",
                "object": "battery",
                "evidence": {"line_start": 2, "line_end": 2},
            }
        ],
    }
    with pytest.raises(GraphSemanticError) as error:
        replay_graph(body, AGREEMENT_TEXT)
    assert error.value.reason == "relation_endpoint_types"


def test_native_empty_relations_still_fail_without_drop_record():
    from quality.graph_semantics import GraphSemanticError

    body = {
        "entities": [{"key": "supplier", "kind": "supplier", "name": "演示电池公司"}],
        "relations": [],
    }
    with pytest.raises(GraphSemanticError) as error:
        replay_graph(body, AGREEMENT_TEXT)
    assert error.value.reason == "no_supported_relations"


def test_null_line_evidence_for_empty_fields_is_treated_as_absent():
    from quality.incidents import parse_incident

    text = "佛山市顺德区唯铭德电器有限公司召回2018年12月至2019年6月期间生产的UMD-L520型号电热水壶。"
    content = json.dumps(
        {
            "event_type": "recall",
            "brand": "唯铭德",
            "models": ["UMD-L520"],
            "production_scope": "range",
            "production_start": "2018-12",
            "production_end": "2019-06",
            "batches": [],
            "serial_scope_original": None,
            "field_evidence": {
                "event_type": {"line_start": 1, "line_end": 1},
                "brand": {"quote": "唯铭德"},
                "models": {"quote": "UMD-L520型号电热水壶"},
                "production_scope": {"quote": "2018年12月至2019年6月"},
                "production_start": {"quote": "2018年12月至2019年6月"},
                "production_end": {"quote": "2018年12月至2019年6月"},
                "batches": {"line_start": None, "line_end": None},
                "serial_scope_original": None,
            },
        },
        ensure_ascii=False,
    )
    result = parse_incident(content, text)
    assert result["production_start"] == "2018-12"
    assert "batches" not in result["field_evidence"] or not result["field_evidence"]["batches"]


def test_null_line_evidence_for_filled_field_still_requires_quote():
    from quality.incidents import parse_incident

    text = "某公司召回2020年1月生产的P2型号产品。"
    content = json.dumps(
        {
            "event_type": "recall",
            "brand": "某公司",
            "production_start": "2020-01",
            "field_evidence": {
                "event_type": {"quote": "召回"},
                "brand": {"quote": "某公司"},
                "production_start": {"line_start": None, "line_end": None},
            },
        },
        ensure_ascii=False,
    )
    result = parse_incident(content, text)
    assert result["brand"] == "某公司"
    assert result["production_start"] is None
    assert result["_field_review"]["fields"]["production_start"]["status"] == "invalid"
    assert result["scope_review_required"] is True
