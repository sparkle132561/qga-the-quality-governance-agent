import hashlib
import json
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from quality.catalog import applicable_rules
from quality.cleaning import normalize_quantity
from quality.models import EvidenceChunk, Material, Product, ReviewCase
from quality.retrieval_agent import FinalAdvice
from quality.workflow_agents import WorkflowProfile, build_specialist_graph

BRANCHES = ("rules", "product", "material", "history")
TASKS = {
    "rules": (
        "核对当前准入规则的必需资料和字段，缺资料时指出缺口，官方参考不可假称认证通过。"
        "本分支证据范围是规则与官方参考；商家提交的材料文档由material分支核对，"
        "不因证据集中看不到材料文档而判缺资料。已登记规则条目即核对依据："
        "官方条目未列明细时，按已登记的平台规则要求完成核对并给出结论；"
        "调取规则原文全文属人工复核范围，不是本分支缺口，不因此降级。"
    ),
    "product": (
        "核对SKU、商家归属和声明型号；同名型号不同SKU不能合并。"
        "证书、标签、检测报告等材料文档由material分支负责，不在本分支证据范围内；"
        "商品记录与申报核对一致即review_ready，不因材料文档缺席降级。"
    ),
    "material": (
        "核对材料型号、功率、证书主体及必需资料。material_assessments列出文档类型"
        "和含类型关键词的原文引文，source_id为工具来源ID；"
        "声明和材料不是事实认证，冲突需人工核验。"
        "本系统演示库材料带is_demo标记属设计边界，按有效证据正常核对，"
        "不因演示标记或缺少正式认证效力降级。"
    ),
    "history": (
        "只说明同商家相关历史记录及限制；历史结论不能替代当前规则和材料。"
        "本分支只陈述历史记录内容与限制，不评价当前案件的资料是否齐全；"
        "有历史记录时如实总结记录内容即可提交结论。"
    ),
}


class MaterialAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=100)
    document_type: Literal["certificate", "label", "test_report"]
    quote: str = Field(min_length=10, max_length=300)


class QualificationAdvice(FinalAdvice):
    material_assessments: list[MaterialAssessment] = Field(default_factory=list, max_length=20)


def snapshot_hit(identifier, text, location, is_demo=True):
    return {
        "id": identifier,
        "text": text,
        "location": location,
        "is_demo": is_demo,
        "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }


def qualification_snapshot(session, case: ReviewCase):
    """仅取当前案件、同商家SKU/材料/历史及日期内规则；不把用户声明变成核验事实。"""
    payload = case.payload
    missing, conflicts = [], []
    try:
        as_of = date.fromisoformat(payload.get("as_of", ""))
    except (ValueError, TypeError):
        as_of = None
        missing.append("as_of")
    sku_id = payload.get("sku_id")
    product = (
        session.scalar(
            select(Product).where(Product.id == sku_id, Product.merchant_id == case.merchant_id)
        )
        if isinstance(sku_id, str)
        else None
    )
    if product is None:
        missing.append("owned_sku_id")
    elif payload.get("model") and payload["model"] != product.model:
        conflicts.append({"field": "model", "declared": payload["model"], "catalog": product.model})
    hits = {name: [] for name in BRANCHES}
    required_documents = []
    required_fields = []
    if as_of:
        for rule in applicable_rules(session, as_of):
            if rule.rule_key not in {"DEMO-QUALIFICATION", "CNCA-C07-01"}:
                continue
            if product and rule.category != product.category:
                continue
            required_documents.extend(rule.requirements.get("documents", []))
            required_fields.extend(rule.requirements.get("fields", []))
            text = json.dumps(
                {"content": rule.content, "requirements": rule.requirements}, ensure_ascii=False
            )
            hits["rules"].append(
                snapshot_hit(
                    "rule:" + rule.id,
                    text,
                    {
                        "kind": "rule_record",
                        "rule_id": rule.id,
                        "source_id": rule.source_id,
                        "version": rule.version,
                    },
                    rule.is_demo,
                )
            )
    if not hits["rules"]:
        missing.append("applicable_rules")
    if not required_documents:
        missing.append("platform_qualification_policy")
    if product and product.category != "electric_kettle":
        missing.append("supported_category")
    rule_ids = [hit["location"]["rule_id"] for hit in hits["rules"]]
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
    if product:
        text = json.dumps(
            {
                "sku_id": product.id,
                "merchant_id": product.merchant_id,
                "brand": product.brand,
                "model": product.model,
            },
            ensure_ascii=False,
        )
        hits["product"].append(
            snapshot_hit(
                "product:" + product.id,
                text,
                {"kind": "business_record", "table": "products", "record_id": product.id},
                product.is_demo,
            )
        )
    materials = list(
        session.scalars(
            select(Material)
            .where(Material.case_id == case.id)
            .order_by(Material.created_at)
            .limit(21)
        )
    )
    if len(materials) > 20:
        missing.append("material_capacity_review")
    fields = {}
    unit_count = 0
    for material in materials[:20]:
        if material.status != "parsed":
            missing.append("material_parse:" + material.id)
        for field in material.parsed.get("fields", []):
            name, value = field["name"], str(field["value"])
            if name == "net_quantity":
                normalized = normalize_quantity(value)
                if normalized["status"] == "normalized":
                    value = normalized["value"] + " " + normalized["unit"]
            fields.setdefault(name, set()).add(value)
            if name == "model" and product and value != product.model:
                conflicts.append(
                    {
                        "field": name,
                        "material_id": material.id,
                        "value": value,
                        "catalog": product.model,
                        "location": field["location"],
                    }
                )
        for index, unit in enumerate(material.parsed.get("units", [])):
            unit_count += 1
            if len(hits["material"]) >= 8:
                continue
            hit = snapshot_hit(
                f"material:{material.id}:{index}",
                unit["text"],
                {**unit["location"], "material_id": material.id, "unit": index},
            )
            hit["material_sha256"] = material.sha256
            hits["material"].append(hit)
    if not hits["material"]:
        missing.append("source_materials")
    if unit_count > 8:
        missing.append("material_units_truncated")
    for name, values in fields.items():
        if len(values) > 1:
            conflicts.append(
                {"field": name, "values": sorted(values), "kind": "material_disagreement"}
            )
    for field in set(required_fields):
        if not fields.get(field):
            missing.append("material_field:" + field)
    history = list(
        session.scalars(
            select(ReviewCase)
            .where(
                ReviewCase.merchant_id == case.merchant_id,
                ReviewCase.id != case.id,
                ReviewCase.latest_analysis.is_not(None),
            )
            .order_by(ReviewCase.created_at.desc())
            .limit(50)
        )
    )
    for previous in history:
        if not isinstance(previous.latest_analysis, dict):
            continue  # 中断案件的JSON null不能作为已保存分析读取。
        if sku_id and previous.payload.get("sku_id") == sku_id:
            analysis = previous.latest_analysis
            # 历史只保留固定字段和来源ID，不能递归包含旧分支的完整历史/原文/Trace。
            digest = {
                "recommendation": analysis.get("recommendation"),
                "engine_mode": analysis.get("engine_mode"),
                "run_id": analysis.get("run_id"),
                "missing_fields": [
                    str(field)[:100] for field in analysis.get("missing_fields", [])[:10]
                ],
                "conflict_fields": sorted(
                    {
                        str(item.get("field", "unknown"))[:100]
                        for item in analysis.get("conflicts", [])[:10]
                    }
                ),
                "evidence_ids": [
                    str(hit["id"])[:100]
                    for output in analysis.get("branches", {}).values()
                    for hit in output.get("evidence", [])
                    if "id" in hit
                ][:20],
                "summary_kind": "fixed_fields_not_llm_summary",
            }
            text = json.dumps(
                {
                    "case_id": previous.id,
                    "revision": previous.revision,
                    "status": previous.status,
                    "analysis_digest": digest,
                },
                ensure_ascii=False,
            )
            hits["history"].append(
                snapshot_hit(
                    "history:" + previous.id,
                    text,
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
            "declared_payload": payload,
            "as_of": as_of.isoformat() if as_of else None,
            "catalog_model": product.model if product else None,
            "required_documents": sorted(set(required_documents)),
            "rule_index_snapshot": index_manifest,
        },
        "source_hits": hits,
        "checks": {
            "missing": sorted(set(missing)),
            "conflicts": conflicts,
            "notice": "材料类型与证书真实性还需模型取证及人工核验",
        },
    }


def build_qualification_graph(settings, checkpointer, sessions):
    def coordinate(state):
        branches = {name: state[f"{name}_result"] for name in BRANCHES}
        missing = set(state["checks"]["missing"])
        # 分支自由文本缺失只作审核提示；ready判定以系统可验证类别为准。
        system_missing_prefixes = (
            "document:",
            "material_field:",
            "branch_review:",
            "material_parse:",
        )
        system_missing_names = {
            "as_of",
            "owned_sku_id",
            "applicable_rules",
            "platform_qualification_policy",
            "supported_category",
            "rule_index_capacity",
            "material_capacity_review",
            "source_materials",
            "material_units_truncated",
            "unverified_material_classification",
            "history_evidence_review",
        }
        for name, output in branches.items():
            missing.update(
                field
                for field in output.get("missing_fields", [])
                if field.startswith(system_missing_prefixes)
                or field in system_missing_names
            )
            if output["status"] == "failed" or output.get("error_code"):
                missing.add("branch_review:" + name)
        conflicts = state["checks"]["conflicts"]
        material_sources = {hit["id"]: hit for hit in state["source_hits"]["material"]}
        covered = set()
        anchors = {
            "certificate": ("证书", "certificate"),
            "label": ("标签", "铭牌", "label"),
            "test_report": ("检测报告", "test report", "test_report"),
        }
        for item in branches["material"].get("advice", {}).get("material_assessments", []):
            source = material_sources.get(item["source_id"])
            quote = item["quote"]
            if (
                source
                and item["source_id"]
                in branches["material"].get("advice", {}).get("evidence_ids", [])
                and quote in source["text"]
                and any(
                    # 模型引文常截去文档类型词；分类锚点允许由来源原文佐证。
                    anchor in f"{quote} {source['text']}".lower()
                    for anchor in anchors[item["document_type"]]
                )
            ):
                covered.add(item["document_type"])
            else:
                missing.add("unverified_material_classification")
        missing.update(
            "document:" + kind
            for kind in state["facts"]["required_documents"]
            if kind not in covered
        )
        history = branches["history"]
        # 历史分支的价值是陈述历史；模型给出带证据的陈述即视为已复核，
        # 只有协议失败（failed/error_code）才阻塞，避免对当前资料的越权判断挡住结论。
        if history["status"] == "failed" or history.get("error_code"):
            missing.add("history_evidence_review")
        # 规则分支对资料齐全性的确认由聚合document:/material_field:检查权威取代；
        # 分支needs_info是模型谨慎表述，不阻塞；协议失败已计入missing。
        ready = (
            not missing
            and not conflicts
            and all(
                branches[name]["status"] == "ready" for name in ("product", "material")
            )
            and branches["rules"]["status"] != "failed"
            and not branches["rules"].get("error_code")
        )
        reject_proposed = (
            not missing
            and not conflicts
            and all(
                branches[name]["status"] == "ready"
                or (
                    branches[name].get("advice", {}).get("recommendation") == "out_of_scope"
                    and branches[name].get("advice", {}).get("evidence_ids")
                )
                for name in ("rules", "product", "material")
            )
            and any(
                branches[name].get("advice", {}).get("recommendation") == "out_of_scope"
                and branches[name].get("advice", {}).get("evidence_ids")
                for name in ("rules", "product", "material")
            )
        )
        modes = {value["engine_mode"] for value in branches.values()} - {"deterministic"}
        engine_mode = (
            "live"
            if modes == {"live"}
            else "fake"
            if modes <= {"fake", "contract_fixture"} and modes
            else "partial"
        )
        return {
            "summary": {
                "engine_mode": engine_mode,
                "branches": branches,
                "recommendation": "reject_proposed"
                if reject_proposed
                else "review_ready"
                if ready
                else "needs_info",
                "missing_fields": sorted(missing),
                "conflicts": conflicts,
                "notice": "协调结果只是审核建议；证书真实性与风险判断仍需人工核验，审批未执行",
            }
        }

    profile = WorkflowProfile(
        "qualification", TASKS, QualificationAdvice, ("as_of", "catalog_model")
    )
    return build_specialist_graph(
        settings, checkpointer, sessions, profile, coordinate, qualification_snapshot
    )
