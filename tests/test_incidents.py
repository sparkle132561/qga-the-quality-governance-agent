import copy
import json

import pytest
from conftest import headers
from quality.incidents import NormalizedIncident, normalize_incident
from quality.models import IncidentDraft

TEXT = "模拟公告：召回演示品牌 K170 电热水壶，生产日期2025-01-01至2025-04-30，原因把手松脱风险。"
PROPOSAL = {
    "event_type": "recall",
    "brand": "演示品牌",
    "models": ["K170"],
    "production_scope": "range",
    "production_start": "2025-01-01",
    "production_end": "2025-04-30",
    "defect_summary": "把手松脱风险",
    "field_evidence": {
        "event_type": {"quote": "召回"},
        "brand": {"quote": "演示品牌"},
        "models": {"quote": "演示品牌 K170 电热水壶"},
        "production_start": {"quote": "生产日期2025-01-01至2025-04-30"},
        "production_end": {"quote": "生产日期2025-01-01至2025-04-30"},
        "defect_summary": {"quote": "把手松脱风险"},
    },
}


def create_draft(client, text=TEXT):
    client.post("/api/admin/import-demo", headers=headers(client, "admin"))
    response = client.post(
        "/api/incidents/drafts",
        headers=headers(client, "reviewer"),
        json={
            "source_id": "demo-platform-policy",
            "title": "模拟范围标准化",
            "raw_text": text,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_incident_line_selection_produces_exact_source_evidence(client, monkeypatch):
    text = "召回公告\n品牌：万利达\n型号：BM-251\n生产日期2023年02月08日至2023年02月08日"
    row = create_draft(client, text)
    body = {
        "event_type": "recall",
        "brand": "万利达",
        "models": ["BM-251"],
        "production_scope": "range",
        "production_start": "2023-02-08",
        "production_end": "2023-02-08",
        "field_evidence": {
            key: {"line_start": line, "line_end": line}
            for key, line in {
                "event_type": 1,
                "brand": 2,
                "models": 3,
                "production_start": 4,
                "production_end": 4,
            }.items()
        },
    }
    monkeypatch.setattr(
        "quality.incidents.AgnesClient.complete",
        lambda *args: {"message": {"content": json.dumps(body)}, "engine_mode": "fake"},
    )
    response = client.post(
        f"/api/incidents/drafts/{row['id']}/extract",
        headers=headers(client, "reviewer"),
        json={"expected_revision": row["revision"]},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "pending_confirmation"
    assert result["event_id"] is None
    for evidence in result["proposal"]["field_evidence"].values():
        assert text[evidence["char_start"] : evidence["char_end"]] == evidence["quote"]


def test_disjoint_production_ranges_cannot_become_continuous_scope():
    text = "召回品牌甲 M1，生产日期2020-01-01至2020-02-01、2021-01-01至2021-02-01。"
    body = {
        "event_type": "recall",
        "brand": "品牌甲",
        "models": ["M1"],
        "production_scope": "range",
        "production_start": "2020-01-01",
        "production_end": "2021-02-01",
        "field_evidence": {
            key: {"quote": text}
            for key in ["event_type", "brand", "models", "production_start", "production_end"]
        },
    }
    result = normalize_incident(NormalizedIncident.model_validate(body), text)
    assert result["scope_review_required"] is True
    assert "multiple_production_ranges" in result["unresolved_fields"]


def test_selected_defect_is_source_text_not_model_rewritten_summary():
    from quality.incidents import parse_incident

    text = "召回公告\n缺陷描述：长期使用\n可能发热起火。"
    body = {
        "event_type": "recall",
        "defect_summary": None,
        "field_evidence": {
            "event_type": {"line_start": 1, "line_end": 1},
            "defect_summary": {"line_start": 2, "line_end": 3},
        },
    }
    result = parse_incident(json.dumps(body), text)
    assert result["defect_summary"] == "缺陷描述：长期使用\n可能发热起火。"
    assert result["scope_review_required"] is True


def test_selected_incident_schema_requires_type_evidence_and_forbids_unknown_keys():
    from quality.incidents import IncidentSelection

    with pytest.raises(ValueError):
        IncidentSelection.model_validate({"event_type": "recall", "field_evidence": {}})
    with pytest.raises(ValueError):
        IncidentSelection.model_validate(
            {
                "event_type": "inspection",
                "field_evidence": {
                    "event_type": {"line_start": 1, "line_end": 1},
                    "table_body": {"line_start": 1, "line_end": 100},
                },
            }
        )


def test_selected_serial_scope_preserves_source_punctuation_and_stays_unresolved():
    from quality.incidents import parse_incident
    text = "召回公告\nSN号尾数为阿拉伯数字或字母“D”"
    body = {"event_type": "recall", "serial_scope_original": None, "field_evidence": {
        "event_type": {"line_start": 1, "line_end": 1},
        "serial_scope_original": {"line_start": 2, "line_end": 2}}}
    result = parse_incident(json.dumps(body), text)
    assert result["serial_scope_original"] == "SN号尾数为阿拉伯数字或字母“D”"
    assert result["serial_filter_status"] == "requires_interpretation"
    assert result["scope_review_required"] is True


def test_fenced_incident_stays_pending_confirmation(client, monkeypatch):
    row = create_draft(client)
    monkeypatch.setattr("quality.incidents.AgnesClient.complete", lambda *args: {
        "message": {"role": "assistant", "content": "```json\n" + json.dumps(PROPOSAL) + "\n```"},
        "engine_mode": "fake", "usage": {}, "elapsed_ms": 0})
    response = client.post(f"/api/incidents/drafts/{row['id']}/extract",
                           headers=headers(client, "reviewer"),
                           json={"expected_revision": row["revision"]})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending_confirmation"
    assert response.json()["event_id"] is None


def test_fenced_incident_does_not_accept_fabricated_field_evidence(client, monkeypatch):
    row = create_draft(client)
    proposal = copy.deepcopy(PROPOSAL)
    proposal["field_evidence"]["brand"]["quote"] = "原文不存在的品牌说明"
    monkeypatch.setattr("quality.incidents.AgnesClient.complete", lambda *args: {
        "message": {"role": "assistant", "content": "```json\n" + json.dumps(proposal) + "\n```"},
        "engine_mode": "fake", "usage": {}, "elapsed_ms": 0})
    response = client.post(f"/api/incidents/drafts/{row['id']}/extract",
                           headers=headers(client, "reviewer"),
                           json={"expected_revision": row["revision"]})
    assert response.status_code == 200
    assert response.json()["status"] == "needs_info"
    assert response.json()["proposal"]["brand"] is None
    assert response.json()["proposal"]["models"] == ["K170"]
    assert response.json()["field_review"]["fields"]["brand"]["status"] == "invalid"
    with client.app.state.sessions() as session:
        assert session.get(IncidentDraft, row["id"]).event_id is None


def test_model_proposal_waits_for_field_confirmation_and_is_demo(client, monkeypatch):
    row = create_draft(client)
    path = f"/api/incidents/drafts/{row['id']}"
    monkeypatch.setattr(
        "quality.incidents.AgnesClient.complete",
        lambda *args: {
            "message": {"role": "assistant", "content": json.dumps(PROPOSAL)},
            "engine_mode": "fake",
            "usage": {},
            "elapsed_ms": 0,
        },
    )
    extracted = client.post(
        path + "/extract", headers=headers(client, "reviewer"), json={"expected_revision": 1}
    )
    assert extracted.status_code == 200, extracted.text
    assert extracted.json()["status"] == "pending_confirmation"
    assert extracted.json()["event_id"] is None
    decision = {
        "expected_revision": 2,
        "proposal": PROPOSAL,
        "note": "逐字段核对模拟原文，非真实官方公告",
    }
    assert (
        client.post(path + "/confirm", headers=headers(client, "admin"), json=decision).status_code
        == 403
    )
    response = client.post(path + "/confirm", headers=headers(client, "reviewer"), json=decision)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "confirmed"
    assert (
        client.post(
            path + "/confirm", headers=headers(client, "reviewer"), json=decision
        ).status_code
        == 409
    )
    event = next(
        event
        for event in client.get("/api/events", headers=headers(client)).json()
        if event["id"] == response.json()["event_id"]
    )
    assert event["is_demo"] is True
    assert event["event_type"] == "recall"
    assert event["scope"]["date_precision"] == "day"
    assert event["scope"]["scope_review_required"] is False
    for field, source in event["scope"]["field_evidence"].items():
        assert TEXT[source["char_start"] : source["char_end"]] == source["quote"], field


def test_missing_key_keeps_draft_and_platform_permissions(client):
    row = create_draft(client)
    path = f"/api/incidents/drafts/{row['id']}"
    assert client.get("/api/incidents/drafts", headers=headers(client)).status_code == 403
    response = client.post(
        path + "/extract", headers=headers(client, "reviewer"), json={"expected_revision": 1}
    )
    assert response.status_code == 503
    saved = client.get("/api/incidents/drafts", headers=headers(client, "reviewer")).json()[0]
    assert saved["last_error"] == "missing_agnes_key" and saved["engine_mode"] == "not_called"
    assert saved["status"] == "needs_info" and saved["revision"] == 1
    response = client.post(
        path + "/confirm",
        headers=headers(client, "reviewer"),
        json={
            "expected_revision": 1,
            "proposal": PROPOSAL,
            "note": "手动核对模拟字段",
        },
    )
    assert response.status_code == 200


def test_month_precision_does_not_expand_into_invented_days():
    raw = TEXT.replace("2025-01-01", "2025-01").replace("2025-04-30", "2025-04")
    body = json.loads(
        json.dumps(PROPOSAL).replace("2025-01-01", "2025-01").replace("2025-04-30", "2025-04")
    )
    normalized = normalize_incident(NormalizedIncident.model_validate(body), raw)
    assert normalized["date_precision"] == "month"
    assert normalized["production_start"] == "2025-01"
    body["production_start"] = "2025-01-01"
    with pytest.raises(ValueError):
        normalize_incident(NormalizedIncident.model_validate(body), raw)


@pytest.mark.parametrize(
    "error", ["invented_quote", "short_model", "slash_model", "all_dates", "bad_offset", "bad_end"]
)
def test_scope_cannot_escape_source(error):
    body, raw = copy.deepcopy(PROPOSAL), TEXT
    if error == "invented_quote":
        body["field_evidence"]["brand"]["quote"] = "不存在的品牌"
    elif error == "short_model":
        raw = raw.replace("K170", "K170S")
        body["field_evidence"]["models"]["quote"] = "演示品牌 K170S 电热水壶"
    elif error == "slash_model":
        raw = raw.replace("K170", "K170/S")
        body["field_evidence"]["models"]["quote"] = "演示品牌 K170/S 电热水壶"
    elif error == "all_dates":
        body.update(production_scope="all", production_start=None, production_end=None)
    elif error == "bad_offset":
        body["field_evidence"]["models"]["char_start"] = 0
    else:
        start = raw.index("把手松脱风险")
        body["field_evidence"]["defect_summary"] = {
            "quote": raw[start:],
            "char_start": start,
            "char_end": 99999,
        }
    with pytest.raises(ValueError):
        normalize_incident(NormalizedIncident.model_validate(body), raw)


def test_negation_and_date_conflict_cannot_become_unlimited_scope():
    raw = TEXT + "并非全部生产日期。"
    body = copy.deepcopy(PROPOSAL)
    body.update(production_scope="all", production_start=None, production_end=None)
    body["field_evidence"]["production_scope"] = {"quote": "全部生产日期"}
    normalized = normalize_incident(NormalizedIncident.model_validate(body), raw)
    assert normalized["production_scope"] == "unknown"
    assert normalized["scope_review_required"] is True
    assert "production_scope_negated_or_conflicting" in normalized["unresolved_fields"]
    raw = TEXT + "另称所有生产日期。"
    body["field_evidence"]["production_scope"] = {"quote": "所有生产日期"}
    normalized = normalize_incident(NormalizedIncident.model_validate(body), raw)
    assert normalized["production_scope"] == "unknown" and normalized["scope_review_required"]


def test_inspection_keeps_type_and_unknown_serial_scope():
    raw = TEXT.replace("召回", "抽检") + "序列号YA643****至YA724****待解释。"
    body = copy.deepcopy(PROPOSAL)
    body["event_type"] = "inspection"
    body["field_evidence"]["event_type"]["quote"] = "抽检"
    normalized = normalize_incident(NormalizedIncident.model_validate(body), raw)
    assert normalized["event_type"] == "inspection"
    assert normalized["scope_review_required"] is True
    assert "serial_scope_requires_interpretation" in normalized["unresolved_fields"]


@pytest.mark.parametrize("suffix", ["S", "/S", "+"])
def test_truncated_quote_cannot_hide_model_suffix(suffix):
    raw = TEXT.replace("K170", "K170" + suffix)
    body = copy.deepcopy(PROPOSAL)
    body["field_evidence"]["models"]["quote"] = "K170"
    with pytest.raises(ValueError):
        normalize_incident(NormalizedIncident.model_validate(body), raw)


def test_truncated_batch_quote_checks_original_boundary():
    raw = TEXT + "批次B01/S。"
    body = copy.deepcopy(PROPOSAL)
    body["batches"] = ["B01"]
    body["field_evidence"]["batches"] = {"quote": "B01"}
    with pytest.raises(ValueError):
        normalize_incident(NormalizedIncident.model_validate(body), raw)


def test_clipped_date_quote_cannot_reduce_day_precision_to_month():
    body = copy.deepcopy(PROPOSAL)
    body.update(production_start="2025-01", production_end="2025-04")
    body["field_evidence"]["production_start"] = {"quote": "2025-01"}
    body["field_evidence"]["production_end"] = {"quote": "2025-04"}
    with pytest.raises(ValueError):
        normalize_incident(NormalizedIncident.model_validate(body), TEXT)


def test_recover_only_abandoned_extraction(client):
    row = create_draft(client)
    path = f"/api/incidents/drafts/{row['id']}"
    with client.app.state.sessions.begin() as session:
        draft = session.get(IncidentDraft, row["id"])
        draft.status = "extracting"
        draft.active_run = {"worker_id": client.app.state.worker_id, "run_id": "stalled"}
    assert client.post(path + "/recover", headers=headers(client, "reviewer")).status_code == 409
    with client.app.state.sessions.begin() as session:
        session.get(IncidentDraft, row["id"]).active_run = {
            "worker_id": "previous-worker",
            "run_id": "abandoned",
        }
    response = client.post(path + "/recover", headers=headers(client, "reviewer"))
    assert response.status_code == 200 and response.json()["status"] == "needs_info"
