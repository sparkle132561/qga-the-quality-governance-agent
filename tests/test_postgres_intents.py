import json
import os
from uuid import uuid4

import pytest
from conftest import headers
from fastapi.testclient import TestClient
from quality.config import Settings
from quality.main import create_app
from quality.providers import AgnesClient


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="需独立PostgreSQL测试库")
def test_intent_confirmation_after_restart_persists_audit_without_business_write(monkeypatch):
    url = os.environ["TEST_DATABASE_URL"]
    if not url.endswith("/quality_test"):
        pytest.fail("仅允许quality_test独立库")
    settings = Settings(database_url=url, jwt_secret="intent-pg-only-" * 4,
                        agnes_api_key="", siliconflow_api_key="", model_mode="fake")
    monkeypatch.setattr(AgnesClient, "complete", lambda *args, **kwargs: {
        "message": {"content": json.dumps({"workflow": None, "intent": "query_case",
                                            "entities": {}, "confidence": 0.8})},
        "engine_mode": "fake", "usage": {}, "elapsed_ms": 0,
    })
    with TestClient(create_app(settings)) as first:
        auth = headers(first)
        case = first.post("/api/cases", headers=auth, json={
            "title": "TASK2 PG restart " + str(uuid4()), "workflow": "qualification",
        }).json()
        proposed = first.post(f"/api/cases/{case['id']}/intent", headers=auth,
                              json={"question": "查询当前案件"})
        assert proposed.status_code == 200
        token = proposed.json()["token"]
    with TestClient(create_app(settings)) as second:
        auth = headers(second)
        confirmed = second.post(f"/api/cases/{case['id']}/intent/confirm", headers=auth,
                                json={"token": token})
        assert confirmed.status_code == 200
        assert confirmed.json()["business_action_executed"] is False
    with TestClient(create_app(settings)) as third:
        auth = headers(third)
        found = third.get(f"/api/cases/{case['id']}", headers=auth).json()
        assert (found["revision"], found["status"], found["latest_analysis"]) == (1, "draft", None)
        events = third.get(f"/api/cases/{case['id']}/audit", headers=auth).json()
        assert events[-1]["action"] == "confirm_intent"
        assert events[-1]["details"]["business_action_executed"] is False


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="需独立PostgreSQL测试库")
def test_case_summary_and_original_analysis_survive_checkpoint_restart(monkeypatch):
    from quality.agent_core import ToolExecutor
    from quality.models import ReviewCase
    from quality.retrieval_agent import build_retrieval_agent, prepare_model_context
    url = os.environ["TEST_DATABASE_URL"]
    if not url.endswith("/quality_test"):
        pytest.fail("仅允许quality_test独立库")
    settings = Settings(database_url=url, jwt_secret="summary-pg-only-" * 4,
                        agnes_api_key="", siliconflow_api_key="", model_mode="fake")
    monkeypatch.setattr(AgnesClient, "complete", lambda *args, **kwargs: {
        "message": {"role": "assistant", "content": json.dumps({
            "recommendation": "needs_info", "explanation": "测试协议，待补件",
            "evidence_ids": [], "missing_fields": [],
        })}, "engine_mode": "fake", "usage": {}, "elapsed_ms": 0,
    })
    with TestClient(create_app(settings)) as first:
        auth = headers(first)
        case = first.post("/api/cases", headers=auth, json={
            "title": "TASK3 PG summary " + str(uuid4()), "workflow": "qualification",
            "payload": {"model": "K170", "as_of": "2025-07-01"},
        }).json()
        with first.app.state.sessions.begin() as session:
            session.get(ReviewCase, case["id"]).latest_analysis = {
                "run_id": "archived-run", "thread_id": "archived-thread", "revision": 1,
                "explanation": "原分析须完整保留" * 500,
            }
        summary = first.get(f"/api/cases/{case['id']}/summary", headers=auth).json()
        result = first.post(f"/api/cases/{case['id']}/assistant", headers=auth,
                            json={"question": "解释当前字段", "as_of": "2025-07-01"})
        assert result.status_code == 200
        thread = result.json()["thread_id"]
    with TestClient(create_app(settings)) as second:
        auth = headers(second)
        graph = build_retrieval_agent(None, ToolExecutor([]), second.app.state.checkpointer)
        saved = graph.get_state({"configurable": {"thread_id": thread}}).values
        assert saved["case_summary"] == summary
        assert "原分析须完整保留" in saved["facts"]["latest_analysis"]["explanation"]
        assert second.get(f"/api/cases/{case['id']}/summary", headers=auth).json() == summary
        with second.app.state.sessions.begin() as session:
            row = session.get(ReviewCase, case["id"])
            row.payload = {"model": "K170S", "as_of": "2025-07-01"}
            row.revision += 1
        fresh = second.get(f"/api/cases/{case['id']}", headers=auth).json()
        saved["facts"] = fresh
        with pytest.raises(ValueError):
            prepare_model_context(saved)
