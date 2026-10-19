from datetime import date
from types import SimpleNamespace

import pytest
from quality.task_routing import execute_route


@pytest.mark.parametrize("available,initial_hits,expected_calls", [
    (True, [], 2), (False, [], 1), (True, [{"id": "existing"}], 1),
])
def test_empty_lexical_gets_one_authorized_semantic_attempt(
    available, initial_hits, expected_calls
):
    calls = []
    who = object()
    deadline = 10**20
    def reader(request, **kwargs):
        calls.append((request, kwargs))
        hits = initial_hits if request.method == "bm25" else [{"id": "semantic-result"}]
        return {"hits": hits, "query_runs": [{"candidates": hits}],
                "query_plan": {"status": "disabled"},
                "provider_requests": {"rewrite": {}, "embedding": [], "rerank": {}},
                "timing_ms": {"total": 2, "embedding": int(request.method == "hybrid")}}
    app = SimpleNamespace(state=SimpleNamespace(search_reader=reader))
    plan = {"route": "evidence", "policy": "adaptive", "hybrid": False,
            "rewrite": False, "rerank": False, "capabilities": {"hybrid": available},
            "allowed_graph_starts": [], "clarification": [], "planner_ms": 0}
    result = execute_route(app, "current", who, "certificate", date(2025, 7, 1), 5,
                           plan, deadline=deadline)
    assert len(calls) == expected_calls
    for request, kwargs in calls:
        assert request.case_id == "current" and request.as_of == date(2025, 7, 1)
        assert request.query == "certificate" and kwargs["who"] is who
        assert kwargs["deadline"] == deadline and not request.rewrite
    if expected_calls == 2:
        assert result["hits"][0]["id"] == "semantic-result"
        assert result["timing_ms"]["search"] == 4
        assert "empty_lexical_semantic_fallback" in result["route"]["clarification"]
