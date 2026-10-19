import hashlib
import json
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from fastapi import Depends, HTTPException
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from quality.domain import Actor, Role
from quality.models import (
    ApprovalRecord,
    AuditEvent,
    Batch,
    DemoOrder,
    Product,
    ProductListing,
    SimulatedEffect,
    WorkOrder,
    utcnow,
)


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    analysis_run_id: str = Field(min_length=1, max_length=36)
    decision: Literal["approve", "reject", "request_info"]
    note: str = Field(min_length=1, max_length=500)

    @field_validator("note")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("审核说明不能为空白")
        return value.strip()


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    analysis_run_id: str = Field(min_length=1, max_length=36)
    simulation_only: Literal[True] = True


def snapshot_digest(snapshot):
    return hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def register_approval_routes(app, sessions, actor, visible_case, workflow_graph, workflow_snapshot):
    def reviewer(who):
        if who.role != Role.REVIEWER:
            raise HTTPException(403, "只有审核员可以决定或执行模拟处置")

    def output(row):
        return {
            "id": row.id,
            "case_id": row.case_id,
            "revision": row.revision,
            "analysis_run_id": row.analysis_run_id,
            "decision": row.decision,
            "status": row.status,
            "actor_id": row.actor_id,
            "note": row.note,
            "targets": row.targets,
            "snapshot_sha256": row.snapshot_sha256,
            "executed_at": row.executed_at,
            "created_at": row.created_at,
            "is_demo": True,
        }

    def saved_snapshot(graph, thread_id):
        state = graph.get_state({"configurable": {"thread_id": thread_id}})
        if not state.values.get("summary"):
            raise HTTPException(409, "取证checkpoint缺失，需重新分析")
        return state, {key: state.values[key] for key in ("facts", "source_hits", "checks")}

    def check_memory(session, case, state, snapshot):
        from quality.memory import select_memories

        if "reference_memory" in state.values and state.values[
            "reference_memory"
        ] != select_memories(
            session,
            case.merchant_id,
            snapshot,
            app.state.memory_source_snapshots,
        ):
            raise HTTPException(409, "分析参考记忆已变化，请撤回并重新取证")

    def destination_versions(session, product_ids):
        result = {}
        for product_id in sorted(product_ids):
            listing = session.get(ProductListing, product_id)
            result[product_id] = {
                "version": listing.version if listing else 0,
                "status": listing.status if listing else None,
                "last_approval_id": listing.last_approval_id if listing else None,
            }
        return result

    @app.get("/api/cases/{case_id}/approvals")
    def approvals(case_id: str, who: Actor = Depends(actor)):
        with sessions() as session:
            visible_case(session, case_id, who)
            return [
                output(row)
                for row in session.scalars(
                    select(ApprovalRecord)
                    .where(ApprovalRecord.case_id == case_id)
                    .order_by(ApprovalRecord.created_at.desc())
                )
            ]

    @app.post("/api/cases/{case_id}/decision")
    def decide(case_id: str, request: DecisionRequest, who: Actor = Depends(actor)):
        reviewer(who)
        identifier = str(uuid5(NAMESPACE_URL, case_id + ":decision:" + request.analysis_run_id))
        with sessions.begin() as session:
            case = visible_case(session, case_id, who, lock=True)
            existing = session.get(ApprovalRecord, identifier)
            if existing:
                if (
                    existing.revision != request.expected_revision
                    or existing.decision != request.decision
                    or existing.note != request.note
                    or existing.actor_id != who.user_id
                ):
                    raise HTTPException(409, "同一分析的决定不可覆盖，请重新分析")
                return {**output(existing), "idempotent": True}
            analysis = case.latest_analysis or {}
            if (
                case.status != "waiting_review"
                or case.revision != request.expected_revision
                or analysis.get("revision") != case.revision
                or analysis.get("run_id") != request.analysis_run_id
            ):
                raise HTTPException(409, "案件状态、资料或分析版本已变化")
            if not case.is_demo or analysis.get("engine_mode") not in {"fake", "live"}:
                raise HTTPException(409, "此接口仅处理具有完整取证的本地演示案件")
            if request.decision == "approve" and analysis.get("recommendation") != "review_ready":
                raise HTTPException(409, "拟拒绝或缺证据分析不能直接通过")
            kind = {
                "qualification": "qualification",
                "incident": "impact",
                "remediation": "remediation",
            }.get(case.workflow)
            if kind is None:
                raise HTTPException(409, "此流程审批尚未接通")
            graph = workflow_graph(kind)
            config = {"configurable": {"thread_id": analysis["thread_id"]}}
            state, original = saved_snapshot(graph, analysis["thread_id"])
            current = workflow_snapshot(session, case, kind)
            if current != original:
                raise HTTPException(409, "取证来源已变化，请撤回或重新分析")
            check_memory(session, case, state, current)
            impact = analysis.get("impact") or {}
            target_ids = (
                impact.get("affected_product_ids", [])
                if kind == "impact"
                else [case.payload["sku_id"]]
                if kind == "qualification"
                else []
            )
            review_order = None
            if kind == "remediation":
                review_order = session.scalar(
                    select(WorkOrder)
                    .where(
                        WorkOrder.id == case.payload.get("work_order_id"),
                        WorkOrder.merchant_id == case.merchant_id,
                    )
                    .with_for_update()
                )
                if (
                    review_order is None
                    or review_order.active_review_case_id != case.id
                    or review_order.revision != case.payload.get("work_order_revision")
                    or review_order.status == "closed"
                ):
                    raise HTTPException(409, "工单复核版本已变化")
            locked_targets = list(
                session.scalars(
                    select(Product)
                    .where(Product.id.in_(target_ids), Product.merchant_id == case.merchant_id)
                    .order_by(Product.id)
                    .with_for_update()
                )
            )
            if {product.id for product in locked_targets} != set(target_ids):
                raise HTTPException(409, "批准目标已变化")
            envelope = {
                "id": identifier,
                "case_id": case_id,
                "revision": case.revision,
                "run_id": request.analysis_run_id,
                "decision": request.decision,
                "actor": who.model_dump(mode="json"),
                "note": request.note,
                "destination_versions": destination_versions(session, target_ids),
            }
            prior = state.values.get("approval_decision")
            if prior:
                if "destination_versions" not in prior:
                    raise HTTPException(409, "旧checkpoint未绑定处置版本，需撤回重新分析")
                envelope["destination_versions"] = prior["destination_versions"]
                if prior != envelope:
                    raise HTTPException(409, "checkpoint已有不同决定，不能覆盖")
            elif state.next == ("human_gate",):
                resumed = graph.invoke(Command(resume=envelope), config)
                if resumed.get("approval_decision") != envelope:
                    raise HTTPException(409, "审批恢复未完成")
            else:
                raise HTTPException(409, "没有可恢复的人工审批中断")
            targets = {
                "product_ids": target_ids,
                "batch_ids": impact.get("affected_batch_ids", []),
                "order_ids": impact.get("affected_order_ids", []),
                "event_id": case.payload.get("event_id"),
                "destination_versions": envelope["destination_versions"],
                "work_order_id": review_order.id if review_order else None,
                "work_order_revision": review_order.revision if review_order else None,
                "review_product_ids": review_order.product_ids if review_order else [],
            }
            row = ApprovalRecord(
                id=identifier,
                case_id=case_id,
                revision=case.revision,
                analysis_run_id=request.analysis_run_id,
                graph_thread_id=analysis["thread_id"],
                graph_kind=kind,
                decision=request.decision,
                status="active" if request.decision == "approve" else "closed",
                actor_id=who.user_id,
                note=request.note,
                snapshot_sha256=snapshot_digest(original),
                targets=targets,
            )
            session.add(row)
            case.status = (
                ("confirmed" if kind == "impact" else "approved")
                if request.decision == "approve"
                else "rejected"
                if request.decision == "reject"
                else "needs_info"
            )
            if review_order:
                if request.decision == "reject":
                    review_order.status = "escalated"
                elif request.decision == "request_info":
                    review_order.status = "in_progress"
            session.add(
                AuditEvent(
                    case_id=case_id,
                    actor_id=who.user_id,
                    action="human_decision",
                    details={
                        "approval_id": identifier,
                        "decision": request.decision,
                        "revision": case.revision,
                    },
                )
            )
            session.flush()
            return {**output(row), "idempotent": False}

    def record(session, case_id, identifier):
        row = session.scalar(
            select(ApprovalRecord)
            .where(ApprovalRecord.id == identifier, ApprovalRecord.case_id == case_id)
            .with_for_update()
        )
        if row is None:
            raise HTTPException(404, "审批记录不存在")
        return row

    @app.post("/api/cases/{case_id}/withdraw-analysis")
    def withdraw_analysis(case_id: str, request: ExecuteRequest, who: Actor = Depends(actor)):
        reviewer(who)
        with sessions.begin() as session:
            case = visible_case(session, case_id, who, lock=True)
            if (
                case.status != "waiting_review"
                or case.revision != request.expected_revision
                or (case.latest_analysis or {}).get("run_id") != request.analysis_run_id
            ):
                raise HTTPException(409, "当前待审版本已变化")
            case.status = "submitted"
            session.add(
                AuditEvent(
                    case_id=case_id,
                    actor_id=who.user_id,
                    action="withdraw_pending_analysis",
                    details={
                        "analysis_run_id": request.analysis_run_id,
                        "checkpoint_retained": True,
                    },
                )
            )
            return {"status": case.status, "analysis_run_id": request.analysis_run_id}

    @app.post("/api/cases/{case_id}/approvals/{identifier}/revoke")
    def revoke(case_id: str, identifier: str, who: Actor = Depends(actor)):
        reviewer(who)
        with sessions.begin() as session:
            case = visible_case(session, case_id, who, lock=True)
            row = record(session, case_id, identifier)
            if row.status != "active" or row.executed_at:
                raise HTTPException(409, "此决定已撤回或已执行，不能重复撤回")
            if (
                case.revision != row.revision
                or (case.latest_analysis or {}).get("run_id") != row.analysis_run_id
            ):
                raise HTTPException(409, "当前案件版本已不同，不能撤回其他版本")
            row.status, case.status = "revoked", "submitted"
            session.add(
                AuditEvent(
                    case_id=case_id,
                    actor_id=who.user_id,
                    action="revoke_decision",
                    details={"approval_id": row.id},
                )
            )
            return output(row)

    @app.post("/api/cases/{case_id}/approvals/{identifier}/execute")
    def execute(
        case_id: str, identifier: str, request: ExecuteRequest, who: Actor = Depends(actor)
    ):
        reviewer(who)
        with sessions.begin() as session:
            case = visible_case(session, case_id, who, lock=True)
            row = record(session, case_id, identifier)
            if (
                row.status != "active"
                or row.decision != "approve"
                or row.revision != case.revision
                or request.expected_revision != case.revision
                or request.analysis_run_id != row.analysis_run_id
                or (case.latest_analysis or {}).get("run_id") != row.analysis_run_id
            ):
                raise HTTPException(409, "未批准、已撤回或审批版本不符")

            def receipts():
                return [
                    {
                        "id": effect.id,
                        "kind": effect.kind,
                        "target_id": effect.target_id,
                        "details": effect.details,
                        "is_demo": effect.is_demo,
                    }
                    for effect in session.scalars(
                        select(SimulatedEffect).where(SimulatedEffect.approval_id == row.id)
                    )
                ]

            if row.executed_at:
                return {"idempotent": True, "effects": receipts(), "is_demo": True}
            expected_status = "confirmed" if row.graph_kind == "impact" else "approved"
            if case.status != expected_status or not case.is_demo:
                raise HTTPException(409, "案件未处于本地模拟处置允许状态")
            # 整改不写listing，但同准入保持Product→WorkOrder锁顺序。
            review_products = (
                list(
                    session.scalars(
                        select(Product)
                        .where(
                            Product.id.in_(row.targets.get("review_product_ids", [])),
                            Product.merchant_id == case.merchant_id,
                        )
                        .order_by(Product.id)
                        .with_for_update()
                    )
                )
                if row.graph_kind == "remediation"
                else []
            )
            products = list(
                session.scalars(
                    select(Product)
                    .where(
                        Product.id.in_(row.targets["product_ids"]),
                        Product.merchant_id == case.merchant_id,
                    )
                    .order_by(Product.id)
                    .with_for_update()
                )
            )
            if {product.id for product in products} != set(row.targets["product_ids"]) or any(
                not p.is_demo for p in products
            ):
                raise HTTPException(409, "目标不属于当前商家或不是模拟数据")
            batches = list(
                session.scalars(
                    select(Batch)
                    .where(
                        Batch.id.in_(row.targets["batch_ids"]),
                        Batch.product_id.in_(row.targets["product_ids"]),
                    )
                    .order_by(Batch.id)
                    .with_for_update()
                )
            )
            orders = list(
                session.scalars(
                    select(DemoOrder)
                    .where(
                        DemoOrder.id.in_(row.targets["order_ids"]),
                        DemoOrder.merchant_id == case.merchant_id,
                        DemoOrder.batch_id.in_(row.targets["batch_ids"]),
                    )
                    .order_by(DemoOrder.id)
                    .with_for_update()
                )
            )
            if (
                {batch.id for batch in batches} != set(row.targets["batch_ids"])
                or {order.id for order in orders} != set(row.targets["order_ids"])
                or any(not value.is_demo for value in [*batches, *orders])
            ):
                raise HTTPException(409, "批准批次或订单的归属、关系或模拟标记不符")
            if destination_versions(session, row.targets["product_ids"]) != row.targets.get(
                "destination_versions"
            ):
                raise HTTPException(409, "商品处置版本已变化，旧批准不能覆盖新状态")
            current = workflow_snapshot(session, case, row.graph_kind)
            if snapshot_digest(current) != row.snapshot_sha256:
                raise HTTPException(409, "批准后的来源已变化，不得执行旧目标")
            saved_state, _ = saved_snapshot(workflow_graph(row.graph_kind), row.graph_thread_id)
            check_memory(session, case, saved_state, current)
            if row.graph_kind == "qualification":
                open_orders = session.scalars(
                    select(WorkOrder)
                    .where(WorkOrder.merchant_id == case.merchant_id, WorkOrder.status != "closed")
                    .order_by(WorkOrder.id)
                    .limit(101)
                    .with_for_update()
                )
                pending = list(open_orders)
                if len(pending) > 100 or any(
                    set(value.product_ids) & set(row.targets["product_ids"]) for value in pending
                ):
                    raise HTTPException(409, "相关事件工单尚未关闭，不能以准入上架覆盖治理状态")

            def effect(kind, target, details):
                effect_id = str(uuid5(NAMESPACE_URL, row.id + ":" + kind + ":" + target))
                session.add(
                    SimulatedEffect(
                        id=effect_id,
                        approval_id=row.id,
                        case_id=case_id,
                        kind=kind,
                        target_id=target,
                        details=details,
                        is_demo=True,
                    )
                )

            for product in products:
                listing = session.get(ProductListing, product.id)
                if listing is None:
                    listing = ProductListing(
                        product_id=product.id,
                        status="unrecorded",
                        version=0,
                        last_approval_id=row.id,
                        is_demo=True,
                    )
                    session.add(listing)
                listing.status = "listed" if row.graph_kind == "qualification" else "delisted"
                listing.last_approval_id = row.id
                listing.version += 1
                effect(
                    "simulate_listing" if row.graph_kind == "qualification" else "simulate_delist",
                    product.id,
                    {"listing_status": listing.status, "scope_batch_ids": row.targets["batch_ids"]},
                )
            if row.graph_kind == "impact" and products:
                effect(
                    "simulate_notification",
                    case.merchant_id,
                    {
                        "transport": "local_ledger_only",
                        "product_ids": row.targets["product_ids"],
                        "order_ids": row.targets["order_ids"],
                    },
                )
                order_id = str(uuid5(NAMESPACE_URL, row.id + ":work-order"))
                session.add(
                    WorkOrder(
                        id=order_id,
                        case_id=case_id,
                        approval_id=row.id,
                        merchant_id=case.merchant_id,
                        status="open",
                        assigned_to=case.merchant_id,
                        product_ids=row.targets["product_ids"],
                        batch_ids=row.targets["batch_ids"],
                        is_demo=True,
                    )
                )
                effect("create_work_order", order_id, {"assigned_to": case.merchant_id})
            if row.graph_kind == "remediation":
                order = session.scalar(
                    select(WorkOrder)
                    .where(
                        WorkOrder.id == row.targets.get("work_order_id"),
                        WorkOrder.merchant_id == case.merchant_id,
                    )
                    .with_for_update()
                )
                if (
                    order is None
                    or order.active_review_case_id != case.id
                    or not order.is_demo
                    or order.revision != row.targets.get("work_order_revision")
                    or order.status == "closed"
                ):
                    raise HTTPException(409, "当前工单不能按此批准关闭")
                review_batches = list(
                    session.scalars(
                        select(Batch)
                        .where(
                            Batch.id.in_(order.batch_ids),
                            Batch.product_id.in_(order.product_ids),
                        )
                        .order_by(Batch.id)
                        .with_for_update()
                    )
                )
                if (
                    {p.id for p in review_products} != set(order.product_ids)
                    or {b.id for b in review_batches} != set(order.batch_ids)
                    or any(not item.is_demo for item in [*review_products, *review_batches])
                    or snapshot_digest(workflow_snapshot(session, case, row.graph_kind))
                    != row.snapshot_sha256
                ):
                    raise HTTPException(409, "工单目标归属、模拟标记或资料已变化")
                order.status, order.closure_note = "closed", row.note
                order.revision += 1
                effect(
                    "close_work_order",
                    order.id,
                    {
                        "review_kind": case.payload["review_kind"],
                        "closure_note": row.note,
                        "listing_restored": False,
                    },
                )
            row.executed_at = utcnow()
            session.add(
                AuditEvent(
                    case_id=case_id,
                    actor_id=who.user_id,
                    action="execute_simulation",
                    details={"approval_id": row.id, "product_ids": row.targets["product_ids"]},
                )
            )
            session.flush()
            return {"idempotent": False, "effects": receipts(), "is_demo": True}

    @app.get("/api/cases/{case_id}/effects")
    def effects(case_id: str, who: Actor = Depends(actor)):
        with sessions() as session:
            visible_case(session, case_id, who)
            return [
                {
                    "id": effect.id,
                    "kind": effect.kind,
                    "target_id": effect.target_id,
                    "details": effect.details,
                    "is_demo": effect.is_demo,
                    "created_at": effect.created_at,
                }
                for effect in session.scalars(
                    select(SimulatedEffect).where(SimulatedEffect.case_id == case_id)
                )
            ]
