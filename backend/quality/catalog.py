import json
import threading
from datetime import date

from fastapi import Depends, HTTPException, Query
from sqlalchemy import or_, select

from quality.config import ROOT
from quality.domain import Actor, Role
from quality.models import (
    Batch,
    DemoOrder,
    Product,
    QualityEvent,
    RuleVersion,
    SourceRecord,
    Supplier,
)

_demo_import_lock = threading.Lock()


def import_demo(session):
    catalog = json.loads((ROOT / "data/demo/catalog.json").read_text(encoding="utf-8"))
    scenarios = json.loads((ROOT / "data/demo/scenarios.json").read_text(encoding="utf-8"))
    counts = {}
    for table, entries in [
        (SourceRecord, catalog["sources"]),
        (RuleVersion, catalog["rules"]),
        (Supplier, catalog["suppliers"]),
        (QualityEvent, catalog["events"]),
    ]:
        added = 0
        for raw in entries:
            if session.get(table, raw["id"]) is not None:
                continue
            values = dict(raw)
            for key in ("published_on", "verified_on", "effective_from", "effective_until"):
                if values.get(key):
                    values[key] = date.fromisoformat(values[key])
            session.add(table(**values))
            added += 1
        session.flush()
        counts[table.__tablename__] = added
    counts.update(products=0, batches=0, demo_orders=0)
    for raw in scenarios["products"]:
        if session.get(Product, raw["id"]) is None:
            product_values = {
                key: value for key, value in raw.items() if key not in ("batch", "production_date")
            }
            session.add(
                Product(
                    **{
                        **product_values,
                        "supplier_id": "supplier-a"
                        if raw["merchant_id"] == "merchant-a"
                        else "supplier-b",
                        "category": "electric_kettle",
                    }
                )
            )
            counts["products"] += 1
    session.flush()
    for raw in scenarios["products"]:
        batch_id = "batch-" + raw["id"]
        if session.get(Batch, batch_id) is None:
            session.add(
                Batch(
                    id=batch_id,
                    product_id=raw["id"],
                    number=raw["batch"],
                    production_date=date.fromisoformat(raw["production_date"]),
                )
            )
            counts["batches"] += 1
    session.flush()
    for raw in catalog["orders"]:
        if session.get(DemoOrder, raw["id"]) is None:
            values = {key: value for key, value in raw.items() if key != "product_id"}
            session.add(DemoOrder(**values, batch_id="batch-" + raw["product_id"]))
            counts["demo_orders"] += 1
    session.flush()
    return counts


def applicable_rules(session, as_of: date, rule_key: str | None = None):
    query = (
        select(RuleVersion)
        .where(
            RuleVersion.effective_from <= as_of,
            or_(RuleVersion.effective_until.is_(None), RuleVersion.effective_until > as_of),
        )
        .order_by(RuleVersion.rule_key, RuleVersion.effective_from.desc())
    )
    if rule_key:
        query = query.where(RuleVersion.rule_key == rule_key)
    return list(session.scalars(query))


def register_catalog_routes(app, sessions, actor):
    @app.post("/api/admin/import-demo")
    def import_catalog(who: Actor = Depends(actor)):
        if who.role != Role.ADMIN:
            raise HTTPException(403, "只有管理员可以导入演示目录")
        # 本地单进程部署：锁覆盖事务提交，不能只锁查重或插入步骤。
        with _demo_import_lock:
            with sessions.begin() as session:
                return {
                    "added": import_demo(session),
                    "is_demo_catalog": True,
                    "notice": "官方来源与自建模拟资料已分开标注",
                }

    @app.get("/api/sources")
    def sources(who: Actor = Depends(actor)):
        with sessions() as session:
            return [
                {
                    "id": item.id,
                    "title": item.title,
                    "url": item.url,
                    "publisher": item.publisher,
                    "source_kind": item.source_kind,
                    "published_on": item.published_on,
                    "verified_on": item.verified_on,
                    "license_status": item.license_status,
                    "redistribution_allowed": item.redistribution_allowed,
                    "metadata": item.metadata_json,
                }
                for item in session.scalars(select(SourceRecord))
            ]

    @app.get("/api/rules")
    def rules(
        as_of: date = Query(...),
        rule_key: str | None = Query(None, max_length=100),
        who: Actor = Depends(actor),
    ):
        with sessions() as session:
            return [
                {
                    "id": item.id,
                    "rule_key": item.rule_key,
                    "version": item.version,
                    "source_id": item.source_id,
                    "category": item.category,
                    "effective_from": item.effective_from,
                    "effective_until": item.effective_until,
                    "content": item.content,
                    "requirements": item.requirements,
                    "is_demo": item.is_demo,
                }
                for item in applicable_rules(session, as_of, rule_key)
            ]

    @app.get("/api/products")
    def products(
        who: Actor = Depends(actor),
        limit: int = Query(100, ge=1, le=200),
        merchant_id: str | None = Query(None, max_length=80),
    ):
        with sessions() as session:
            query = select(Product).order_by(Product.id).limit(limit)
            if who.role == Role.MERCHANT:
                if merchant_id not in (None, who.merchant_id):
                    raise HTTPException(403, "不能查询其他商家的商品")
                query = query.where(Product.merchant_id == who.merchant_id)
            elif merchant_id:
                query = query.where(Product.merchant_id == merchant_id)
            found = list(session.scalars(query))
            batches = (
                list(
                    session.scalars(
                        select(Batch).where(Batch.product_id.in_([product.id for product in found]))
                    )
                )
                if found
                else []
            )
            return [
                {
                    "id": item.id,
                    "merchant_id": item.merchant_id,
                    "brand": item.brand,
                    "model": item.model,
                    "is_demo": item.is_demo,
                    "batches": [
                        {
                            "id": batch.id,
                            "number": batch.number,
                            "production_date": batch.production_date,
                            "is_demo": batch.is_demo,
                        }
                        for batch in batches
                        if batch.product_id == item.id
                    ],
                }
                for item in found
            ]

    @app.get("/api/events")
    def events(who: Actor = Depends(actor)):
        with sessions() as session:
            return [
                {
                    "id": item.id,
                    "title": item.title,
                    "event_type": item.event_type,
                    "source_id": item.source_id,
                    "scope": item.scope_json,
                    "is_demo": item.is_demo,
                }
                for item in session.scalars(select(QualityEvent))
            ]
