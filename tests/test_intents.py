import json
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from conftest import headers
from quality.models import Batch, Product, ReviewCase
from quality.providers import AgnesClient


def create(client, workflow="qualification", payload=None):
    return client.post("/api/cases", headers=headers(client), json={
        "title": "意图测试", "workflow": workflow, "payload": payload or {},
    }).json()


def model(monkeypatch, intent="query_case", workflow=None, entities=None):
    def complete(self, messages, tools=None):
        return {"message": {"role": "assistant", "content": json.dumps({
            "workflow": workflow, "intent": intent, "entities": entities or {},
            "confidence": 0.8,
        })}, "usage": {}, "elapsed_ms": 1, "engine_mode": "fake"}
    monkeypatch.setattr(AgnesClient, "complete", complete)


def propose(client, case, who=None, question="查询当前案件"):
    return client.post(f"/api/cases/{case['id']}/intent", headers=who or headers(client),
                       json={"question": question})


def confirm(client, case, value, who=None):
    return client.post(f"/api/cases/{case['id']}/intent/confirm",
                       headers=who or headers(client), json={"token": value["token"]})


def test_confirm_is_explicit_audited_and_never_executes_business(client, monkeypatch):
    model(monkeypatch)
    case = create(client)
    value = propose(client, case).json()
    assert value["entities"]["case_id"]["source"] == "current_case"
    assert value["human_review_required"] is True
    before = client.get(f"/api/cases/{case['id']}/audit", headers=headers(client)).json()
    assert not any(row["action"] == "confirm_intent" for row in before)
    response = confirm(client, case, value)
    assert response.json()["destination"] == "case"
    assert response.json()["business_action_executed"] is False
    found = client.get(f"/api/cases/{case['id']}", headers=headers(client)).json()
    assert (found["status"], found["revision"], found["latest_analysis"]) == ("draft", 1, None)
    audit = client.get(f"/api/cases/{case['id']}/audit", headers=headers(client)).json()
    assert audit[-1]["action"] == "confirm_intent"


def test_question_slots_have_source_spans_and_missing_fields_block_confirmation(
    client, monkeypatch
):
    model(monkeypatch, "search_rules", entities={"as_of": "2025-07-01"})
    case = create(client)
    value = propose(client, case, question="查询2025-07-01适用规则").json()
    slot = value["entities"]["as_of"]
    assert "查询2025-07-01适用规则"[slot["start"]:slot["end"]] == slot["value"]
    assert confirm(client, case, value).json()["destination"] == "evidence"
    model(monkeypatch, "search_rules")
    missing = propose(client, case).json()
    assert missing["missing_fields"] == ["as_of"]
    assert confirm(client, case, missing).status_code == 422


@pytest.mark.parametrize("intent,workflow,entities,question", [
    ("unknown", None, {}, "查询案件"),
    ("query_case", None, {"model": "invented"}, "查询案件"),
])
def test_invalid_or_hallucinated_extraction_is_rejected(
    client, monkeypatch, intent, workflow, entities, question
):
    model(monkeypatch, intent, workflow, entities)
    assert propose(client, create(client), question=question).status_code == 422


def test_workflow_conflict_and_unknown_intent_require_clarification(client, monkeypatch):
    case = create(client)
    model(monkeypatch, "submit_appeal", "remediation")
    value = propose(client, case).json()
    assert set(value["missing_fields"]) == {"work_order_id", "workflow_conflict"}
    assert confirm(client, case, value).status_code == 422
    model(monkeypatch, "out_of_scope")
    assert propose(client, case).json()["missing_fields"] == ["supported_intent"]


def test_cross_owner_is_rejected_before_model_call_and_signed_token_is_actor_bound(
    client, monkeypatch
):
    model(monkeypatch)
    case = create(client)
    value = propose(client, case).json()
    assert propose(client, case, headers(client, "merchant-b")).status_code == 404
    assert confirm(client, case, value, headers(client, "reviewer")).status_code == 403
    second = create(client)
    assert confirm(client, second, value).status_code == 403
    assert client.get("/api/auth/me", headers={
        "Authorization": "Bearer " + value["token"],
    }).status_code == 401


@pytest.mark.parametrize("key,value", [("merchant_id", "merchant-b"), ("case_id", "other")])
def test_model_entity_never_changes_permission_scope(client, monkeypatch, key, value):
    model(monkeypatch, entities={key: value})
    assert propose(client, create(client), question=value).status_code == 403


@pytest.mark.parametrize("change", ["revision", "status", "payload"])
def test_confirmation_rechecks_revision_status_and_payload(client, monkeypatch, change):
    model(monkeypatch)
    case = create(client)
    value = propose(client, case).json()
    with client.app.state.sessions.begin() as session:
        row = session.get(ReviewCase, case["id"])
        if change == "revision":
            row.revision += 1
        elif change == "status":
            row.status = "submitted"
        else:
            row.payload = {"as_of": "2025-07-01"}
    assert confirm(client, case, value).status_code == 409


def test_tampered_and_expired_tokens_are_rejected(client, monkeypatch):
    model(monkeypatch)
    case = create(client)
    value = propose(client, case).json()
    assert confirm(client, case, {"token": "tampered"}).status_code == 403
    key = client.app.state.settings.jwt_secret
    payload = jwt.decode(value["token"], key, algorithms=["HS256"],
                         audience="intent-confirmation")
    payload["exp"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    expired = jwt.encode(payload, key, algorithm="HS256")
    assert confirm(client, case, {"token": expired}).status_code == 403


def test_destructive_intent_requires_role_and_waiting_review_but_still_only_handoffs(
    client, monkeypatch
):
    model(monkeypatch, "approve")
    case = create(client)
    assert confirm(client, case, propose(client, case).json()).status_code == 403
    reviewer = headers(client, "reviewer")
    pending = propose(client, case, reviewer).json()
    assert confirm(client, case, pending, reviewer).status_code == 409
    with client.app.state.sessions.begin() as session:
        session.get(ReviewCase, case["id"]).status = "waiting_review"
    result = confirm(client, case, propose(client, case, reviewer).json(), reviewer).json()
    assert result["destination"] == "approval" and not result["business_action_executed"]
    found = client.get(f"/api/cases/{case['id']}", headers=reviewer).json()
    assert found["status"] == "waiting_review"


def test_current_case_changes_during_model_call_cannot_produce_valid_proposal(client, monkeypatch):
    case = create(client)
    def changed(self, messages, tools=None):
        with client.app.state.sessions.begin() as session:
            session.get(ReviewCase, case["id"]).revision += 1
        return {"message": {"content": json.dumps({
            "intent": "query_case", "workflow": None, "entities": {}, "confidence": 0.5,
        })}, "usage": {}, "elapsed_ms": 1, "engine_mode": "fake"}
    monkeypatch.setattr(AgnesClient, "complete", changed)
    assert propose(client, case).status_code == 409


def test_missing_key_and_empty_question_do_not_fabricate_intent(client):
    case = create(client)
    assert propose(client, case).status_code == 503
    assert propose(client, case, question="   ").status_code == 422


def test_entity_lookup_is_owner_filtered_and_rechecked_at_confirmation(client, monkeypatch):
    with client.app.state.sessions.begin() as session:
        session.add_all([
            Product(id="own-sku", merchant_id="merchant-a", supplier_id="s", brand="DEMO",
                    model="K170", category="kettle"),
            Product(id="foreign-sku", merchant_id="merchant-b", supplier_id="s", brand="DEMO",
                    model="K170", category="kettle"),
        ])
        session.flush()
        session.add(Batch(id="batch-a", product_id="own-sku", number="B001"))
    case = create(client, payload={"sku_id": "own-sku"})
    model(monkeypatch, entities={"model": "K170", "batch_number": "B001"})
    value = propose(client, case, question="查询K170批次B001").json()
    assert not value["missing_fields"]
    with client.app.state.sessions.begin() as session:
        session.get(Product, "own-sku").model = "K999"
    assert confirm(client, case, value).status_code == 409
    model(monkeypatch, entities={"sku_id": "foreign-sku"})
    assert propose(client, case, question="foreign-sku").status_code == 404
    model(monkeypatch, entities={"batch_number": "NOT-FOUND"})
    assert propose(client, case, question="NOT-FOUND").status_code == 404


@pytest.mark.parametrize("key,value,question", [
    ("model", "K170", "核对K170S"),
    ("sku_id", "sku-hit", "查询sku-hit-extra"),
    ("as_of", "2025-07-01", "检索2025-07-010"),
])
def test_identifier_prefix_is_not_an_exact_slot(client, monkeypatch, key, value, question):
    model(monkeypatch, entities={key: value})
    assert propose(client, create(client), question=question).status_code == 422


def test_empty_slot_can_be_proposed_without_mutating_current_case(client, monkeypatch):
    with client.app.state.sessions.begin() as session:
        session.add(Product(id="own-sku", merchant_id="merchant-a", supplier_id="s",
                            brand="DEMO", model="K170", category="kettle"))
    case = create(client)
    model(monkeypatch, "create_review", entities={"sku_id": "own-sku", "as_of": "2025-07-01"})
    value = propose(client, case, question="创建own-sku 2025-07-01准入").json()
    assert value["missing_fields"] == []
    assert confirm(client, case, value).json()["destination"] == "workspace"
    assert client.get(f"/api/cases/{case['id']}", headers=headers(client)).json()["payload"] == {}


@pytest.mark.parametrize("prefix,suffix,expected", [
    ("", "", 200), (" \n```json\n", "\n```\n", 200),
    ("这是答案\n```json\n", "\n```", 422),
])
def test_only_bare_or_complete_json_fence_is_accepted(
    client, monkeypatch, prefix, suffix, expected
):
    content = json.dumps({"intent": "query_case", "workflow": None,
                          "entities": {}, "confidence": 0.8})
    monkeypatch.setattr(AgnesClient, "complete", lambda *args, **kwargs: {
        "message": {"content": prefix + content + suffix}, "engine_mode": "fake",
        "usage": {}, "elapsed_ms": 0,
    })
    assert propose(client, create(client)).status_code == expected


def test_literal_date_is_recovered_when_model_omits_it_or_returns_null(client, monkeypatch):
    model(monkeypatch, "search_rules", entities={"as_of": None})
    case = create(client)
    value = propose(client, case, question="检索2025-07-01规则").json()
    assert value["entities"]["as_of"]["value"] == "2025-07-01"
    assert value["missing_fields"] == []
    assert confirm(client, case, value).json()["destination"] == "evidence"
    multiple = propose(client, case, question="比较2025-07-01和2025-08-01规则").json()
    assert "as_of_conflict" in multiple["missing_fields"]
    assert confirm(client, case, multiple).status_code == 422
