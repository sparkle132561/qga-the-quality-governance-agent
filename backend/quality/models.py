from datetime import date, datetime, timezone
from uuid import uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    return str(uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Merchant(Base):
    __tablename__ = "merchants"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))


class VerifiedMemory(Base):
    __tablename__ = "verified_memories"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    source_case_id: Mapped[str] = mapped_column(ForeignKey("review_cases.id"))
    source_revision: Mapped[int] = mapped_column(Integer)
    analysis_run_id: Mapped[str] = mapped_column(String(36))
    approval_id: Mapped[str] = mapped_column(ForeignKey("approval_records.id"))
    source_id: Mapped[str] = mapped_column(String(100))
    source_sha256: Mapped[str] = mapped_column(String(64))
    snapshot_sha256: Mapped[str] = mapped_column(String(64))
    quote: Mapped[str] = mapped_column(String(500))
    as_of: Mapped[date] = mapped_column(Date)
    expires_on: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="candidate", index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    proposer_id: Mapped[str] = mapped_column(String(80))
    verifier_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    note: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AssistantRunControl(Base):
    __tablename__ = "assistant_run_controls"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("review_cases.id"), index=True)
    actor_id: Mapped[str] = mapped_column(String(80))
    revision: Mapped[int] = mapped_column(Integer)
    thread_id: Mapped[str] = mapped_column(String(200))
    worker_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(20), default="running")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReviewCase(Base):
    __tablename__ = "review_cases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    workflow: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    latest_analysis: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    active_run: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("review_cases.id"), index=True)
    actor_id: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(80))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Material(Base):
    __tablename__ = "materials"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("review_cases.id"), index=True)
    filename: Mapped[str] = mapped_column(String(200))
    stored_name: Mapped[str] = mapped_column(String(80))
    sha256: Mapped[str] = mapped_column(String(64))
    media_type: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30))
    case_revision: Mapped[int] = mapped_column(Integer)
    parsed: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SourceRecord(Base):
    __tablename__ = "sources"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    title: Mapped[str] = mapped_column(String(250))
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    publisher: Mapped[str] = mapped_column(String(150))
    source_kind: Mapped[str] = mapped_column(String(40))
    published_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    verified_on: Mapped[date] = mapped_column(Date)
    license_status: Mapped[str] = mapped_column(String(40))
    redistribution_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_json: Mapped[dict] = mapped_column(JSON)


class RuleVersion(Base):
    __tablename__ = "rule_versions"
    __table_args__ = (UniqueConstraint("rule_key", "version", name="uq_rule_key_version"),)
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    rule_key: Mapped[str] = mapped_column(String(100), index=True)
    version: Mapped[str] = mapped_column(String(50))
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    category: Mapped[str] = mapped_column(String(50))
    effective_from: Mapped[date] = mapped_column(Date)
    effective_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    content: Mapped[str] = mapped_column(String(5000))
    requirements: Mapped[dict] = mapped_column(JSON)
    is_demo: Mapped[bool] = mapped_column(Boolean)


class Supplier(Base):
    __tablename__ = "suppliers"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=True)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), index=True)
    supplier_id: Mapped[str] = mapped_column(ForeignKey("suppliers.id"))
    brand: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(100), index=True)
    category: Mapped[str] = mapped_column(String(50))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=True)


class Batch(Base):
    __tablename__ = "batches"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    number: Mapped[str] = mapped_column(String(100))
    production_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=True)


class DemoOrder(Base):
    __tablename__ = "demo_orders"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.id"), index=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=True)


class QualityEvent(Base):
    __tablename__ = "quality_events"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    event_type: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(250))
    scope_json: Mapped[dict] = mapped_column(JSON)
    is_demo: Mapped[bool] = mapped_column(Boolean)


class EvidenceChunk(Base):
    __tablename__ = "evidence_chunks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(100), index=True)
    rule_id: Mapped[str | None] = mapped_column(
        ForeignKey("rule_versions.id"), nullable=True, index=True
    )
    case_id: Mapped[str | None] = mapped_column(
        ForeignKey("review_cases.id"), nullable=True, index=True
    )
    material_id: Mapped[str | None] = mapped_column(ForeignKey("materials.id"), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    location: Mapped[dict] = mapped_column(JSON)
    source_sha256: Mapped[str] = mapped_column(String(64))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_demo: Mapped[bool] = mapped_column(Boolean)


class GraphExtraction(Base):
    __tablename__ = "graph_extractions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 索引可删除重建；保留抽取审计，但必须重查当前chunk和hash才能使用。
    chunk_id: Mapped[str] = mapped_column(String(36), index=True)
    source_sha256: Mapped[str] = mapped_column(String(64))
    case_id: Mapped[str | None] = mapped_column(ForeignKey("review_cases.id"), nullable=True)
    case_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    body: Mapped[dict] = mapped_column(JSON)
    location: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    engine_mode: Mapped[str] = mapped_column(String(30))
    is_demo: Mapped[bool] = mapped_column(Boolean)
    created_by: Mapped[str] = mapped_column(String(80))
    reviewed_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    review_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GraphEntityLink(Base):
    __tablename__ = "graph_entity_links"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    subject: Mapped[str] = mapped_column(String(160))
    object: Mapped[str] = mapped_column(String(160))
    case_id: Mapped[str | None] = mapped_column(ForeignKey("review_cases.id"), nullable=True)
    reviewed_by: Mapped[str] = mapped_column(String(80))
    note: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IncidentDraft(Base):
    __tablename__ = "incident_drafts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    title: Mapped[str] = mapped_column(String(250))
    raw_text: Mapped[str] = mapped_column(Text)
    source_sha256: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    proposal: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(60), nullable=True)
    engine_mode: Mapped[str] = mapped_column(String(30), default="not_called")
    active_run: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str] = mapped_column(String(80))
    confirmed_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    confirm_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    event_id: Mapped[str | None] = mapped_column(ForeignKey("quality_events.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApprovalRecord(Base):
    __tablename__ = "approval_records"
    __table_args__ = (
        UniqueConstraint("case_id", "analysis_run_id", name="uq_case_analysis_decision"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("review_cases.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    analysis_run_id: Mapped[str] = mapped_column(String(36))
    graph_thread_id: Mapped[str] = mapped_column(String(160))
    graph_kind: Mapped[str] = mapped_column(String(30))
    decision: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30), default="active")
    actor_id: Mapped[str] = mapped_column(String(80))
    note: Mapped[str] = mapped_column(String(500))
    snapshot_sha256: Mapped[str] = mapped_column(String(64))
    targets: Mapped[dict] = mapped_column(JSON)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SimulatedEffect(Base):
    __tablename__ = "simulated_effects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    approval_id: Mapped[str] = mapped_column(ForeignKey("approval_records.id"), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("review_cases.id"))
    kind: Mapped[str] = mapped_column(String(40))
    target_id: Mapped[str] = mapped_column(String(100))
    details: Mapped[dict] = mapped_column(JSON)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProductListing(Base):
    __tablename__ = "product_listings"
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(30))
    version: Mapped[int] = mapped_column(Integer, default=0)
    last_approval_id: Mapped[str] = mapped_column(ForeignKey("approval_records.id"))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=True)


class WorkOrder(Base):
    __tablename__ = "work_orders"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("review_cases.id"), index=True)
    approval_id: Mapped[str] = mapped_column(ForeignKey("approval_records.id"), unique=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="open")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    active_review_case_id: Mapped[str | None] = mapped_column(
        ForeignKey("review_cases.id"), nullable=True
    )
    closure_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    assigned_to: Mapped[str] = mapped_column(String(80))
    product_ids: Mapped[list] = mapped_column(JSON)
    batch_ids: Mapped[list] = mapped_column(JSON)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
