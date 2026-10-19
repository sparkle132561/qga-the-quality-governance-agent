import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from quality.agent_core import ToolExecutor
from quality.retrieval_agent import build_retrieval_agent
from test_retrieval_agent import ScriptedModel, initial


@pytest.mark.parametrize("ids,prose,expected", [
    ([], False, "completed"),
    (["fabricated-source"], False, "needs_info"),
    ([], True, "needs_info"),
])
def test_final_json_fence_keeps_schema_and_citation_guards(ids, prose, expected):
    body = {"recommendation": "needs_info", "explanation": "没有材料不能认定齐全",
            "evidence_ids": ids, "missing_fields": ["source_materials"]}
    content = "```json\n" + json.dumps(body, ensure_ascii=False) + "\n```"
    if prose:
        content = "Extra explanation before JSON: " + content
    model = ScriptedModel([{"role": "assistant", "content": content}])
    graph = build_retrieval_agent(model, ToolExecutor([]), InMemorySaver())
    result = graph.invoke(initial(), {"configurable": {"thread_id": "fence-regression"}})
    assert result["status"] == expected
    if expected == "needs_info":
        assert result["error_code"] == "invalid_final" and result["final"]["evidence_ids"] == []
