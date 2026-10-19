"""默认只做本地机制/存储/HTTP检索和受控编排实验；不调用外部模型。"""

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from importlib.metadata import version
from pathlib import Path

import httpx
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from quality.config import Settings  # noqa: E402
from quality.domain import Actor, Role  # noqa: E402
from quality.evaluation import (  # noqa: E402
    controlled_architecture,
    controlled_context_comparison,
    evaluation_code_fingerprint,
    graph_result_correct,
    latency_summary,
    public_snapshot,
    require_same_snapshot,
    run_contract,
    timed_retrieval,
    validate_public_hits,
)  # noqa: E402
from quality.graph_paths import PathRequest, find_paths, graph_signature  # noqa: E402
from quality.knowledge_graph import business_graph  # noqa: E402
from quality.models import Batch, DemoOrder, EvidenceChunk, Product, RuleVersion  # noqa: E402
from quality.retrieval import eligible_query, keyword_search, result_hit  # noqa: E402


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def sql_orders(session, input_data):
    kind, identifier = input_data["start_id"].split(":", 1)
    criterion = (
        Product.supplier_id == identifier if kind == "supplier" else Product.id == identifier
    )
    return set(
        session.scalars(
            select(DemoOrder.id)
            .join(Batch, DemoOrder.batch_id == Batch.id)
            .join(Product, Batch.product_id == Product.id)
            .where(
                criterion,
                Product.merchant_id == input_data["merchant_id"],
                DemoOrder.merchant_id == input_data["merchant_id"],
            )
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=10)
    args = parser.parse_args()
    if not 2 <= args.repetitions <= 30:
        parser.error("重复次数须2～30")
    dataset_path = ROOT / "tests/fixtures/contract-cases.json"
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    settings = Settings()
    frozen_code = evaluation_code_fingerprint(ROOT)
    engine = create_engine(settings.database_url, connect_args={"connect_timeout": 5})
    records, rag_samples, graph_samples, architecture_samples = [], [], [], []
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=60) as client:
        login = client.post("/api/auth/login", json={"username": "admin", "password": "demo-only"})
        login.raise_for_status()
        client.headers["Authorization"] = "Bearer " + login.json()["access_token"]
        client.post("/api/admin/import-demo").raise_for_status()
        started = time.perf_counter()
        indexed = client.post("/api/admin/index-rules")
        indexed.raise_for_status()
        index_ms = (time.perf_counter() - started) * 1000
        with Session(engine) as session:
            corpus = [
                {
                    "id": row.id,
                    "rule_id": row.rule_id,
                    "source_sha256": row.source_sha256,
                    "location": row.location,
                }
                for row in session.scalars(
                    select(EvidenceChunk)
                    .where(EvidenceChunk.rule_id.is_not(None))
                    .order_by(EvidenceChunk.id)
                )
            ]
            rules = [
                {
                    "id": row.id,
                    "content": row.content,
                    "requirements": row.requirements,
                    "from": str(row.effective_from),
                    "until": str(row.effective_until),
                }
                for row in session.scalars(select(RuleVersion).order_by(RuleVersion.id))
            ]
            frozen_public = public_snapshot(session)
            remote = client.get("/api/admin/evaluation-snapshot")
            remote.raise_for_status()
            require_same_snapshot(frozen_public, remote.json())
            for case in dataset["cases"]:
                # expected从不传给被测函数，评分在观测返回之后。
                input_data = case["input"]
                kind = case["kind"]
                if kind in {"impact_scope", "tool_contract", "context_contract"}:
                    observed = run_contract(kind, input_data)
                    correct = all(
                        observed.get(key) == value for key, value in case["expected"].items()
                    )
                    if input_data.get("mode") == "compact":
                        correct = correct and observed.get("summary", {}).get("retrieved_ids") == [
                            "e1"
                        ]
                    if input_data.get("mode") == "memory":
                        correct = correct and not observed["system_memory_instruction_injected"]
                    if input_data.get("mode") == "orphan":
                        correct = correct and observed["context_trace"]["invalid_round_groups"] == 1
                    records.append(
                        {"id": case["id"], "kind": kind, "observed": observed, "correct": correct}
                    )
                elif kind == "retrieval":
                    first = None
                    for repetition in range(args.repetitions):
                        for method in (
                            ["substring", "bm25_local", "bm25"]
                            if repetition % 2 == 0
                            else ["bm25", "bm25_local", "substring"]
                        ):

                            def operation():
                                if method == "substring":
                                    rows = list(
                                        session.scalars(
                                            eligible_query(
                                                date.fromisoformat(input_data["as_of"]), None
                                            )
                                            .where(
                                                EvidenceChunk.text.contains(
                                                    input_data["query"], autoescape=True
                                                )
                                            )
                                            .order_by(EvidenceChunk.id)
                                            .limit(input_data["limit"])
                                        )
                                    )
                                    hits = [result_hit(row, 0, "substring") for row in rows]
                                    stages = None
                                elif method == "bm25_local":
                                    hits = keyword_search(
                                        session,
                                        input_data["query"],
                                        date.fromisoformat(input_data["as_of"]),
                                        input_data["limit"],
                                        None,
                                    )
                                    stages = None
                                else:
                                    response = client.post(
                                        "/api/search", json={**input_data, "method": "bm25"}
                                    )
                                    response.raise_for_status()
                                    payload = response.json()
                                    hits, stages = payload["hits"], payload["timing_ms"]
                                return hits, stages

                            hits, stages, measured_ms, score = timed_retrieval(
                                operation, case["expected"]
                            )
                            validate_public_hits(hits, frozen_public)
                            sample = {
                                "id": case["id"],
                                "method": method,
                                "repetition": repetition,
                                "first_pass": repetition == 0,
                                "wall_ms": measured_ms,
                                "stages_ms": stages,
                                "score": score,
                                "scope": "same_public_chunk_snapshot_and_as_of_no_case_materials",
                                "hits": [
                                    {
                                        "id": h["id"],
                                        "rule_id": h["rule_id"],
                                        "location": h["location"],
                                    }
                                    for h in hits
                                ],
                            }
                            rag_samples.append(sample)
                            if method == "bm25" and repetition == 0:
                                first = score
                    records.append(
                        {
                            "id": case["id"],
                            "kind": kind,
                            "observed": first,
                            "correct": first["correct"],
                        }
                    )
                elif kind == "graph_path":
                    who = Actor(
                        user_id=input_data["merchant_id"],
                        role=Role.MERCHANT,
                        merchant_id=input_data["merchant_id"],
                    )
                    expected = set(case["expected"]["order_ids"])
                    correct = True
                    for repetition in range(args.repetitions):
                        sample = {
                            "id": case["id"],
                            "repetition": repetition,
                            "first_pass": repetition == 0,
                        }
                        for method in ["sql", "graph"] if repetition % 2 == 0 else ["graph", "sql"]:
                            started = time.perf_counter()
                            if method == "sql":
                                found = sql_orders(session, input_data)
                            else:
                                graph = business_graph(
                                    session, who, date.fromisoformat(input_data["as_of"])
                                )
                                try:
                                    paths = find_paths(
                                        graph,
                                        PathRequest(
                                            as_of=input_data["as_of"],
                                            start_id=input_data["start_id"],
                                            target_kind="order",
                                            max_hops=3,
                                            limit=20,
                                        ),
                                    )
                                    sample["graph_denied"] = False
                                except HTTPException as error:
                                    if error.status_code != 404:
                                        raise
                                    paths = {"paths": [], "truncated": False}
                                    sample["graph_denied"] = True
                                found = {
                                    path["nodes"][-1]["id"].split(":", 1)[1]
                                    for path in paths["paths"]
                                }
                                sample["graph_snapshot_sha256"] = graph_signature(graph)
                                sample["graph_truncated"] = paths["truncated"]
                            sample[method + "_ms"] = (time.perf_counter() - started) * 1000
                            sample[method + "_order_ids"] = sorted(found)
                            sample[method + "_correct"] = graph_result_correct(
                                method,
                                found,
                                expected,
                                sample.get("graph_truncated", False),
                                sample.get("graph_denied", False),
                                case["expected"].get("graph_denied", False),
                            )
                            correct = correct and sample[method + "_correct"]
                        graph_samples.append(sample)
                    records.append(
                        {
                            "id": case["id"],
                            "kind": kind,
                            "correct": correct,
                            "observed": graph_samples[-1],
                        }
                    )
            final_corpus = [
                {
                    "id": row.id,
                    "rule_id": row.rule_id,
                    "source_sha256": row.source_sha256,
                    "location": row.location,
                }
                for row in session.scalars(
                    select(EvidenceChunk)
                    .where(EvidenceChunk.rule_id.is_not(None))
                    .order_by(EvidenceChunk.id)
                )
            ]
            if digest(final_corpus) != digest(corpus):
                raise RuntimeError("语料快照变化，不保存混合配置评测")
            final_rules = [
                {
                    "id": row.id,
                    "content": row.content,
                    "requirements": row.requirements,
                    "from": str(row.effective_from),
                    "until": str(row.effective_until),
                }
                for row in session.scalars(select(RuleVersion).order_by(RuleVersion.id))
            ]
            remote = client.get("/api/admin/evaluation-snapshot")
            remote.raise_for_status()
            require_same_snapshot(frozen_public, remote.json())
            require_same_snapshot(frozen_public, public_snapshot(session))
            if digest(final_rules) != digest(rules):
                raise RuntimeError("规则适用或要求已变化，不保存混合配置评测")
    for repetition in range(args.repetitions):
        modes = ["single", "multi_serial", "multi_parallel"]
        if repetition % 2:
            modes.reverse()
        for mode in modes:
            architecture_samples.append(
                {
                    **controlled_architecture(mode),
                    "repetition": repetition,
                    "first_pass": repetition == 0,
                }
            )
    rag_summary = {}
    for method in ["substring", "bm25_local", "bm25"]:
        selected = [s for s in rag_samples if s["method"] == method]
        positive = [
            s["score"]["evidence_recall_at_k"]
            for s in selected
            if s["score"]["evidence_recall_at_k"] is not None
        ]
        rag_summary[method] = {
            "warm_latency": latency_summary(
                [s["wall_ms"] for s in selected if not s["first_pass"]]
            ),
            "draft_checks_passed": sum(s["score"]["correct"] for s in selected),
            "count": len(selected),
            "draft_evidence_recall_at_k": sum(positive) / len(positive) if positive else None,
            "draft_mrr": sum(s["score"]["mrr"] for s in selected if s["score"]["mrr"] is not None)
            / len(positive)
            if positive
            else None,
            "draft_ndcg": sum(
                s["score"]["ndcg"] for s in selected if s["score"]["ndcg"] is not None
            )
            / len(positive)
            if positive
            else None,
            "metric_denominator": "repeated_draft_cases_not_independent_human_annotations",
            "measurement_boundary": "in_process_public_chunk_query"
            if method in {"substring", "bm25_local"}
            else "HTTP_full_request",
            "source_quality_is_human_verified": False,
        }
    report = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        "dataset_version": dataset["dataset_version"],
        "draft_case_count": len(dataset["cases"]),
        "human_verified_count": 0,
        "corpus_snapshot_sha256": frozen_public["chunk_snapshot_sha256"],
        "rule_snapshot_sha256": digest(rules),
        "indexed": indexed.json(),
        "index_build_http_ms": index_ms,
        "config": {
            "repetitions": args.repetitions,
            "top_k": 5,
            "rrf_rank_constant": 60,
            "context_chars": 24000,
            "model_delay_ms": 20,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "langgraph": version("langgraph"),
            "code_snapshot_sha256": frozen_code,
            "working_tree_dirty": bool(
                subprocess.check_output(
                    ["git", "status", "--porcelain"], cwd=ROOT, text=True
                ).strip()
            ),
            "commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
        },
        "cases": records,
        "draft_checks_passed": sum(row["correct"] for row in records),
        "rag": rag_summary,
        "rag_samples": rag_samples,
        "graph": {
            "sql": latency_summary([s["sql_ms"] for s in graph_samples if not s["first_pass"]]),
            "graph": latency_summary([s["graph_ms"] for s in graph_samples if not s["first_pass"]]),
        },
        "graph_samples": graph_samples,
        "controlled_architecture": {
            mode: {
                "warm_latency": latency_summary(
                    [
                        s["wall_ms"]
                        for s in architecture_samples
                        if s["mode"] == mode and not s["first_pass"]
                    ]
                ),
                "model_calls_per_run": next(
                    s["model_calls"] for s in architecture_samples if s["mode"] == mode
                ),
                "tool_calls_per_run": next(
                    s["tool_calls"] for s in architecture_samples if s["mode"] == mode
                ),
                "coverage_all_runs": all(
                    s["source_coverage"] == 1 for s in architecture_samples if s["mode"] == mode
                ),
                "quality_status": "controlled_protocol_not_live_model",
            }
            for mode in ["single", "multi_serial", "multi_parallel"]
        },
        "architecture_samples": architecture_samples,
        "controlled_context": controlled_context_comparison(),
        "external_model_requests": 0,
        "actual_external_cost": 0,
        "live_matrix": {
            method: {
                "status": "not_run_missing_key"
                if not settings.agnes_api_key or not settings.siliconflow_api_key
                else "not_run_explicit_live_validation_required",
                "quality": None,
                "latency_ms": None,
                "tokens": None,
                "cost": None,
            }
            for method in [
                "vector",
                "hybrid",
                "rerank",
                "rewrite",
                "agentic_rag",
                "graph_agentic",
                "adaptive_vs_all",
                "semantic_summary",
                "memory_ablation_live",
                "single_multi_live",
            ]
        },
        "limitations": [
            "40条机制边界草案不是人工核验Gold，也不是40条完整业务闭环案例",
            "真实模型/视觉/意图/Token/成本与消融未验证；受控时延不证明LLM加速",
            "不同测量边界不可直接比较速度，子串与BM25共享语料但一个进程内SQL一个完整HTTP",
            "首次通过含状态/分词缓存预热，P95小样本初步观测；未缓存完整检索结果",
            "所有答案只由评分器读取，不加入运行输入/工具或RAG索引",
        ],
    }
    if evaluation_code_fingerprint(ROOT) != frozen_code:
        raise RuntimeError("被测代码在实验中变化，不保存混合版本报告")
    out = ROOT / "artifacts/benchmarks/unit-14-evaluation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(out),
                "draft_checks_passed": report["draft_checks_passed"],
                "draft_cases": len(records),
                "human_verified": 0,
                "rag": rag_summary,
                "graph": report["graph"],
                "controlled_architecture": report["controlled_architecture"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
