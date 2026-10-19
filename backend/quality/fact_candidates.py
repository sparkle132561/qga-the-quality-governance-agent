"""版本2候选事实契约；保存来源声明，不生成已确认业务关系。"""

import hashlib
import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DateRangeText(StrictRecord):
    start_text: str = Field(min_length=1, max_length=30)
    end_text: str = Field(min_length=1, max_length=30)


class ProductAttributes(StrictRecord):
    name: str | None = Field(default=None, max_length=120)
    brand: str | None = Field(default=None, max_length=100)
    models: list[str] = Field(default_factory=list, max_length=20)
    batches: list[str] = Field(default_factory=list, max_length=30)
    production_ranges: list[DateRangeText] = Field(default_factory=list, max_length=20)
    serial_condition: str | None = Field(default=None, max_length=300)
    conclusion: str | None = Field(default=None, max_length=300)


class SupplyAttributes(StrictRecord):
    supplier: str = Field(min_length=1, max_length=120)
    product: str = Field(min_length=1, max_length=120)
    recipient: str | None = Field(default=None, max_length=120)
    status: Literal["reported_delivery", "commitment", "negated", "unknown"]
    valid_time: str | None = Field(default=None, max_length=120)
    condition: str | None = Field(default=None, max_length=300)


class ProductItem(StrictRecord):
    product_record: str = Field(min_length=1, max_length=1800)
    product_record_attributes: ProductAttributes


class SupplyItem(StrictRecord):
    supply_claim: str = Field(min_length=1, max_length=1800)
    supply_claim_attributes: SupplyAttributes


class FactItems(StrictRecord):
    extractions: list[ProductItem | SupplyItem] = Field(max_length=40)


def exact_span(text, quote, interval=None):
    if interval is None:
        if text.count(quote) != 1:
            raise ValueError("记录证据不存在或位置不唯一，需要明确对齐位置")
        start = text.index(quote)
        end = start + len(quote)
    else:
        start, end = interval
    if (
        type(start) is not int
        or type(end) is not int
        or not 0 <= start < end <= len(text)
        or text[start:end] != quote
    ):
        raise ValueError("记录证据与字符位置不一致")
    return {"quote": quote, "char_start": start, "char_end": end}


def source_value(value, evidence):
    if value is None:
        return None
    if not value.strip() or value not in evidence["quote"]:
        raise ValueError("字段不在所属记录证据中")
    offsets = [m.start() for m in re.finditer(re.escape(value), evidence["quote"])]
    # 重复出现保留全部位置，不替模型猜测哪个 occurrence 表示业务角色。
    return {
        "text": value,
        "spans": [
            {
                "char_start": evidence["char_start"] + offset,
                "char_end": evidence["char_start"] + offset + len(value),
            }
            for offset in offsets
        ],
    }


def date_value(value, evidence):
    source = source_value(value, evidence)
    complete_dates = {
        (evidence["char_start"] + match.start(), evidence["char_start"] + match.end())
        for match in re.finditer(
            r"(?<!\d)(?:\d{4}年\s*\d{1,2}月(?:\s*\d{1,2}日)?"
            r"|\d{4}-\d{2}(?:-\d{2})?)(?![\d-])",
            evidence["quote"],
        )
    }
    source["spans"] = [
        span for span in source["spans"] if (span["char_start"], span["char_end"]) in complete_dates
    ]
    if not source["spans"]:
        raise ValueError("必须引用完整日期，不能将日期前缀降为月份")
    match = re.fullmatch(r"(\d{4})年\s*(\d{1,2})月(?:\s*(\d{1,2})日)?", value)
    if match:
        year, month, day = match.groups()
        normalized = f"{int(year):04}-{int(month):02}"
        if day:
            normalized += f"-{int(day):02}"
    elif re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", value):
        normalized = value
    else:
        raise ValueError("日期格式或精度需要人工解释")
    date.fromisoformat(normalized if len(normalized) == 10 else normalized + "-01")
    return {
        **source,
        "normalized": normalized,
        "precision": "day" if len(normalized) == 10 else "month",
        "normalization_rule": "date_literal_v2",
    }


def resolve_facts(items: FactItems, text: str, *, intervals=None):
    """两条抽取路线共用；只检结构、精确出处和日期，不声称语义已核验。"""
    if intervals is not None and len(intervals) != len(items.extractions):
        raise ValueError("对齐位置数量与抽取数量不一致")
    products, claims, seen = [], [], set()
    for index, item in enumerate(items.extractions):
        is_product = isinstance(item, ProductItem)
        quote = item.product_record if is_product else item.supply_claim
        attrs = item.product_record_attributes if is_product else item.supply_claim_attributes
        evidence = exact_span(text, quote, None if intervals is None else intervals[index])
        fingerprint = (type(item).__name__, evidence["char_start"], attrs.model_dump_json())
        if fingerprint in seen:
            raise ValueError("重复候选记录")
        seen.add(fingerprint)
        record = {"evidence": evidence}
        if is_product:
            if not any((attrs.name, attrs.brand, attrs.models)):
                raise ValueError("产品记录缺少可辨识的原文字段")
            for field in ("name", "brand", "serial_condition", "conclusion"):
                record[field] = source_value(getattr(attrs, field), evidence)
            for field in ("models", "batches"):
                values = getattr(attrs, field)
                if len(set(values)) != len(values):
                    raise ValueError("重复型号或批次")
                record[field] = [source_value(value, evidence) for value in values]
            ranges = []
            for period in attrs.production_ranges:
                start, end = (
                    date_value(value, evidence) for value in (period.start_text, period.end_text)
                )
                if (
                    start["precision"] != end["precision"]
                    or start["normalized"] > end["normalized"]
                ):
                    raise ValueError("生产日期范围倒置或精度不一致")
                ranges.append({"start": start, "end": end})
            if len({(r["start"]["normalized"], r["end"]["normalized"]) for r in ranges}) != len(
                ranges
            ):
                raise ValueError("重复生产日期范围")
            record["production_ranges"] = ranges
            products.append(record)
        else:
            for field in ("supplier", "product", "recipient", "valid_time", "condition"):
                record[field] = source_value(getattr(attrs, field), evidence)
            record["status"] = attrs.status
            claims.append(record)
    return {
        "schema_version": "fact_candidates_v2",
        "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "products": products,
        "supply_claims": claims,
        "status": "pending_quality_review" if products or claims else "needs_info",
        "structural_and_source_checks_passed": True,
        "semantic_verified": False,
        "human_verified_count": 0,
    }
