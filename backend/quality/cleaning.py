"""可审计的保守清洗：原文不覆盖，不猜测OCR错字，不删除重复业务内容。"""
import re
import unicodedata
from decimal import Decimal

VERSION = "nfkc-whitespace-quantity-v2"


def normalize_quantity(value: str) -> dict:
    """处理明确公制/件数；双单位只采用明示公制值，歧义进入复核。"""
    text = unicodedata.normalize("NFKC", value).strip()
    metric = re.fullmatch(r"\d+(?:[.,]\d+)?\s*(?:oz|lbs?)\s*"
                          r"\((\d+(?:[.,]\d+)?\s*(?:kg|mg|g))\)", text, re.IGNORECASE)
    if metric:
        text = metric.group(1)
    matched = re.fullmatch(
        r"(?:(\d+)\s*[x×*]\s*)?(\d+(?:[.,]\d+)?)\s*(kg|mg|g|ml|cl|l|pieces|piece|pcs|pc)",
        text, re.IGNORECASE)
    if not matched:
        return {"raw": value, "status": "needs_review", "reason": "unsupported_quantity"}
    count, number, unit = matched.groups()
    if re.search(r"[.,]\d{3}$", number):
        return {"raw": value, "status": "needs_review", "reason": "ambiguous_separator"}
    factors = {"kg": ("g", "1000"), "mg": ("g", "0.001"), "g": ("g", "1"),
               "l": ("ml", "1000"), "cl": ("ml", "10"), "ml": ("ml", "1"),
               "pieces": ("piece", "1"), "piece": ("piece", "1"),
               "pcs": ("piece", "1"), "pc": ("piece", "1")}
    canonical_unit, factor = factors[unit.lower()]
    amount = Decimal(number.replace(",", ".")) * Decimal(factor) * Decimal(count or "1")
    if amount <= 0:
        return {"raw": value, "status": "needs_review", "reason": "non_positive_quantity"}
    if canonical_unit == "piece" and amount != amount.to_integral_value():
        return {"raw": value, "status": "needs_review", "reason": "fractional_piece"}
    return {"raw": value, "status": "normalized", "value": format(amount.normalize(), "f"),
            "unit": canonical_unit, "pack_count": int(count or "1"),
            "basis": "printed_metric_component" if metric else "explicit_unit"}


def clean_units(units: list[dict]) -> dict:
    cleaned, issues = [], []
    for index, unit in enumerate(units):
        raw = unit["text"]
        normalized = unicodedata.normalize("NFKC", raw)
        text = re.sub(r"\s+", " ", normalized).strip()
        operations = []
        if normalized != raw:
            operations.append("unicode_nfkc")
        if text != normalized:
            operations.append("whitespace")
        if "\ufffd" in raw:
            issues.append({"unit_index": index, "code": "replacement_character"})
        if unit.get("confidence") is not None and unit["confidence"] < 0.75:
            issues.append({"unit_index": index, "code": "low_ocr_score",
                           "score": unit["confidence"]})
        cleaned.append({"unit_index": index, "text": text, "operations": operations})
    return {"version": VERSION, "units": cleaned, "issues": issues,
            "changed_units": sum(bool(unit["operations"]) for unit in cleaned),
            "review_required": bool(issues) or not units,
            "policy": "保留原文/原件，不纠正疑似字符，不推断缺失字段；OCR分数不是正确概率"}
