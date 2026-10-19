"""确定性案件摘要：版本绑定的派生视图，不生成新事实、不提供处置许可。"""

import hashlib
import json
from types import SimpleNamespace

from fastapi import Depends, HTTPException, Query
from sqlalchemy import select

from quality.models import ApprovalRecord, SimulatedEffect

VERSION = "case-summary-v1"
CASE_FIELDS = {
    "sku_id", "model", "brand", "as_of", "event_id", "work_order_id", "work_order_revision",
    "review_kind", "batch_number", "production_date", "serial_number", "date_from", "date_to",
}


class InvalidCaseSummary(ValueError):
    pass


def digest(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def current_snapshot(app, session, case, as_of=None):
    # 查询日期覆盖仅作用于派生只读对象，不能改变SQLAlchemy业务实例或触发autoflush。
    scoped = SimpleNamespace(id=case.id, revision=case.revision, merchant_id=case.merchant_id,
                             payload={**case.payload, **({"as_of": as_of} if as_of else {})})
    kind = {"qualification": "qualification", "incident": "impact",
            "remediation": "remediation"}.get(case.workflow, "stub")
    if case.workflow == "remediation" and not case.payload.get("work_order_id"):
        kind = "stub"
    return app.state.workflow_snapshot(session, scoped, kind)


def build_case_summary(session, case, snapshot, as_of=None):
    facts = snapshot.get("facts", {})
    expected_payload = {**case.payload, **({"as_of": as_of} if as_of else {})}
    if (facts.get("case_id", case.id) != case.id
            or facts.get("merchant_id", case.merchant_id) != case.merchant_id
            or facts.get("revision", case.revision) != case.revision
            or facts.get("declared_payload", expected_payload) != expected_payload):
        raise ValueError("摘要输入快照不属于当前案件版本")
    checks = snapshot.get("checks", {})
    references = []
    for branch in ("rules", "product", "material"):
        for hit in snapshot.get("source_hits", {}).get(branch, []):
            references.append({
                "id": hit["id"], "branch": branch, "location": hit.get("location", {}),
                "sha256": hashlib.sha256(hit.get("text", "").encode()).hexdigest(),
                "source_version": {key: value for key, value in hit.items()
                                   if key not in {"id", "text", "location"}},
            })
    priority = {"rules": 0, "product": 1, "material": 2}
    references.sort(key=lambda row: (priority[row["branch"]], row["id"]))
    receipts = list(session.execute(
        select(SimulatedEffect, ApprovalRecord).join(
            ApprovalRecord, SimulatedEffect.approval_id == ApprovalRecord.id
        ).where(SimulatedEffect.case_id == case.id, ApprovalRecord.case_id == case.id,
                ApprovalRecord.decision == "approve", ApprovalRecord.executed_at.is_not(None))
        .order_by(SimulatedEffect.created_at.desc(), SimulatedEffect.id).limit(11)
    ))
    actions = [{"id": effect.id, "kind": effect.kind, "target_id": effect.target_id,
                "approval_id": approved.id, "case_revision": approved.revision,
                "analysis_run_id": approved.analysis_run_id,
                "created_at": effect.created_at.isoformat(), "is_demo": effect.is_demo}
               for effect, approved in receipts[:10]]
    analysis = case.latest_analysis if isinstance(case.latest_analysis, dict) else {}
    matches = [{key: value for key, value in row.items() if key != "paths"}
               for row in checks.get("findings", [])]
    body = {
        "version": VERSION,
        "case": {"id": case.id, "merchant_id": case.merchant_id, "workflow": case.workflow,
                 "revision": case.revision, "status": case.status},
        "case_fields_sha256": digest(case.payload),
        "case_fields": {key: value for key, value in case.payload.items() if key in CASE_FIELDS},
        "case_fields_authority": "persisted_declarations_not_certification",
        "business_scope": {key: value for key, value in facts.items() if key not in {
            "case_id", "revision", "merchant_id", "declared_payload",
        }},
        "matching_records": matches,
        "unresolved": {"missing_fields": checks.get("missing", []),
                       "conflicts": checks.get("conflicts", []),
                       "unknown_record_ids": [row.get("batch_id") for row in matches
                                              if row.get("status") == "unknown"],
                       "case_needs_info": case.status == "needs_info"},
        "evidence_refs": references[:20],
        "source_manifest_sha256": digest(references),
        "completed_actions": actions,
        "analysis_reference": {"thread_id": analysis.get("thread_id"),
                               "run_id": analysis.get("run_id"),
                               "revision": analysis.get("revision"),
                               "authority": "historical_analysis_not_current_evidence"}
                              if analysis else None,
        "scope_date": as_of if as_of else facts.get("as_of"),
        "truncated": {"evidence_refs": len(references) > 20,
                      "completed_actions": len(receipts) > 10},
        "source_counts": {"evidence_refs": len(references)},
        "authority": "derived_case_view_not_new_facts_or_action_permission",
    }
    return {**body, "content_sha256": digest(body)}


def validate_summary(summary, state):
    body = {key: value for key, value in summary.items() if key != "content_sha256"}
    subject = summary.get("case", {})
    facts = state.get("facts", {})
    actor = state.get("actor", {})
    if (summary.get("version") != VERSION or digest(body) != summary.get("content_sha256")
            or subject.get("id") != state["case_id"]
            or subject.get("revision") != facts.get("revision")
            or (facts.get("merchant_id") and subject.get("merchant_id") != facts["merchant_id"])
            or (actor.get("role") == "merchant"
                and subject.get("merchant_id") != actor.get("merchant_id"))):
        raise InvalidCaseSummary("案件摘要版本、归属或完整性已变化")
    payload = facts.get("payload", facts.get("declared_payload"))
    if payload is not None and digest(payload) != summary["case_fields_sha256"]:
        raise InvalidCaseSummary("案件摘要字段已过期")
    if any(key in facts and facts[key] != subject[key] for key in ("status", "workflow")):
        raise InvalidCaseSummary("案件摘要状态或流程已过期")
    if any(key in facts and facts[key] != value
           for key, value in summary["business_scope"].items()):
        raise InvalidCaseSummary("案件摘要业务范围已过期")
    if "retrieval_as_of" in facts and facts["retrieval_as_of"] != summary["scope_date"]:
        raise InvalidCaseSummary("案件摘要查询日期不一致")


def context_summary(summary, branch):
    # 专业分支只获得本专业来源ID；整体来源manifest用于失效，不暴露其他分支正文。
    view = {key: value for key, value in summary.items() if key != "content_sha256"}
    view.update(evidence_refs=[item for item in summary["evidence_refs"]
                               if item["branch"] == branch],
                source_summary_sha256=summary["content_sha256"], branch_projection=branch)
    return {**view, "content_sha256": digest(view)}


def register_summary_routes(app, sessions, actor, visible_case):
    @app.get("/api/cases/{case_id}/summary")
    def summary(case_id: str, as_of: str | None = Query(None), who=Depends(actor)):
        if as_of:
            from datetime import date
            try:
                date.fromisoformat(as_of)
            except ValueError:
                raise HTTPException(422, "摘要适用日期需为ISO日期") from None
        with sessions() as session:
            case = visible_case(session, case_id, who)
            return build_case_summary(session, case, current_snapshot(app, session, case, as_of),
                                      as_of)
