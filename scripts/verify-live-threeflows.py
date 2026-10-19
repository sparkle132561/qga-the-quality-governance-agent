"""live三流程主线核验：准入缺件补件→事件影响→整改关闭；逐分支断言并限速。"""

import argparse
import json
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
AS_OF = "2025-07-01"


def protocol_failures(result):
    """缺件是业务结论；分支缺失、调用失败或模式不符才是协议失败。"""
    branches = result.get("branches", {})
    return [
        name for name in ("rules", "product", "material", "history")
        if name not in branches
        or branches[name].get("error_code")
        or branches[name].get("status") not in {"ready", "needs_info", "no_history"}
        or branches[name].get("engine_mode") not in {"live", "deterministic"}
    ]


def protocol_only(result):
    missing = result.get("missing_fields") or []
    derivative = {"product_evidence_coverage", "history_evidence_review", "review_evidence_basis"}
    return bool(protocol_failures(result)) and all(
        name.startswith("branch_review:") or name in derivative for name in missing
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--interval-seconds", type=float, default=15.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("拒绝覆盖既有结果")
    client = httpx.Client(base_url=args.base_url, timeout=180)
    tokens = {}
    steps = []

    def login(username):
        response = client.post(
            "/api/auth/login", json={"username": username, "password": "demo-only"}
        )
        response.raise_for_status()
        tokens[username] = {"Authorization": "Bearer " + response.json()["access_token"]}

    def step(name, detail):
        steps.append({"step": name, **detail})
        line = json.dumps(detail, ensure_ascii=False)[:150]
        print(f"[{len(steps):02}] {name}: {line}", flush=True)

    def submit(case_id):
        client.post(
            f"/api/cases/{case_id}/submit", headers=tokens["merchant-a"]
        ).raise_for_status()

    def pace():
        if args.interval_seconds > 0:
            time.sleep(args.interval_seconds)

    for username in ("merchant-a", "reviewer", "admin"):
        login(username)

    client.post("/api/admin/import-demo", headers=tokens["admin"]).raise_for_status()
    client.post("/api/admin/index-rules", headers=tokens["admin"]).raise_for_status()
    stamp = str(int(time.time()))
    # ── 流程一：准入（先缺检测报告，补件后通过）────────────────────────
    case = client.post(
        "/api/cases",
        headers=tokens["merchant-a"],
        json={
            "title": f"live三流程准入-{stamp}",
            "workflow": "qualification",
            "payload": {"sku_id": "sku-hit", "as_of": AS_OF},
        },
    )
    case.raise_for_status()
    qualification_id = case.json()["id"]

    def upload(name, text):
        response = client.post(
            f"/api/cases/{qualification_id}/materials",
            headers=tokens["merchant-a"],
            files={"file": (name, text.encode(), "text/html")},
        )
        response.raise_for_status()

    def analyze(case_id, label, attempts=3, retry=True):
        """运行分析；纯分支协议失败时按业务恢复路径重试，最多共三次尝试。"""

        result = None
        for index in range(attempts if retry else 1):
            client.post(
                f"/api/cases/{case_id}/submit", headers=tokens["merchant-a"]
            ).raise_for_status()
            pace()
            if index:
                time.sleep(30)
            response = client.post(f"/api/cases/{case_id}/analyze", headers=tokens["reviewer"])
            response.raise_for_status()
            result = response.json()
            if not protocol_only(result):
                return result
            step(f"{label}协议性失败重试{index + 1}", {"missing_fields": result["missing_fields"]})
        return result

    upload(
        "certificate.html",
        "DEMO CERTIFICATE MANUFACTURER: 演示品牌电器有限公司 MODEL: K170"
        " CERTIFICATE NO: DEMO-CCC-K170 POWER: 1800W",
    )
    upload("label.html", "DEMO LABEL MODEL: K170 POWER: 1800W")
    first_run = analyze(qualification_id, "准入首轮", retry=False)
    first_case = client.get(
        f"/api/cases/{qualification_id}", headers=tokens["merchant-a"]
    ).json()
    step(
        "准入首轮分析",
        {"recommendation": first_run.get("recommendation"), "case_status": first_case["status"],
         "missing_fields": first_run.get("missing_fields"), "branches": first_run.get("branches")},
    )
    assert not protocol_failures(first_run), first_run
    assert "document:test_report" in first_run.get("missing_fields", []), first_run
    assert first_run.get("recommendation") == "needs_info", first_run
    assert first_case["status"] == "needs_info", first_case

    upload("report.html", "DEMO TEST REPORT MODEL: K170 POWER: 1800W RESULT: PASS")
    second_run = analyze(qualification_id, "准入补件后")
    step("准入补件后分析", {"recommendation": second_run.get("recommendation")})
    assert not protocol_failures(second_run), second_run
    assert second_run.get("recommendation") == "review_ready", second_run

    approve = client.post(
        f"/api/cases/{qualification_id}/decision",
        headers=tokens["reviewer"],
        json={
            "expected_revision": second_run["revision"],
            "analysis_run_id": second_run["run_id"],
            "decision": "approve",
            "note": "补件齐全，批准准入",
        },
    )
    approve.raise_for_status()
    approval_id = approve.json()["id"]
    listed = client.post(
        f"/api/cases/{qualification_id}/approvals/{approval_id}/execute",
        headers=tokens["reviewer"],
        json={
            "expected_revision": second_run["revision"],
            "analysis_run_id": second_run["run_id"],
            "simulation_only": True,
        },
    )
    listed.raise_for_status()
    kinds = [effect["kind"] for effect in listed.json()["effects"]]
    step("准入执行", {"effects": kinds})
    assert kinds == ["simulate_listing"], kinds

    # ── 流程二：质量事件影响分析（demo-recall-001）────────────────────
    incident_case = client.post(
        "/api/cases",
        headers=tokens["merchant-a"],
        json={
            "title": f"live三流程影响-{stamp}",
            "workflow": "incident",
            "payload": {"event_id": "demo-recall-001", "as_of": AS_OF},
        },
    )
    incident_case.raise_for_status()
    incident_id = incident_case.json()["id"]
    impact_run = analyze(incident_id, "影响")
    step(
        "影响分析",
        {
            "recommendation": impact_run.get("recommendation"),
            "affected": (impact_run.get("impact") or {}).get("affected_product_ids"),
        },
    )
    assert not protocol_failures(impact_run), impact_run
    assert impact_run.get("recommendation") == "review_ready", impact_run
    affected = (impact_run.get("impact") or {}).get("affected_product_ids")
    assert affected == ["sku-hit"], impact_run.get("impact")

    impact_approve = client.post(
        f"/api/cases/{incident_id}/decision",
        headers=tokens["reviewer"],
        json={
            "expected_revision": impact_run["revision"],
            "analysis_run_id": impact_run["run_id"],
            "decision": "approve",
            "note": "受影响批次与订单已核对，批准处置",
        },
    )
    impact_approve.raise_for_status()
    impact_approval_id = impact_approve.json()["id"]
    disposed = client.post(
        f"/api/cases/{incident_id}/approvals/{impact_approval_id}/execute",
        headers=tokens["reviewer"],
        json={
            "expected_revision": impact_run["revision"],
            "analysis_run_id": impact_run["run_id"],
            "simulation_only": True,
        },
    )
    disposed.raise_for_status()
    effects = disposed.json()["effects"]
    order_id = next(e["target_id"] for e in effects if e["kind"] == "create_work_order")
    step("处置执行", {"effects": [e["kind"] for e in effects], "work_order": order_id})
    delisted = [e["target_id"] for e in effects if e["kind"] == "simulate_delist"]
    assert delisted == ["sku-hit"], effects

    # ── 流程三：商家整改→复核关闭 ─────────────────────────────────────
    orders = client.get("/api/work-orders", headers=tokens["merchant-a"]).json()
    order = next(o for o in (orders if isinstance(orders, list) else orders.get("items", []))
                 if o["id"] == order_id)
    review = client.post(
        f"/api/work-orders/{order_id}/review-cases",
        headers=tokens["merchant-a"],
        json={
            "expected_revision": order["revision"],
            "review_kind": "remediation",
            "statement": "已完成把手加固整改并复检合格，申请恢复",
            "as_of": AS_OF,
        },
    )
    review.raise_for_status()
    remediation_id = review.json()["case_id"]
    remediation_file = {
        "file": ("remediation.html", b"DEMO REMEDIATION MODEL: K170 HANDLE FIXED", "text/html")
    }
    client.post(
        f"/api/cases/{remediation_id}/materials",
        headers=tokens["merchant-a"],
        files=remediation_file,
    ).raise_for_status()
    remediation_run = analyze(remediation_id, "整改复核")
    step("整改复核分析", {"recommendation": remediation_run.get("recommendation")})
    assert not protocol_failures(remediation_run), remediation_run
    assert remediation_run.get("recommendation") == "review_ready", remediation_run

    close_approve = client.post(
        f"/api/cases/{remediation_id}/decision",
        headers=tokens["reviewer"],
        json={
            "expected_revision": remediation_run["revision"],
            "analysis_run_id": remediation_run["run_id"],
            "decision": "approve",
            "note": "整改证据核验通过，关闭工单",
        },
    )
    close_approve.raise_for_status()
    closed = client.post(
        f"/api/cases/{remediation_id}/approvals/{close_approve.json()['id']}/execute",
        headers=tokens["reviewer"],
        json={
            "expected_revision": remediation_run["revision"],
            "analysis_run_id": remediation_run["run_id"],
            "simulation_only": True,
        },
    )
    closed.raise_for_status()
    close_kinds = [e["kind"] for e in closed.json()["effects"]]
    step("整改关闭执行", {"effects": close_kinds})
    assert close_kinds == ["close_work_order"], close_kinds
    order_after = client.get("/api/work-orders", headers=tokens["merchant-a"]).json()
    items = order_after if isinstance(order_after, list) else order_after.get("items", [])
    final = next(o for o in items if o["id"] == order_id)
    step("工单终态", {"status": final["status"]})
    assert final["status"] == "closed", final

    args.output.write_text(
        json.dumps(
            {
                "base_url": args.base_url,
                "completed": True,
                "qualification_case": qualification_id,
                "incident_case": incident_id,
                "remediation_case": remediation_id,
                "work_order": order_id,
                "steps": steps,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print("PASS live three flows: listing→delist→closure")


if __name__ == "__main__":
    main()
