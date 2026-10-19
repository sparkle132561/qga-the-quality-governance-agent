import json
import math

import pytest
from quality.config import ROOT
from quality.evaluation import (
    controlled_architecture,
    controlled_context_comparison,
    evaluation_code_fingerprint,
    graph_result_correct,
    latency_summary,
    public_snapshot,
    require_same_snapshot,
    retrieval_score,
    run_contract,
    timed_retrieval,
)


def test_ranking_scores_count_one_target_once_and_enforce_version_policy():
    expected = {"rule_id": "new", "page": 8, "forbidden_rule_id": "old"}
    hits = [
        {"rule_id": "other", "location": {"page": 8}},
        {"rule_id": "new", "location": {"page": 8}},
        {"rule_id": "new", "location": {"page": 8}},
    ]
    score = retrieval_score(hits, expected)
    assert score["evidence_recall_at_k"] == 1 and score["mrr"] == 0.5
    assert score["ndcg"] == pytest.approx(1 / math.log2(3))
    assert not retrieval_score([*hits, {"rule_id": "old"}], expected)["correct"]
    assert retrieval_score([], {"forbidden_rule_ids": ["old"]})["evidence_recall_at_k"] is None
    assert latency_summary([])["p95_ms"] is None
    assert latency_summary([1, 2, 99, 100])["p95_ms"] == 100


def test_dataset_remains_unverified_and_contract_boundaries_match_draft_expectations():
    dataset = json.loads((ROOT / "tests/fixtures/contract-cases.json").read_text(encoding="utf-8"))
    assert len(dataset["cases"]) == 40 and dataset["human_verified_count"] == 0
    assert len({case["id"] for case in dataset["cases"]}) == 40
    assert all(case["human_verified"] is False for case in dataset["cases"])
    for case in dataset["cases"]:
        if case["kind"] in {"impact_scope", "tool_contract", "context_contract"}:
            observed = run_contract(case["kind"], case["input"])
            assert all(observed[key] == value for key, value in case["expected"].items()), case[
                "label"
            ]


@pytest.mark.parametrize(
    "mode,calls,tools", [("single", 2, 1), ("multi_serial", 8, 4), ("multi_parallel", 8, 4)]
)
def test_controlled_agent_comparison_preserves_sources_and_reports_fake_cost_boundary(
    mode, calls, tools
):
    result = controlled_architecture(mode, delay_seconds=0)
    assert result["source_coverage"] == 1 and result["completed"]
    assert result["model_calls"] == calls and result["tool_calls"] == tools
    assert result["external_requests"] == 0 and result["engine_mode"] == "fake"
    assert result["actual_tokens"] is None


def test_context_comparison_reports_size_reduction_without_claiming_semantic_quality():
    rows = controlled_context_comparison()
    assert all(row["facts_retained"] for row in rows)
    assert not rows[0]["within_3000_chars"]
    assert all(row["within_3000_chars"] for row in rows[1:])
    assert rows[2]["context_trace"]["selected_memory_ids"] == ["controlled-reference"]
    assert all(row["actual_tokens"] is None and row["task_quality"] is None for row in rows)


def test_sql_score_is_independent_of_graph_truncation_and_scorer_delay_excluded(monkeypatch):
    assert graph_result_correct("sql", ["order"], ["order"], truncated=True)
    assert not graph_result_correct("graph", ["order"], ["order"], truncated=True)
    clock = [0.0]
    monkeypatch.setattr("quality.evaluation.time.perf_counter", lambda: clock[0])

    def operation():
        clock[0] += 0.005
        return [], None

    def slow_score(hits, expected):
        clock[0] += 0.020
        return {"correct": True}

    _, _, elapsed, _ = timed_retrieval(operation, {}, scorer=slow_score)
    assert elapsed == pytest.approx(5)


def test_different_database_snapshots_are_rejected(client):
    from conftest import headers
    from quality.models import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    client.post("/api/admin/import-demo", headers=headers(client, "admin"))
    second = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(second)
    with client.app.state.sessions() as first, Session(second) as other:
        with pytest.raises(ValueError, match="快照不一致"):
            require_same_snapshot(public_snapshot(first), public_snapshot(other))
    second.dispose()


def test_code_fingerprint_covers_graph_tools_and_new_modules(tmp_path):
    folder = tmp_path / "backend/quality"
    folder.mkdir(parents=True)
    script = tmp_path / "scripts"
    script.mkdir()
    (script / "evaluate-project.py").write_text("runner", encoding="utf-8")
    for name in ["graph_paths.py", "knowledge_graph.py", "agent_core.py"]:
        (folder / name).write_text(name, encoding="utf-8")
    previous = evaluation_code_fingerprint(tmp_path)
    for name in ["graph_paths.py", "knowledge_graph.py", "agent_core.py"]:
        (folder / name).write_text(name + " changed", encoding="utf-8")
        current = evaluation_code_fingerprint(tmp_path)
        assert current != previous
        previous = current
    (folder / "new_module.py").write_text("new source", encoding="utf-8")
    assert evaluation_code_fingerprint(tmp_path) != previous


def test_snapshot_endpoint_is_admin_only_and_never_returns_rule_text_or_answers(client):
    from conftest import headers

    assert client.get("/api/admin/evaluation-snapshot", headers=headers(client)).status_code == 403
    result = client.get("/api/admin/evaluation-snapshot", headers=headers(client, "admin"))
    assert result.status_code == 200
    assert set(result.json()) == {"chunk_snapshot_sha256", "rule_snapshot_sha256"}
