"""任务4.2小样本真实规划/改写协议对照；不等于任务5质量评测。"""
import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from psycopg import sql
from sqlalchemy import create_engine, select, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from quality.config import Settings  # noqa: E402
from quality.main import create_app  # noqa: E402
from quality.models import Base, RuleVersion  # noqa: E402
from quality.providers import EMBEDDING_MODEL, RERANK_MODEL  # noqa: E402
from quality.retrieval import index_units  # noqa: E402


def assess_order_paths(paths):
    """此合成SKU的预期结果；不能用非空路径数量冒充质量正确。"""
    expected = {"order:demo-order-hit"}
    observed = {path["nodes"][-1]["id"] for path in paths
                if path.get("nodes") and path["nodes"][-1]["kind"] == "order"}
    correct = [path for path in paths
               if path.get("nodes") and path["nodes"][0]["id"] == "product:sku-hit"
               and path["nodes"][-1]["id"] in expected
               and [edge["predicate"] for edge in path["edges"]]
               == ["has_batch", "ordered_from"]
               and path["hops"] == 2 and len(path["nodes"]) == 3]
    return {"expected_orders": sorted(expected), "observed_orders": sorted(observed),
            "missing_orders": sorted(expected - observed),
            "unexpected_orders": sorted(observed - expected),
            "correct_path_count": len(correct),
            "passed": observed == expected and bool(correct)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--postgres-port", type=int, default=55432)
    args = parser.parse_args()
    if not args.confirm_live:
        parser.error("真实免费接口需--confirm-live")
    if not 1 <= args.postgres_port <= 65535:
        parser.error("数据库端口不合法")
    runtime = ROOT / "data/runtime" / ("routing-live-" + str(uuid4()))
    runtime.mkdir(parents=True)
    settings = Settings(_env_file=ROOT / ".env.live", model_mode="live",
                        upload_dir=runtime / "uploads")
    if (settings.agnes_base_url != "https://apihub.agnes-ai.com/v1"
            or settings.siliconflow_base_url != "https://api.siliconflow.cn/v1"):
        parser.error("只允许既有官方Agnes/硅基流动入口")
    if not settings.agnes_api_key or not settings.siliconflow_api_key:
        parser.error("本地私有配置需要Agnes与硅基流动Key")
    database = "quality_task04_" + uuid4().hex[:12]
    # 专用新库：不覆盖quality、quality_test或fake_only演示库，也不重置数据库卷。
    with psycopg.connect(f"postgresql://quality:quality_demo@127.0.0.1:{args.postgres_port}/quality",
                         autocommit=True, connect_timeout=5) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    settings.database_url = (
        f"postgresql+psycopg://quality:quality_demo@127.0.0.1:{args.postgres_port}/" + database
    )
    engine = create_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(engine)
    engine.dispose()
    report = {"checked_at": datetime.now(timezone.utc).isoformat(),
              "model": settings.agnes_model, "database": database,
              "postgres_port": args.postgres_port,
              "embedding_model": EMBEDDING_MODEL, "rerank_model": RERANK_MODEL,
              "account_bill_verified": False, "human_user_acceptance": False,
              "siliconflow_key_configured": True, "business_actions_executed": False,
              "scope": "synthetic_protocol_sample_not_quality_gold", "runs": []}
    with TestClient(create_app(settings, InMemorySaver())) as client:
        def auth(name):
            token = client.post("/api/auth/login", json={"username": name,
                                                       "password": "demo-only"}).json()
            return {"Authorization": "Bearer " + token["access_token"]}
        owner = auth("merchant-a")
        admin = auth("admin")
        client.post("/api/admin/import-demo", headers=admin).raise_for_status()
        # 仅嵌入少量合成规则，避免将本机官方PDF缓存意外纳入探针成本。
        with client.app.state.sessions.begin() as session:
            for rule in session.scalars(select(RuleVersion).where(RuleVersion.is_demo.is_(True))):
                index_units(session, "rule:" + rule.id, [{"text": rule.content,
                            "location": {"kind": "synthetic_probe_rule", "rule_id": rule.id,
                                         "rule_version": rule.version}}],
                            hashlib.sha256(rule.content.encode()).hexdigest(), True,
                            rule_id=rule.id)
        embedded = client.post("/api/admin/embed-rules", headers=admin)
        report["embedding_setup"] = {"status": embedded.status_code}
        if embedded.status_code == 200:
            report["embedding_setup"].update(embedded.json())
        if embedded.status_code != 200 or not embedded.json().get("embedded"):
            output = ROOT / "artifacts/benchmarks/task-04-02-routing-live.json"
            report["passed"] = False
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print("Embedding setup failed; sanitized report saved; no model fallback.")
            return 1
        case = client.post("/api/cases", headers=owner, json={
            "title": "TASK4 synthetic routing", "workflow": "qualification",
            "payload": {"sku_id": "sku-hit", "as_of": "2025-07-01"},
        }).json()
        query = "请沿当前SKU到供应商、批次和订单关系核对来源，说明能找到哪些订单。"
        for question, policy in [("状态", "adaptive"), (query, "adaptive"), (query, "all")]:
            response = client.post(f"/api/cases/{case['id']}/routed-search", headers=owner,
                                   json={"query": question, "policy": policy,
                                         "as_of": "2025-07-01", "limit": 5})
            row = {"question": question, "policy": policy, "status": response.status_code}
            if response.status_code == 200:
                data = response.json()
                row.update(route=data["route"], timing_ms=data["timing_ms"],
                           path_count=len(data["paths"]), hit_count=len(data["hits"]),
                           hit_ids=[hit["id"] for hit in data["hits"]],
                           result_sha256=data["result_sha256"],
                           query_plan=data.get("search_result", {}).get("query_plan"),
                           rerank_plan=data.get("search_result", {}).get("rerank_plan"),
                           quality=assess_order_paths(data["paths"]) if question == query else None,
                           provider_requests=data.get("search_result", {}).get("provider_requests"))
            report["runs"].append(row)
        current = client.get(f"/api/cases/{case['id']}", headers=owner).json()
        report["business_case_unchanged"] = current == case
    report["passed"] = (all(row["status"] == 200 for row in report["runs"])
                        and report["business_case_unchanged"]
                        and report["runs"][0].get("route", {}).get("planner_mode") == "not_called"
                        and report["runs"][1].get("route", {}).get("planner_mode") == "live"
                        and report["runs"][1].get("quality", {}).get("passed", False)
                        and report["runs"][2].get("quality", {}).get("passed", False))
    if report["passed"]:
        all_run = report["runs"][2]
        report["passed"] = (all_run["hit_count"] > 0
                            and not all_run["route"]["disabled"]
                            and bool(all_run["provider_requests"]["embedding"])
                            and all_run["provider_requests"]["rerank"].get("attempts", 0) > 0)
    output = ROOT / "artifacts/benchmarks/task-04-02-routing-live.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "statuses": [row["status"]
                      for row in report["runs"]], "report": str(output)}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
