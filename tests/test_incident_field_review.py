import copy
import json

import pytest
from conftest import headers
from test_impact import fake_impact
from test_incidents import PROPOSAL, TEXT, create_draft


def extract_candidate(client, monkeypatch, candidate, text=TEXT):
    row = create_draft(client, text)
    monkeypatch.setattr(
        "quality.incidents.AgnesClient.complete",
        lambda *args: {
            "message": {"content": json.dumps(candidate, ensure_ascii=False)},
            "engine_mode": "fake",
            "usage": {},
            "elapsed_ms": 0,
        },
    )
    response = client.post(
        f"/api/incidents/drafts/{row['id']}/extract",
        headers=headers(client, "reviewer"),
        json={"expected_revision": row["revision"]},
    )
    return row, response


def test_invalid_date_retains_brand_model_and_field_evidence(client, monkeypatch):
    candidate = copy.deepcopy(PROPOSAL)
    candidate["production_start"] = "2025-02-01"
    row, response = extract_candidate(client, monkeypatch, candidate)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "needs_info" and result["event_id"] is None
    assert result["proposal"]["brand"] == "演示品牌"
    assert result["proposal"]["models"] == ["K170"]
    assert result["proposal"]["production_start"] is None
    assert result["proposal"]["production_end"] is None
    assert result["proposal"]["production_scope"] == "unknown"
    review = result["field_review"]
    assert review["source_sha256"] == row["source_sha256"]
    assert review["extracted_revision"] == result["revision"]
    fields = review["fields"]
    assert fields["brand"]["status"] == "valid"
    assert fields["models"]["normalized_value"] == ["K170"]
    assert fields["production_start"]["raw_candidate"] == "2025-02-01"
    assert fields["production_start"]["status"] == "invalid"
    assert fields["production_start"]["failure_reason"]
    evidence = fields["brand"]["evidence"]
    assert TEXT[evidence["char_start"] : evidence["char_end"]] == "演示品牌"
    stored = next(
        item
        for item in client.get("/api/incidents/drafts", headers=headers(client, "reviewer")).json()
        if item["id"] == row["id"]
    )
    assert stored["field_review"] == review


def test_confirming_a_correction_keeps_original_extraction_review(client, monkeypatch):
    candidate = copy.deepcopy(PROPOSAL)
    candidate["production_start"] = "2025-02-01"
    row, extracted = extract_candidate(client, monkeypatch, candidate)
    assert extracted.status_code == 200, extracted.text
    result = client.post(
        f"/api/incidents/drafts/{row['id']}/confirm",
        headers=headers(client, "reviewer"),
        json={
            "expected_revision": extracted.json()["revision"],
            "proposal": PROPOSAL,
            "note": "生产日起点已按原文修正",
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["proposal"]["production_start"] == "2025-01-01"
    assert result.json()["field_review"] == extracted.json()["field_review"]
    assert result.json()["proposal"]["scope_review_required"] is False


def test_omitting_an_invalid_batch_cannot_make_the_scope_executable(client, monkeypatch):
    candidate = copy.deepcopy(PROPOSAL)
    candidate["batches"] = ["B01"]
    candidate["field_evidence"]["batches"] = {"quote": "原文没有的批次"}
    row, extracted = extract_candidate(client, monkeypatch, candidate)
    assert extracted.status_code == 200, extracted.text
    reviewer = headers(client, "reviewer")
    confirmed = client.post(
        f"/api/incidents/drafts/{row['id']}/confirm",
        headers=reviewer,
        json={
            "expected_revision": extracted.json()["revision"],
            "proposal": PROPOSAL,
            "note": "只清空批次，未解决原候选问题",
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["proposal"]["scope_review_required"] is True
    assert "field_review:batches" in confirmed.json()["proposal"]["unresolved_fields"]
    owner = headers(client)
    created = client.post(
        "/api/cases",
        headers=owner,
        json={
            "title": "错误批次不能解除限制",
            "workflow": "incident",
            "payload": {"event_id": confirmed.json()["event_id"], "as_of": "2025-07-01"},
        },
    )
    assert created.status_code == 201, created.text
    path = "/api/cases/" + created.json()["id"]
    fake_impact(monkeypatch)
    assert client.post(path + "/submit", headers=owner).status_code == 200
    analyzed = client.post(path + "/analyze", headers=reviewer)
    assert analyzed.status_code == 200, analyzed.text
    assert analyzed.json()["recommendation"] == "needs_info"
    assert analyzed.json()["impact"]["affected_product_ids"] == []
    assert analyzed.json()["impact"]["unknown_product_ids"] == ["sku-hit"]
    assert client.get(path + "/effects", headers=owner).json() == []


def test_adjacent_product_dates_cannot_be_confirmed_for_another_model(client, monkeypatch):
    text = (
        "召回公告\n产品：甲\n品牌：演示品牌\n型号：K170\n"
        "产品：乙\n品牌：其他品牌\n型号：M200\n生产日期2025-01-01至2025-04-30。"
    )
    row, response = extract_candidate(client, monkeypatch, PROPOSAL, text)
    assert response.status_code == 422, response.text
    manual = copy.deepcopy(PROPOSAL)
    manual["field_evidence"].pop("defect_summary")
    manual["defect_summary"] = None
    manual["field_evidence"]["models"] = {"quote": "型号：K170"}
    confirmed = client.post(
        f"/api/incidents/drafts/{row['id']}/confirm",
        headers=headers(client, "reviewer"),
        json={
            "expected_revision": row["revision"],
            "proposal": manual,
            "note": "型号甲搭配产品乙日期，必须拒绝",
        },
    )
    assert confirmed.status_code == 422, confirmed.text


def test_inline_product_labels_cannot_borrow_another_products_dates(client, monkeypatch):
    text = (
        "召回公告\n产品甲，品牌：演示品牌，型号：K170。\n"
        "产品乙，品牌：其他品牌，型号：M200，生产日期2025-01-01至2025-04-30。"
    )
    candidate = copy.deepcopy(PROPOSAL)
    candidate["defect_summary"] = None
    candidate["field_evidence"].pop("defect_summary")
    candidate["field_evidence"]["models"] = {"quote": "型号：K170"}
    row, extracted = extract_candidate(client, monkeypatch, candidate, text)
    assert extracted.status_code == 422, extracted.text
    confirmed = client.post(
        f"/api/incidents/drafts/{row['id']}/confirm", headers=headers(client, "reviewer"),
        json={"expected_revision": row["revision"], "proposal": candidate,
              "note": "行内多产品的日期不能互借"},
    )
    assert confirmed.status_code == 422, confirmed.text


@pytest.mark.parametrize("mutation", ["source", "text"])
def test_changed_source_or_original_text_invalidates_field_confirmation(
    client, monkeypatch, mutation
):
    from quality.models import IncidentDraft, SourceRecord

    row, extracted = extract_candidate(client, monkeypatch, PROPOSAL)
    assert extracted.status_code == 200, extracted.text
    with client.app.state.sessions.begin() as session:
        if mutation == "source":
            session.get(SourceRecord, row["source_id"]).url = "https://example.invalid/changed"
        else:
            session.get(IncidentDraft, row["id"]).raw_text = TEXT + "原文已变更"
    confirmed = client.post(
        f"/api/incidents/drafts/{row['id']}/confirm",
        headers=headers(client, "reviewer"),
        json={
            "expected_revision": extracted.json()["revision"],
            "proposal": PROPOSAL,
            "note": "旧版本不得继续确认",
        },
    )
    assert confirmed.status_code == 409, confirmed.text


def test_disjoint_dates_preserve_candidates_as_unresolved_fields(client, monkeypatch):
    text = TEXT + "另一个生产日期区间2026-01-01至2026-02-01。"
    _, response = extract_candidate(client, monkeypatch, PROPOSAL, text)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["proposal"]["production_start"] is None
    date_review = result["field_review"]["fields"]["production_start"]
    assert date_review["raw_candidate"] == "2025-01-01"
    assert date_review["normalized_value"] == "2025-01-01"
    assert date_review["status"] == "needs_review"
    assert date_review["failure_reason"]


@pytest.mark.parametrize(
    "field,value,evidence",
    [
        ("brand", {"bad": "type"}, {"quote": "演示品牌"}),
        ("models", "K170", {"quote": "K170"}),
        ("models", ["K170S"], {"quote": "K170"}),
        ("batches", ["B01"], {"quote": "原文没有的批次"}),
        ("brand", "演示品牌", {"line_start": 900, "line_end": 901}),
        ("brand", "演示品牌", {"quote": "演示品牌", "char_start": 0}),
        ("production_start", ["2025-01-01"], {"quote": "2025-01-01"}),
        ("production_start", "2025-01", {"quote": "2025-01"}),
    ],
)
def test_invalid_field_is_isolated_from_other_identity_fields(
    client,
    monkeypatch,
    field,
    value,
    evidence,
):
    candidate = copy.deepcopy(PROPOSAL)
    candidate[field] = value
    candidate["field_evidence"][field] = evidence
    _, response = extract_candidate(client, monkeypatch, candidate)
    assert response.status_code == 200, response.text
    result = response.json()
    failed = result["field_review"]["fields"][field]
    assert failed["status"] == "invalid" and failed["failure_reason"]
    assert failed["raw_candidate"] == value
    assert result["proposal"]["scope_review_required"] is True
    if field != "brand":
        assert result["proposal"]["brand"] == "演示品牌"
    if field != "models":
        assert result["proposal"]["models"] == ["K170"]
    assert result["event_id"] is None


@pytest.mark.parametrize(
    "candidate",
    [
        {},
        [],
        "not an object",
        {"field_evidence": []},
        {"unknown_property": "bad"},
        {"brand": "演示品牌", "field_evidence": {"foreign_key": {"quote": "召回"}}},
    ],
)
def test_broken_record_envelope_remains_an_explicit_failure(client, monkeypatch, candidate):
    row, response = extract_candidate(client, monkeypatch, candidate)
    assert response.status_code == 422, response.text
    stored = next(
        item
        for item in client.get("/api/incidents/drafts", headers=headers(client, "reviewer")).json()
        if item["id"] == row["id"]
    )
    assert stored["event_id"] is None
    assert stored["last_error"] == "invalid_scope_evidence"


def test_raw_date_literal_and_safe_normalization_are_both_saved(client, monkeypatch):
    candidate = copy.deepcopy(PROPOSAL)
    candidate["production_start"] = "2025年01月01日"
    _, response = extract_candidate(client, monkeypatch, candidate)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending_confirmation"
    reviewed = response.json()["field_review"]["fields"]["production_start"]
    assert reviewed["raw_candidate"] == "2025年01月01日"
    assert reviewed["normalized_value"] == "2025-01-01"
    assert reviewed["status"] == "valid"


def test_source_change_during_extraction_does_not_leave_an_active_run(client, monkeypatch):
    from quality.models import SourceRecord

    row = create_draft(client)

    def changed(*args):
        with client.app.state.sessions.begin() as session:
            session.get(SourceRecord, row["source_id"]).url = "https://example.invalid/new"
        return {"message": {"content": json.dumps(PROPOSAL)}, "engine_mode": "fake"}

    monkeypatch.setattr("quality.incidents.AgnesClient.complete", changed)
    result = client.post(
        f"/api/incidents/drafts/{row['id']}/extract",
        headers=headers(client, "reviewer"),
        json={"expected_revision": row["revision"]},
    )
    assert result.status_code == 409, result.text
    stored = next(
        item
        for item in client.get("/api/incidents/drafts", headers=headers(client, "reviewer")).json()
        if item["id"] == row["id"]
    )
    assert stored["status"] == "needs_info"
    assert stored["last_error"] == "source_changed"
    assert stored["event_id"] is None


def test_publication_date_cannot_be_used_as_a_production_range_endpoint(client, monkeypatch):
    text = "公告发布日期2024-01-01。\n" + TEXT
    candidate = copy.deepcopy(PROPOSAL)
    candidate["production_start"] = "2024-01-01"
    candidate["field_evidence"]["production_start"] = {"quote": "公告发布日期2024-01-01"}
    row, response = extract_candidate(client, monkeypatch, candidate, text)
    assert response.status_code == 200, response.text
    assert response.json()["proposal"]["production_start"] is None
    assert response.json()["proposal"]["production_end"] is None
    assert response.json()["field_review"]["fields"]["production_start"]["status"] == "needs_review"
    confirmed = client.post(
        f"/api/incidents/drafts/{row['id']}/confirm",
        headers=headers(client, "reviewer"),
        json={
            "expected_revision": response.json()["revision"],
            "proposal": candidate,
            "note": "不能把发布时间和生产日期拼成区间",
        },
    )
    assert confirmed.status_code == 422, confirmed.text


def test_empty_string_does_not_resolve_an_invalid_field(client, monkeypatch):
    candidate = copy.deepcopy(PROPOSAL)
    candidate["defect_summary"] = "不存在的缺陷"
    row, extracted = extract_candidate(client, monkeypatch, candidate)
    assert extracted.status_code == 200, extracted.text
    cleared = copy.deepcopy(PROPOSAL)
    cleared["defect_summary"] = ""
    cleared["field_evidence"].pop("defect_summary")
    confirmed = client.post(
        f"/api/incidents/drafts/{row['id']}/confirm",
        headers=headers(client, "reviewer"),
        json={
            "expected_revision": extracted.json()["revision"],
            "proposal": cleared,
            "note": "空字符串不是有证据的修正",
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["proposal"]["scope_review_required"] is True
    assert "field_review:defect_summary" in confirmed.json()["proposal"]["unresolved_fields"]


def test_reextraction_keeps_unresolved_constraints_and_original_audit(client, monkeypatch):
    bad = copy.deepcopy(PROPOSAL)
    bad["batches"] = ["B01"]
    bad["field_evidence"]["batches"] = {"quote": "原文没有的批次"}
    row, first = extract_candidate(client, monkeypatch, bad)
    assert first.status_code == 200, first.text
    monkeypatch.setattr(
        "quality.incidents.AgnesClient.complete",
        lambda *args: {
            "message": {"content": json.dumps(PROPOSAL)},
            "engine_mode": "fake",
        },
    )
    second = client.post(
        f"/api/incidents/drafts/{row['id']}/extract",
        headers=headers(client, "reviewer"),
        json={"expected_revision": first.json()["revision"]},
    )
    assert second.status_code == 200, second.text
    assert second.json()["proposal"]["scope_review_required"] is True
    assert "field_review:batches" in second.json()["proposal"]["unresolved_fields"]
    previous = second.json()["field_review"]["previous_extractions"]
    assert previous[-1]["fields"]["batches"]["raw_candidate"] == ["B01"]


@pytest.mark.parametrize("endpoint", ["2025-01-01", "2025-04-30"])
def test_one_interval_endpoint_cannot_be_reused_as_a_single_day_scope(
    client, monkeypatch, endpoint
):
    candidate = copy.deepcopy(PROPOSAL)
    candidate.update(production_start=endpoint, production_end=endpoint)
    row, extracted = extract_candidate(client, monkeypatch, candidate)
    assert extracted.status_code == 200, extracted.text
    assert extracted.json()["proposal"]["production_start"] is None
    assert extracted.json()["proposal"]["scope_review_required"] is True
    confirmed = client.post(
        f"/api/incidents/drafts/{row['id']}/confirm",
        headers=headers(client, "reviewer"),
        json={
            "expected_revision": extracted.json()["revision"],
            "proposal": candidate,
            "note": "范围端点不能重复当成单日",
        },
    )
    assert confirmed.status_code == 422, confirmed.text


def test_explicit_single_production_day_remains_valid(client, monkeypatch):
    text = TEXT.replace("2025-01-01至2025-04-30", "2025-04-30")
    candidate = copy.deepcopy(PROPOSAL)
    candidate.update(production_start="2025-04-30", production_end="2025-04-30")
    for key in ("production_start", "production_end"):
        candidate["field_evidence"][key] = {"quote": "生产日期2025-04-30"}
    _, extracted = extract_candidate(client, monkeypatch, candidate, text)
    assert extracted.status_code == 200, extracted.text
    assert extracted.json()["status"] == "pending_confirmation"
    assert extracted.json()["proposal"]["production_start"] == "2025-04-30"


def test_postgresql_field_review_survives_restart(monkeypatch):
    import os

    from fastapi.testclient import TestClient
    from quality.config import Settings
    from quality.main import create_app

    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("需显式配置独立 PostgreSQL 测试库")
    if not url.endswith("/quality_test"):
        pytest.fail("集成验证仅使用 quality_test 测试库")
    settings = Settings(
        _env_file=None,
        database_url=url,
        model_mode="fake",
        jwt_secret="field-review-integration-" * 3,
        agnes_api_key="",
        siliconflow_api_key="",
    )
    candidate = copy.deepcopy(PROPOSAL)
    candidate["production_start"] = "2025-02-01"
    with TestClient(create_app(settings)) as first:
        row, extracted = extract_candidate(first, monkeypatch, candidate)
        assert extracted.status_code == 200, extracted.text
        review = extracted.json()["field_review"]
    with TestClient(create_app(settings)) as second:
        stored = next(
            item
            for item in second.get(
                "/api/incidents/drafts", headers=headers(second, "reviewer")
            ).json()
            if item["id"] == row["id"]
        )
        assert stored["field_review"] == review
        assert stored["proposal"]["brand"] == "演示品牌"
        assert stored["proposal"]["production_start"] is None
