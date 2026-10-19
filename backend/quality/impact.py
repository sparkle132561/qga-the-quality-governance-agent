import hashlib
import json
from datetime import date

from sqlalchemy import select

from quality.catalog import applicable_rules
from quality.models import (
    Batch,
    DemoOrder,
    EvidenceChunk,
    Material,
    Product,
    QualityEvent,
    ReviewCase,
    Supplier,
)
from quality.qualification import snapshot_hit
from quality.retrieval_agent import FinalAdvice
from quality.workflow_agents import BRANCHES, WorkflowProfile, build_specialist_graph

TASKS = {
    "rules": (
        "核对召回/抽检原始范围、日期精度和资料限制；"
        "不要把抽检扩大为召回，不把当前规则替代历史依据。"
        "已登记事件记录与规则条目即核对依据；商家材料由material分支核对，"
        "调取规则原文全文属人工复核范围，不是本分支缺口，不因此降级。"
    ),
    "product": (
        "解释工具确定性匹配的hit/excluded/unknown及订单路径；"
        "不得用相似型号或模型自拟SKU改写影响集合。"
    ),
    "material": (
        "核对公告字段和已提供材料的可见信息，保留冲突；没有图片不能假称完成视觉缺陷或内部安全检测。"
        "演示库材料带is_demo标记属设计边界，按有效证据正常核对，不因演示标记降级。"
    ),
    "history": (
        "核对同商家同事件的历史摘要，旧结论不能覆盖当前范围和记录。"
        "只陈述历史记录内容与限制，不评价当前案件的资料是否齐全。"
    ),
}

EVENT_SCOPE_FIELDS = {
    "brand", "models", "production_scope", "production_start", "production_end",
    "date_precision", "batches", "serial_scope_original", "serial_filter_status",
    "scope_review_required", "unresolved_fields", "field_evidence",
    "field_normalization",
}


def product_exclusion(scope, product):
    brand, models = scope.get("brand"), scope.get("models")
    if (
        isinstance(brand, str)
        and brand
        and product.brand != brand
        and product.brand.casefold() != brand.casefold()
    ):
        return "brand_out_of_scope"
    if (
        isinstance(models, list)
        and models
        and product.model not in models
        and not any(
            isinstance(model, str) and model.casefold() == product.model.casefold()
            for model in models
        )
    ):
        return "exact_model_out_of_scope"
    return None


def match_batch(scope, product, batch):
    excluded = product_exclusion(scope, product)
    if excluded:
        return "excluded", [excluded]
    brand, models = scope.get("brand"), scope.get("models")
    uncertain = []
    if isinstance(brand, str) and brand and product.brand != brand:
        if product.brand.casefold() == brand.casefold():
            uncertain.append("brand_case_requires_review")
        else:
            return "excluded", ["brand_out_of_scope"]
    if isinstance(models, list) and models and product.model not in models:
        if any(
            isinstance(model, str) and model.casefold() == product.model.casefold()
            for model in models
        ):
            uncertain.append("model_case_requires_review")
        else:
            return "excluded", ["exact_model_out_of_scope"]
    if not isinstance(brand, str) or not brand or not isinstance(models, list) or not models:
        uncertain.append("brand_or_models_missing")
    numbers = scope.get("batches") or []
    if numbers and batch.number not in numbers:
        return "excluded", ["batch_out_of_scope"]
    kind = scope.get("production_scope") or (
        "range" if scope.get("production_start") or scope.get("production_end") else "unknown"
    )
    if kind == "range":
        start, end = scope.get("production_start"), scope.get("production_end")
        precision = scope.get("date_precision", "unknown")
        if not start or not end or precision not in {"day", "month"}:
            return "unknown", ["production_range_missing_or_unresolved"]
        if batch.production_date is None:
            return "unknown", ["batch_production_date_missing"]
        try:
            if precision == "day":
                low, high = date.fromisoformat(start), date.fromisoformat(end)
                value = batch.production_date
            else:
                # 月精度只比较年月，不能伪造公告中没有的具体日。
                low_date, high_date = (
                    date.fromisoformat(start[:7] + "-01"),
                    date.fromisoformat(end[:7] + "-01"),
                )
                low, high = (low_date.year, low_date.month), (high_date.year, high_date.month)
                value = batch.production_date.year, batch.production_date.month
            if low > high:
                return "unknown", ["invalid_production_range"]
            if not low <= value <= high:
                return "excluded", ["production_date_out_of_scope"]
        except (ValueError, TypeError):
            return "unknown", ["invalid_production_range"]
    elif kind != "all" and not numbers:
        return "unknown", ["production_scope_unknown"]
    if uncertain:
        return "unknown", uncertain
    if (
        scope.get("scope_review_required")
        or scope.get("serial_filter_status") == "requires_interpretation"
    ):
        return "unknown", scope.get("unresolved_fields") or ["scope_requires_review"]
    return "hit", ["all_known_scope_constraints_match"]


def impact_snapshot(session, case: ReviewCase):
    missing = []
    try:
        as_of = date.fromisoformat(case.payload.get("as_of", ""))
    except (TypeError, ValueError):
        as_of = None
        missing.append("as_of")
    event_id = case.payload.get("event_id")
    event = session.get(QualityEvent, event_id) if isinstance(event_id, str) else None
    if event is None:
        missing.append("event_id")
    hits = {name: [] for name in BRANCHES}
    scope = event.scope_json if event else {}
    if event:
        primary = snapshot_hit(
            "event:" + event.id,
            json.dumps({"event_type": event.event_type, "scope": scope}, ensure_ascii=False),
            {"kind": "event_record", "event_id": event.id, "source_id": event.source_id},
            event.is_demo,
        )
        hits["rules"].append(primary)
        hits["material"].append(primary)
        if event.event_type not in {"recall", "inspection"}:
            missing.append("event_type")
    if as_of:
        for rule in applicable_rules(session, as_of):
            hits["rules"].append(
                snapshot_hit(
                    "rule:" + rule.id,
                    rule.content,
                    {"kind": "rule_record", "rule_id": rule.id, "source_id": rule.source_id},
                    rule.is_demo,
                )
            )
    rule_ids = [hit["location"]["rule_id"] for hit in hits["rules"] if "rule_id" in hit["location"]]
    indexed = list(
        session.scalars(
            select(EvidenceChunk)
            .where(EvidenceChunk.rule_id.in_(rule_ids))
            .order_by(EvidenceChunk.id)
            .limit(2001)
        )
    )
    if len(indexed) > 2000:
        missing.append("rule_index_capacity")
    index_manifest = hashlib.sha256(
        "\n".join(chunk.id + ":" + chunk.source_sha256 for chunk in indexed).encode()
    ).hexdigest()
    query = select(Product).where(Product.merchant_id == case.merchant_id).order_by(Product.id)
    sku_id = case.payload.get("sku_id")
    if sku_id is not None:
        if isinstance(sku_id, str) and sku_id:
            query = query.where(Product.id == sku_id)
        else:
            query = query.where(False)
            missing.append("owned_sku_id")
    products = list(session.scalars(query.limit(21)))
    if len(products) > 20:
        missing.append("catalog_scope_capacity")
    products = products[:20]
    if case.payload.get("sku_id") and not products:
        missing.append("owned_sku_id")
    batches = list(
        session.scalars(
            select(Batch)
            .where(Batch.product_id.in_([p.id for p in products]))
            .order_by(Batch.id)
            .limit(41)
        )
    )
    if len(batches) > 40:
        missing.append("batch_scope_capacity")
    batches = batches[:40]
    orders = list(
        session.scalars(
            select(DemoOrder)
            .where(
                DemoOrder.merchant_id == case.merchant_id,
                DemoOrder.batch_id.in_([b.id for b in batches]),
            )
            .order_by(DemoOrder.id)
            .limit(101)
        )
    )
    if len(orders) > 100:
        missing.append("order_scope_capacity")
    orders = orders[:100]
    by_product = {p.id: p for p in products}
    findings = []
    suppliers = set(
        session.scalars(
            select(Supplier.id).where(
                Supplier.merchant_id == case.merchant_id,
                Supplier.id.in_([p.supplier_id for p in products]),
            )
        )
    )
    for batch in batches:
        product = by_product[batch.product_id]
        status, reasons = (
            match_batch(scope, product, batch) if event else ("unknown", ["event_id_missing"])
        )
        if product.supplier_id not in suppliers:
            missing.append("supplier_owner_inconsistent")
            status, reasons = "unknown", ["supplier_owner_inconsistent"]
        row = {
            "product_id": product.id,
            "batch_id": batch.id,
            "batch_number": batch.number,
            "model": product.model,
            "brand": product.brand,
            "production_date": batch.production_date.isoformat() if batch.production_date else None,
            "status": status,
            "reasons": reasons,
            "order_ids": [order.id for order in orders if order.batch_id == batch.id],
            "is_demo": product.is_demo and batch.is_demo,
        }
        # 路径仅由本次已选的关系记录构成，不能重新扩大到整商家图。
        edges = []
        if product.supplier_id in suppliers:
            edges.append(
                (
                    "supplier:" + product.supplier_id,
                    "supplies",
                    "product:" + product.id,
                    "products",
                    product.id,
                )
            )
        edges.append(
            ("product:" + product.id, "has_batch", "batch:" + batch.id, "batches", batch.id)
        )
        edges.extend(
            ("batch:" + batch.id, "ordered_from", "order:" + identifier, "demo_orders", identifier)
            for identifier in row["order_ids"]
        )
        row["graph_edges"] = [
            {
                "subject": left,
                "predicate": predicate,
                "object": right,
                "provenance": {"table": table, "record_id": identifier},
                "origin": "business_record",
                "is_demo": row["is_demo"],
            }
            for left, predicate, right, table, identifier in edges
        ]
        findings.append(row)
        hits["product"].append(
            snapshot_hit(
                "batch:" + batch.id,
                json.dumps(row, ensure_ascii=False),
                {"kind": "batch_match", "product_id": product.id, "batch_id": batch.id},
                row["is_demo"],
            )
        )
    for product in products:
        if not any(batch.product_id == product.id for batch in batches):
            excluded = product_exclusion(scope, product) if event else None
            findings.append(
                {
                    "product_id": product.id,
                    "batch_id": None,
                    "status": "excluded" if excluded else "unknown",
                    "reasons": [excluded] if excluded else ["batch_records_missing"],
                    "brand": product.brand,
                    "model": product.model,
                    "order_ids": [],
                    "is_demo": product.is_demo,
                }
            )
            hits["product"].append(
                snapshot_hit(
                    "product:" + product.id,
                    json.dumps(findings[-1], ensure_ascii=False),
                    {"kind": "missing_batch_record", "product_id": product.id},
                    product.is_demo,
                )
            )
    if not products:
        hits["product"].append(
            snapshot_hit(
                "catalog-empty:" + case.merchant_id,
                "当前明确查询范围内没有目录商品",
                {"kind": "catalog_scope", "merchant_id": case.merchant_id},
            )
        )
    if len(hits["product"]) > 20:
        missing.append("product_evidence_capacity")
    materials = list(
        session.scalars(
            select(Material)
            .where(Material.case_id == case.id)
            .order_by(Material.created_at)
            .limit(9)
        )
    )
    if len(materials) > 8:
        missing.append("material_scope_capacity")
    for material in materials[:8]:
        if material.status != "parsed":
            missing.append("material_parse:" + material.id)
        for index, unit in enumerate(material.parsed.get("units", [])[:2]):
            hits["material"].append(
                snapshot_hit(
                    f"material:{material.id}:{index}",
                    unit["text"],
                    {
                        **unit["location"],
                        "material_id": material.id,
                        "unit": index,
                        "material_sha256": material.sha256,
                    },
                )
            )
        if len(material.parsed.get("units", [])) > 2:
            missing.append("material_units_truncated")
    history = session.scalars(
        select(ReviewCase)
        .where(
            ReviewCase.merchant_id == case.merchant_id,
            ReviewCase.id != case.id,
            ReviewCase.latest_analysis.is_not(None),
        )
        .order_by(ReviewCase.created_at.desc())
        .limit(50)
    )
    for previous in history:
        if not isinstance(previous.latest_analysis, dict):
            continue  # JSON null不等于SQL NULL；中断尚未写回的记录没有分析摘要。
        if event and previous.payload.get("event_id") == event.id:
            old = previous.latest_analysis
            digest = {
                "case_id": previous.id,
                "revision": previous.revision,
                "status": previous.status,
                "recommendation": old.get("recommendation"),
                "engine_mode": old.get("engine_mode"),
                "run_id": old.get("run_id"),
            }
            hits["history"].append(
                snapshot_hit(
                    "history:" + previous.id,
                    json.dumps(digest, ensure_ascii=False),
                    {"kind": "case_record", "case_id": previous.id},
                    previous.is_demo,
                )
            )
            if len(hits["history"]) == 3:
                break
    return {
        "facts": {
            "case_id": case.id,
            "revision": case.revision,
            "merchant_id": case.merchant_id,
            "as_of": as_of.isoformat() if as_of else None,
            "event_id": event.id if event else None,
            "event_type": event.event_type if event else "unknown",
            "event_scope": {key: value for key, value in scope.items()
                            if key in EVENT_SCOPE_FIELDS},
            "rule_index_snapshot": index_manifest,
            "declared_payload": case.payload,
        },
        "source_hits": hits,
        "checks": {
            "missing": sorted(set(missing)),
            "findings": findings,
            "catalog_count": len(products),
        },
    }


def build_impact_graph(settings, checkpointer, sessions):
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
        findings = state["checks"]["findings"]
        if any(row["status"] == "unknown" for row in findings):
            missing.add("candidate_scope_unknown")
        cited = set(branches["product"].get("advice", {}).get("evidence_ids", []))
        required = {hit["id"] for hit in state["source_hits"]["product"]}
        if not required.issubset(cited):
            missing.add("product_evidence_coverage")
        statuses = {}
        for row in findings:
            statuses.setdefault(row["product_id"], set()).add(row["status"])
        impact = {
            "findings": findings,
            "affected_product_ids": sorted(
                key for key, values in statuses.items() if "hit" in values
            ),
            "excluded_product_ids": sorted(
                key for key, values in statuses.items() if values == {"excluded"}
            ),
            "unknown_product_ids": sorted(
                key for key, values in statuses.items() if "unknown" in values
            ),
            "affected_batch_ids": sorted(
                {row["batch_id"] for row in findings if row["status"] == "hit"}
            ),
            "affected_order_ids": sorted(
                {key for row in findings if row["status"] == "hit" for key in row["order_ids"]}
            ),
        }
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
                "recommendation": "needs_info" if missing else "review_ready",
                "missing_fields": sorted(missing),
                "conflicts": [],
                "impact": impact,
                "notice": "hit只表示在公告范围内；抽检不等于召回，模拟处置尚未执行",
            }
        }

    profile = WorkflowProfile("impact", TASKS, FinalAdvice, ("as_of", "event_id"))
    return build_specialist_graph(
        settings, checkpointer, sessions, profile, coordinate, impact_snapshot
    )
