"""独立合成案件的三条专业流程真实取证；不自动批准或执行处置。"""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine, func, select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from quality.config import Settings  # noqa: E402
from quality.evaluation import evaluation_code_fingerprint  # noqa: E402
from quality.main import create_app  # noqa: E402
from quality.models import ApprovalRecord, Base, SimulatedEffect, WorkOrder  # noqa: E402


def create_fixture(client, workflow, owner):
    payload = ({"sku_id": "sku-hit", "model": "K170", "as_of": "2025-07-01"}
               if workflow == "qualification" else
               {"event_id": "demo-recall-001", "as_of": "2025-07-01"})
    source = client.post("/api/cases", headers=owner, json={
        "title": "Task5 synthetic workflow fixture", "workflow":
        "incident" if workflow == "remediation" else workflow, "payload": payload})
    source.raise_for_status()
    case_id = source.json()["id"]
    fixture = {"synthetic_prior_approval": False}
    if workflow == "remediation":
        approval_id, order_id = str(uuid4()), str(uuid4())
        targets = {"event_id": "demo-recall-001", "product_ids": ["sku-hit"],
                   "batch_ids": ["batch-sku-hit"], "order_ids": ["demo-order-hit"]}
        # 仅作为整改前置数据，不是假称真实事件已获人工批准或已执行下架。
        with client.app.state.sessions.begin() as session:
            session.add(ApprovalRecord(
                id=approval_id, case_id=case_id, revision=1, analysis_run_id=str(uuid4()),
                graph_thread_id="synthetic-prior-approval", graph_kind="impact",
                decision="approve", status="active", actor_id="reviewer",
                note="SYNTHETIC fixture only; no human acceptance", targets=targets,
                snapshot_sha256=hashlib.sha256(json.dumps(targets).encode()).hexdigest()))
            session.flush()
            session.add(WorkOrder(id=order_id, case_id=case_id, approval_id=approval_id,
                                  merchant_id="merchant-a", assigned_to="reviewer",
                                  product_ids=["sku-hit"], batch_ids=["batch-sku-hit"],
                                  is_demo=True))
        created = client.post(f"/api/work-orders/{order_id}/review-cases", headers=owner, json={
            "expected_revision": 1, "review_kind": "remediation", "as_of": "2025-07-01",
            "statement": "模拟整改提交新增措施和证据，请审核员核验，不代表安全认证。"})
        created.raise_for_status()
        case_id = created.json()["case_id"]
        fixture = {"synthetic_prior_approval": True, "work_order_id": order_id,
                   "notice": "前置工单/批准为直接构造的合成记录，未调用decision/execute"}
    path = f"/api/cases/{case_id}"
    documents = []
    if workflow == "qualification":
        documents = [
            ("certificate", "DEMO CERTIFICATE NUMBER: C2025-001 MODEL: K170"),
            ("label", "DEMO LABEL MODEL: K170 POWER: 1800W"),
            ("test_report", "DEMO TEST REPORT MODEL: K170 POWER: 1800W")]
    elif workflow == "remediation":
        documents = [("remediation", "DEMO REMEDIATION evidence and corrective measures "
                      "MODEL: K170 POWER: 1800W; new label is not proof of internal safety.")]
    for name, text in documents:
        upload = client.post(path + "/materials", headers=owner, files={
            "file": (name + ".html", ("<p>" + text + "</p>").encode(), "text/html")})
        upload.raise_for_status()
    return path, fixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--prepare-only", action="store_true", help="只验证本地夹具，不调用模型")
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--interval-seconds", type=float, default=15)
    args = parser.parse_args()
    if args.repetitions < 1 or args.interval_seconds < 0:
        parser.error("次数须为正整数，间隔非负")
    if not args.prepare_only and not args.confirm_live:
        parser.error("真实请求需confirm-live")
    run_id = uuid4().hex[:12]
    output = ROOT / f"artifacts/benchmarks/task-05-workflows-{run_id}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    runtime = ROOT / f"data/runtime/task05-workflows-{run_id}"
    runtime.mkdir(parents=True)
    os.environ["QUALITY_METERING_FILE"] = str(output.with_suffix(".jsonl"))
    source_hash = evaluation_code_fingerprint(ROOT)
    runner_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "code_sha256": source_hash,
              "runner_sha256": runner_hash, "metering_events": output.with_suffix(".jsonl").name,
              "scope": "synthetic_professional_branch_integration_not_chained_human_approval",
              "storage": "isolated_SQLite_per_workflow_per_repetition", "human_verified_count": 0,
              "account_bill_verified": False, "business_actions_executed": False,
              "prepare_only": args.prepare_only, "repetitions": args.repetitions,
              "run_complete": False, "runs": []}
    def save():
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for repetition in range(args.repetitions):
        for workflow in ("qualification", "incident", "remediation"):
            settings = Settings(_env_file=None if args.prepare_only else ROOT / ".env.live",
                                model_mode="fake" if args.prepare_only else "live",
                                database_url=f"sqlite:///{runtime / f'{workflow}-{repetition}.db'}",
                                upload_dir=runtime / f"{workflow}-{repetition}-uploads")
            if settings.agnes_base_url != "https://apihub.agnes-ai.com/v1":
                parser.error("只使用既有Agnes官方入口")
            if not args.prepare_only and not settings.agnes_api_key:
                parser.error("尚未配置私有Agnes Key")
            engine = create_engine(settings.database_url)
            Base.metadata.create_all(engine)
            engine.dispose()
            with TestClient(create_app(settings, InMemorySaver())) as client:
                def auth(username):
                    response = client.post("/api/auth/login", json={"username": username,
                                                                  "password": "demo-only"})
                    response.raise_for_status()
                    return {"Authorization": "Bearer " + response.json()["access_token"]}
                owner, reviewer, admin = auth("merchant-a"), auth("reviewer"), auth("admin")
                client.post("/api/admin/import-demo", headers=admin).raise_for_status()
                client.post("/api/admin/index-rules", headers=admin).raise_for_status()
                path, fixture = create_fixture(client, workflow, owner)
                client.post(path + "/submit", headers=owner).raise_for_status()
                row = {"workflow": workflow, "repetition": repetition, "fixture": fixture,
                       "content_quality": "pending_manual_review", "model_called": False}
                report["inflight"] = {"workflow": workflow, "repetition": repetition}
                save()
                if not args.prepare_only:
                    started = time.perf_counter()
                    response = client.post(path + "/analyze", headers=reviewer)
                    row.update(http_status=response.status_code,
                               wall_ms=(time.perf_counter() - started) * 1000,
                               analysis=response.json(), model_called=True)
                    branches = row["analysis"].get("branches", {})
                    row["protocol_completed"] = response.status_code == 200 and set(branches) == {
                        "rules", "product", "material", "history"
                    } and all(
                        not branch.get("error_code")
                        and branch.get("status") in {"ready", "needs_info", "no_history"}
                        and branch.get("engine_mode") in {"live", "deterministic"}
                        for branch in branches.values())
                    row["recommendation"] = row["analysis"].get("recommendation")
                    row["missing_fields"] = row["analysis"].get("missing_fields", [])
                    with client.app.state.sessions() as session:
                        row["simulated_effects"] = session.scalar(
                            select(func.count()).select_from(SimulatedEffect))
                row["case_status"] = client.get(path, headers=owner).json()["status"]
                report["runs"].append(row)
                report["inflight"] = None
                save()
                print(f"Workflow completed {workflow}, repetition {repetition + 1}", flush=True)
            if not args.prepare_only:
                time.sleep(args.interval_seconds)
    if (evaluation_code_fingerprint(ROOT) != source_hash
            or hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != runner_hash):
        raise RuntimeError("测试期间代码变化，保留原数据但拒绝标记完成")
    report["run_complete"] = True
    save()
    print(json.dumps({"report": str(output), "count": len(report["runs"]),
                      "prepare_only": args.prepare_only}, ensure_ascii=False))


if __name__ == "__main__":
    main()
