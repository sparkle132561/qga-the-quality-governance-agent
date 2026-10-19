import hashlib
import json
from datetime import date
from typing import Literal

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from quality.catalog import applicable_rules
from quality.domain import Actor, Role
from quality.models import (
    ApprovalRecord,
    AuditEvent,
    Batch,
    EvidenceChunk,
    Material,
    Product,
    QualityEvent,
    ReviewCase,
    WorkOrder,
)
from quality.qualification import snapshot_hit
from quality.retrieval_agent import FinalAdvice
from quality.workflow_agents import BRANCHES, WorkflowProfile, build_specialist_graph

TASKS = {
    "rules": (
        "核对原批准范围、当前工单及整改/申诉要求；旧批准不等于新证据已核验。"
        "已登记工单与规则条目即核对依据，调取原文全文属人工复核范围，不因此降级。"
    ),
    "product": "核对本商家被治理的商品与批次；不扩大或替换工单目标。",
    "material": (
        "核对整改或申诉材料；assessment给出已引用material来源ID和含整改/申诉关键词的原文。"
        "新标签和自述不证明内部安全或真正整改有效。"
        "演示库材料带is_demo标记属设计边界，按有效证据正常核对，不因演示标记降级。"
    ),
    "history": (
        "核对原事件处置与历史时间线，不能重复旧理由关闭当前版本。"
        "只陈述历史记录内容与限制，不评价当前案件的资料是否齐全。"
    ),
}


class EvidenceAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=100)
    quote: str = Field(min_length=10, max_length=300)


class RemediationAdvice(FinalAdvice):
    assessments: list[EvidenceAssessment] = Field(default_factory=list, max_length=20)


class ReviewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    review_kind: Literal["remediation", "appeal"]
    statement: str = Field(min_length=1, max_length=3000)
    as_of: date

    @field_validator("statement")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("说明不能为空白")
        return value.strip()


def remediation_snapshot(session, case):
    missing, conflicts = [], []
    identifier = case.payload.get("work_order_id")
    order = (
        session.scalar(
            select(WorkOrder).where(
                WorkOrder.id == identifier, WorkOrder.merchant_id == case.merchant_id
            )
        )
        if isinstance(identifier, str)
        else None
    )
    hits = {name: [] for name in BRANCHES}
    try:
        as_of = date.fromisoformat(case.payload.get("as_of", ""))
    except (TypeError, ValueError):
        as_of = None
        missing.append("as_of")
    if order is None:
        missing.append("owned_work_order")
    else:
        if (
            order.revision != case.payload.get("work_order_revision")
            or order.active_review_case_id != case.id
        ):
            missing.append("work_order_review_version")
        cause = session.get(ApprovalRecord, order.approval_id)
        record = {
            "work_order_id": order.id,
            "revision": order.revision,
            "source_case_id": order.case_id,
            "merchant_id": order.merchant_id,
            "assigned_to": order.assigned_to,
            "product_ids": order.product_ids,
            "batch_ids": order.batch_ids,
            "cause_approval_id": order.approval_id,
            "cause_targets": cause.targets,
            "cause_note": cause.note,
        }
        primary = snapshot_hit(
            "work-order:" + order.id,
            json.dumps(record, ensure_ascii=False),
            {"kind": "work_order", "id": order.id},
            order.is_demo,
        )
        hits["rules"].append(primary)
        hits["history"].append(primary)
        event = session.get(QualityEvent, cause.targets.get("event_id"))
        if event is None:
            missing.append("original_quality_event")
        else:
            event_hit = snapshot_hit(
                "event:" + event.id,
                json.dumps(
                    {"id": event.id, "title": event.title, "scope": event.scope_json},
                    ensure_ascii=False,
                ),
                {"kind": "quality_event", "id": event.id},
                event.is_demo,
            )
            hits["rules"].append(event_hit)
            hits["history"].append(event_hit)
        products = list(
            session.scalars(
                select(Product)
                .where(Product.id.in_(order.product_ids), Product.merchant_id == case.merchant_id)
                .order_by(Product.id)
            )
        )
        if {p.id for p in products} != set(order.product_ids):
            missing.append("work_order_targets")
        for product in products:
            hits["product"].append(
                snapshot_hit(
                    "product:" + product.id,
                    json.dumps(
                        {"id": product.id, "brand": product.brand, "model": product.model},
                        ensure_ascii=False,
                    ),
                    {"kind": "product_record", "id": product.id},
                    product.is_demo,
                )
            )
        batches = list(
            session.scalars(
                select(Batch)
                .where(
                    Batch.id.in_(order.batch_ids),
                    Batch.product_id.in_([p.id for p in products]),
                )
                .order_by(Batch.id)
            )
        )
        if {batch.id for batch in batches} != set(order.batch_ids):
            missing.append("work_order_batches")
        for batch in batches:
            hits["product"].append(
                snapshot_hit(
                    "batch:" + batch.id,
                    json.dumps(
                        {
                            "id": batch.id,
                            "product_id": batch.product_id,
                            "number": batch.number,
                            "production_date": str(batch.production_date),
                        },
                        ensure_ascii=False,
                    ),
                    {"kind": "batch_record", "id": batch.id},
                    batch.is_demo,
                )
            )
        materials = list(
            session.scalars(
                select(Material)
                .where(Material.case_id == case.id)
                .order_by(Material.created_at)
                .limit(9)
            )
        )
        if len(materials) > 8:
            missing.append("material_capacity")
        models = {p.model for p in products}
        for material in materials[:8]:
            if material.status != "parsed":
                missing.append("material_parse:" + material.id)
            for field in material.parsed.get("fields", []):
                if field["name"] == "model" and field["value"] not in models:
                    conflicts.append(
                        {
                            "field": "model",
                            "material_id": material.id,
                            "value": field["value"],
                            "location": field["location"],
                        }
                    )
            for index, unit in enumerate(material.parsed.get("units", [])[:2]):
                hits["material"].append(
                    snapshot_hit(
                        f"material:{material.id}:{index}",
                        unit["text"],
                        {
                            **unit["location"],
                            "material_id": material.id,
                            "material_sha256": material.sha256,
                        },
                    )
                )
            if len(material.parsed.get("units", [])) > 2:
                missing.append("material_units_truncated")
    if not hits["material"]:
        missing.append("new_review_evidence")
    rule_ids = []
    if as_of and order:
        for rule in applicable_rules(session, as_of, "DEMO-REMEDIATION"):
            if products and any(p.category != rule.category for p in products):
                continue
            rule_ids.append(rule.id)
            hits["rules"].append(
                snapshot_hit(
                    "rule:" + rule.id,
                    json.dumps(
                        {
                            "content": rule.content,
                            "requirements": rule.requirements,
                            "effective_from": str(rule.effective_from),
                            "effective_until": str(rule.effective_until),
                        },
                        ensure_ascii=False,
                    ),
                    {
                        "kind": "rule_record",
                        "rule_id": rule.id,
                        "version": rule.version,
                        "source_id": rule.source_id,
                    },
                    rule.is_demo,
                )
            )
            missing.extend(
                "unsupported_requirement:" + item
                for item in rule.requirements.get("documents", [])
                if item not in {"remediation_statement", "remediation_evidence"}
            )
    if not rule_ids:
        missing.append("applicable_remediation_rules")
    indexed = list(
        session.scalars(
            select(EvidenceChunk)
            .where(
                EvidenceChunk.rule_id.in_(rule_ids),
            )
            .order_by(EvidenceChunk.id)
            .limit(2001)
        )
    )
    if len(indexed) > 2000:
        missing.append("rule_index_capacity")
    manifest = hashlib.sha256(
        "\n".join(chunk.id + ":" + chunk.source_sha256 for chunk in indexed).encode()
    ).hexdigest()
    kind = case.payload.get("review_kind")
    if kind not in {"remediation", "appeal"}:
        missing.append("review_kind")
    if not isinstance(case.payload.get("statement"), str) or not case.payload["statement"].strip():
        missing.append("review_statement")
    return {
        "facts": {
            "case_id": case.id,
            "revision": case.revision,
            "merchant_id": case.merchant_id,
            "as_of": as_of.isoformat() if as_of else None,
            "work_order_id": order.id if order else None,
            "work_order_revision": order.revision if order else None,
            "review_kind": kind,
            "declared_payload": case.payload,
            "rule_index_manifest": manifest,
        },
        "source_hits": hits,
        "checks": {"missing": sorted(set(missing)), "conflicts": conflicts},
    }


def build_remediation_graph(settings, checkpointer, sessions):
    def coordinate(state):
        branches = {name: state[name + "_result"] for name in BRANCHES}
        missing = set(state["checks"]["missing"])
        # 分支风险必须保留；缺少详情也不能把非就绪结论提升为可审核。
        for name, branch in branches.items():
            missing.update(branch.get("missing_fields", []))
            if (
                branch["status"] not in {"ready", "no_history"}
                or branch.get("error_code")
                or branch.get("advice", {}).get("recommendation")
                not in {None, "review_ready"}
            ):
                missing.add("branch_review:" + name)
        sources = {hit["id"]: hit for hit in state["source_hits"]["material"]}
        cited = set(branches["material"].get("advice", {}).get("evidence_ids", []))
        words = (
            ("整改", "remediation")
            if state["facts"]["review_kind"] == "remediation"
            else ("申诉", "appeal")
        )
        valid = any(
            item["source_id"] in sources
            and item["source_id"] in cited
            and item["quote"] in sources[item["source_id"]]["text"]
            and any(word in item["quote"].lower() for word in words)
            for item in branches["material"].get("advice", {}).get("assessments", [])
        )
        if not valid:
            missing.add("review_evidence_basis")
        conflicts = state["checks"]["conflicts"]
        modes = {branch["engine_mode"] for branch in branches.values()} - {"deterministic"}
        mode = (
            "live"
            if modes == {"live"}
            else "fake"
            if modes and modes <= {"fake", "contract_fixture"}
            else "partial"
        )
        return {
            "summary": {
                "engine_mode": mode,
                "branches": branches,
                "recommendation": "review_ready" if not missing and not conflicts else "needs_info",
                "missing_fields": sorted(missing),
                "conflicts": conflicts,
                "notice": "复核建议需人工决定；关闭工单不会自动恢复商品上架",
            }
        }

    profile = WorkflowProfile("remediation", TASKS, RemediationAdvice, ("as_of", "work_order_id"))
    return build_specialist_graph(
        settings, checkpointer, sessions, profile, coordinate, remediation_snapshot
    )


def register_work_order_routes(app, sessions, actor):
    def visible(session, identifier, who, lock=False):
        query = select(WorkOrder).where(WorkOrder.id == identifier)
        if who.role == Role.MERCHANT:
            query = query.where(WorkOrder.merchant_id == who.merchant_id)
        if lock:
            query = query.with_for_update()
        row = session.scalar(query)
        if row is None:
            raise HTTPException(404, "工单不存在")
        return row

    def output(row):
        return {
            "id": row.id,
            "case_id": row.case_id,
            "merchant_id": row.merchant_id,
            "status": row.status,
            "revision": row.revision,
            "assigned_to": row.assigned_to,
            "product_ids": row.product_ids,
            "batch_ids": row.batch_ids,
            "active_review_case_id": row.active_review_case_id,
            "closure_note": row.closure_note,
            "created_at": row.created_at,
            "is_demo": row.is_demo,
        }

    @app.get("/api/work-orders")
    def list_orders(who: Actor = Depends(actor)):
        with sessions() as session:
            query = select(WorkOrder).order_by(WorkOrder.created_at.desc()).limit(50)
            if who.role == Role.MERCHANT:
                query = query.where(WorkOrder.merchant_id == who.merchant_id)
            return [output(row) for row in session.scalars(query)]

    @app.post("/api/work-orders/{identifier}/review-cases", status_code=201)
    def create_review(identifier: str, request: ReviewCreate, who: Actor = Depends(actor)):
        if who.role != Role.MERCHANT:
            raise HTTPException(403, "由所属商家提交整改或申诉资料")
        with sessions.begin() as session:
            order = visible(session, identifier, who, lock=True)
            if order.revision != request.expected_revision:
                raise HTTPException(409, "工单版本已变化，请刷新后提交")
            if order.status == "closed" or (
                order.active_review_case_id
                and session.get(ReviewCase, order.active_review_case_id).status != "rejected"
            ):
                raise HTTPException(409, "工单已关闭或已有待处理复核案件")
            order.revision += 1
            case = ReviewCase(
                title=("整改复核：" if request.review_kind == "remediation" else "申诉复核：")
                + identifier[:8],
                merchant_id=order.merchant_id,
                workflow="remediation",
                payload={
                    "work_order_id": identifier,
                    "work_order_revision": order.revision,
                    "review_kind": request.review_kind,
                    "statement": request.statement,
                    "as_of": request.as_of.isoformat(),
                },
            )
            session.add(case)
            session.flush()
            order.active_review_case_id, order.status = case.id, "in_progress"
            session.add(
                AuditEvent(
                    case_id=case.id,
                    actor_id=who.user_id,
                    action="work_order_review_created",
                    details={
                        "work_order_id": identifier,
                        "work_order_revision": order.revision,
                        "review_kind": request.review_kind,
                    },
                )
            )
            return {"case_id": case.id, "work_order": output(order)}
