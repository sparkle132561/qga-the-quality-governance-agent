"""长期记忆只复用已批准来源的摘录，不保存可覆盖业务事实的自由总结。"""

from datetime import date, timedelta

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from quality.approvals import snapshot_digest
from quality.domain import Actor, Role
from quality.models import ApprovalRecord, AuditEvent, Batch, ReviewCase, VerifiedMemory, utcnow


class MemoryProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_case_id: str = Field(min_length=1, max_length=36)
    product_id: str = Field(min_length=1, max_length=80)
    source_id: str = Field(min_length=1, max_length=100)
    quote: str = Field(min_length=10, max_length=500)
    expires_on: date
    note: str = Field(min_length=1, max_length=500)

    @field_validator("note", "quote")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("摘录和核验说明不能为空")
        return value.strip()


class MemoryCorrection(MemoryProposal):
    expected_revision: int = Field(ge=1)


class MemoryDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    note: str = Field(min_length=1, max_length=500)

    @field_validator("note")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("说明不能为空")
        return value.strip()


def product_scope(snapshot):
    scope = set()
    for hit in snapshot.get("source_hits", {}).get("product", []):
        product_id = hit["location"].get("product_id")
        if product_id:
            scope.add(product_id)
        elif hit["id"].startswith("product:"):
            scope.add(hit["id"].split(":", 1)[1])
    return scope


def memory_digest(snapshot):
    # 新案件追加历史不应使旧摘录失效；当前事实、规则、材料与检查仍全部绑定。
    return snapshot_digest(
        {
            **snapshot,
            "source_hits": {
                key: value for key, value in snapshot["source_hits"].items() if key != "history"
            },
        }
    )


def source_pool(snapshot):
    return {
        hit["id"]: hit
        for name, hits in snapshot["source_hits"].items()
        if name != "history"
        for hit in hits
    }


def source_product_scope(session, source):
    if source["id"].startswith("batch:"):
        batch = session.get(Batch, source["id"].split(":", 1)[1])
        return {batch.product_id} if batch else set()
    scope = product_scope({"source_hits": {"product": [source]}})
    return scope if scope or source["id"].startswith("product:") else None


def approved_source(session, case, source_snapshots):
    analysis = case.latest_analysis or {}
    approval = session.scalar(
        select(ApprovalRecord).where(
            ApprovalRecord.case_id == case.id,
            ApprovalRecord.analysis_run_id == analysis.get("run_id"),
            ApprovalRecord.decision == "approve",
            ApprovalRecord.status == "active",
        )
    )
    if (
        approval is None
        or approval.revision != case.revision
        or analysis.get("revision") != case.revision
        or case.status not in {"approved", "confirmed"}
    ):
        raise HTTPException(409, "只有当前人工批准的来源可进入记忆候选")
    snapshot, original = source_snapshots(session, case, approval.graph_kind)
    if memory_digest(snapshot) != memory_digest(original):
        raise HTTPException(409, "原批准来源已变化，请先重新核验案件")
    return approval, snapshot


def memory_validity(session, row, source_snapshots):
    if row.status != "verified":
        return row.status
    if row.expires_on < utcnow().date():
        return "expired"
    case = session.get(ReviewCase, row.source_case_id)
    if case is None or case.merchant_id != row.merchant_id:
        return "source_changed"
    try:
        approval, snapshot = approved_source(session, case, source_snapshots)
    except HTTPException:
        return "source_changed"
    if (
        approval.id != row.approval_id
        or case.revision != row.source_revision
        or (case.latest_analysis or {}).get("run_id") != row.analysis_run_id
        or memory_digest(snapshot) != row.snapshot_sha256
        or row.product_id not in product_scope(snapshot)
    ):
        return "source_changed"
    source = source_pool(snapshot).get(row.source_id)
    scope = source_product_scope(session, source) if source else set()
    if (
        source is None
        or source["source_sha256"] != row.source_sha256
        or row.quote not in source["text"]
        or (scope is not None and row.product_id not in scope)
    ):
        return "source_changed"
    return "verified"


def select_memories(session, merchant_id, snapshot, source_snapshots):
    """按实体、日期和商家先过滤，最多检查200条，返回5条；容量截断显式标记。"""
    products = product_scope(snapshot)
    as_of = snapshot.get("facts", {}).get("as_of")
    if not products or not as_of:
        return {"items": [], "scan_truncated": False}
    rows = list(
        session.scalars(
            select(VerifiedMemory)
            .where(
                VerifiedMemory.merchant_id == merchant_id,
                VerifiedMemory.product_id.in_(products),
                VerifiedMemory.as_of == date.fromisoformat(as_of),
                VerifiedMemory.status == "verified",
                VerifiedMemory.expires_on >= utcnow().date(),
            )
            .order_by(VerifiedMemory.created_at.desc())
            .limit(201)
        )
    )
    selected = []
    for row in rows[:200]:
        if memory_validity(session, row, source_snapshots) == "verified":
            selected.append(
                {
                    "id": row.id,
                    "revision": row.revision,
                    "quote": row.quote,
                    "source_case_id": row.source_case_id,
                    "source_id": row.source_id,
                    "product_id": row.product_id,
                    "as_of": row.as_of.isoformat(),
                    "expires_on": row.expires_on.isoformat(),
                    "authority": "historical_reference_not_current_evidence",
                }
            )
            if len(selected) == 5:
                break
    return {"items": selected, "scan_truncated": len(rows) > 200}


def register_memory_routes(app, sessions, actor, visible_case, source_snapshots):
    @app.get("/api/cases/{case_id}/reference-memories")
    def case_memories(case_id: str, who: Actor = Depends(actor)):
        with sessions() as session:
            case = visible_case(session, case_id, who)
            kind = {
                "qualification": "qualification",
                "incident": "impact",
                "remediation": "remediation",
            }.get(case.workflow, "stub")
            snapshot = app.state.workflow_snapshot(session, case, kind)
            return select_memories(session, case.merchant_id, snapshot, source_snapshots)

    def visible(session, identifier, who, lock=False):
        query = select(VerifiedMemory).where(VerifiedMemory.id == identifier)
        if who.role == Role.MERCHANT:
            query = query.where(VerifiedMemory.merchant_id == who.merchant_id)
        row = session.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise HTTPException(404, "记忆不存在")
        return row

    def output(session, row):
        return {
            name: getattr(row, name)
            for name in (
                "id",
                "merchant_id",
                "product_id",
                "source_case_id",
                "source_id",
                "quote",
                "expires_on",
                "as_of",
                "status",
                "revision",
                "proposer_id",
                "verifier_id",
                "note",
            )
        } | {"effective_status": memory_validity(session, row, source_snapshots)}

    def fill(session, request, who):
        if request.expires_on < utcnow().date() or request.expires_on > utcnow().date() + timedelta(
            days=90
        ):
            raise HTTPException(422, "有效期应在今天至90天内")
        case = visible_case(session, request.source_case_id, who, lock=True)
        approval, snapshot = approved_source(session, case, source_snapshots)
        source = source_pool(snapshot).get(request.source_id)
        source_products = source_product_scope(session, source) if source else set()
        if (
            request.product_id not in product_scope(snapshot)
            or source is None
            or request.quote not in source["text"]
            or (source_products is not None and request.product_id not in source_products)
        ):
            raise HTTPException(422, "摘录或适用商品不在当前批准来源内")
        return {
            "merchant_id": case.merchant_id,
            "source_case_id": case.id,
            "product_id": request.product_id,
            "source_revision": case.revision,
            "analysis_run_id": approval.analysis_run_id,
            "approval_id": approval.id,
            "source_id": source["id"],
            "source_sha256": source["source_sha256"],
            "snapshot_sha256": memory_digest(snapshot),
            "quote": request.quote,
            "as_of": date.fromisoformat(snapshot["facts"]["as_of"]),
            "expires_on": request.expires_on,
            "note": request.note,
        }

    def audit(session, row, who, action):
        session.add(
            AuditEvent(
                case_id=row.source_case_id,
                actor_id=who.user_id,
                action=action,
                details={
                    "memory_id": row.id,
                    "revision": row.revision,
                    "status": row.status,
                    "source_sha256": row.source_sha256,
                },
            )
        )

    @app.get("/api/memories")
    def list_memory(
        who: Actor = Depends(actor),
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        with sessions() as session:
            query = select(VerifiedMemory).order_by(VerifiedMemory.created_at.desc())
            if who.role == Role.MERCHANT:
                query = query.where(VerifiedMemory.merchant_id == who.merchant_id)
            return [
                output(session, row) for row in session.scalars(query.limit(limit).offset(offset))
            ]

    @app.post("/api/memories", status_code=201)
    def propose(request: MemoryProposal, who: Actor = Depends(actor)):
        if who.role == Role.ADMIN:
            raise HTTPException(403, "管理员不能代替业务记忆核验")
        with sessions.begin() as session:
            row = VerifiedMemory(**fill(session, request, who), proposer_id=who.user_id)
            session.add(row)
            session.flush()
            audit(session, row, who, "memory_proposed")
            return output(session, row)

    @app.put("/api/memories/{identifier}")
    def correct(identifier: str, request: MemoryCorrection, who: Actor = Depends(actor)):
        if who.role != Role.REVIEWER:
            raise HTTPException(403, "只有审核员可以纠正记忆来源")
        with sessions.begin() as session:
            values = fill(session, request, who)
            row = visible(session, identifier, who, lock=True)
            if row.status == "deleted" or row.revision != request.expected_revision:
                raise HTTPException(409, "记忆版本变化或已删除")
            if row.merchant_id != values["merchant_id"]:
                raise HTTPException(409, "纠正不能改变商家归属")
            for name, value in values.items():
                setattr(row, name, value)
            row.status, row.verifier_id = "candidate", None
            row.revision += 1
            audit(session, row, who, "memory_corrected_candidate")
            return output(session, row)

    @app.post("/api/memories/{identifier}/confirm")
    def confirm(identifier: str, request: MemoryDecision, who: Actor = Depends(actor)):
        if who.role != Role.REVIEWER:
            raise HTTPException(403, "只有审核员可以确认记忆")
        with sessions.begin() as session:
            row = visible(session, identifier, who, lock=True)
            if row.status != "candidate" or row.revision != request.expected_revision:
                raise HTTPException(409, "记忆不是当前候选版本")
            row.status = "verified"
            if memory_validity(session, row, source_snapshots) != "verified":
                raise HTTPException(409, "来源过期或改变，不能确认")
            row.verifier_id, row.note = who.user_id, request.note
            row.revision += 1
            audit(session, row, who, "memory_confirmed")
            return output(session, row)

    @app.post("/api/memories/{identifier}/delete")
    def delete(identifier: str, request: MemoryDecision, who: Actor = Depends(actor)):
        if who.role == Role.ADMIN:
            raise HTTPException(403, "管理员不能代替业务记忆操作")
        with sessions.begin() as session:
            row = visible(session, identifier, who, lock=True)
            if row.revision != request.expected_revision:
                raise HTTPException(409, "记忆版本已变化")
            if row.status != "deleted":
                row.status, row.quote, row.note = "deleted", "", request.note
                row.revision += 1
                audit(session, row, who, "memory_deleted")
            return output(session, row)
