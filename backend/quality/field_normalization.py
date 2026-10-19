"""只规范有原文依据的品牌修饰词和明确货号，不推断别名或相似型号。

另吸收模型输出字面量的两类格式方差：null被写成字符串、ISO日期被写成中文日期；
只做等价值转写，不改变证据校验与原文一致性要求。
"""

import re

ARTICLE_MODEL = re.compile(
    r"(?P<model>[A-Za-z0-9._/+\-]+)[（(]\s*货号\s*[:：]?\s*"
    r"(?P<article>[A-Za-z0-9._/+\-]+)\s*[）)]"
)

NULL_LITERALS = {"none", "null"}
CHINESE_DATE = re.compile(r"^(\d{4})\s*年\s*(\d{1,2})\s*月(?:\s*(\d{1,2})\s*日)?$")


def normalize_output_literals(value):
    """转写模型字面量方差；语义证据校验仍在后置校验中强制执行。"""
    for key in (
        "brand",
        "serial_scope_original",
        "defect_summary",
        "production_start",
        "production_end",
    ):
        item = value.get(key)
        if isinstance(item, str) and item.strip().lower() in NULL_LITERALS:
            value[key] = None
    for key in ("production_start", "production_end"):
        item = value.get(key)
        if isinstance(item, str) and (match := CHINESE_DATE.match(item.strip())):
            year, month, day = match.groups()
            value[key] = (
                f"{int(year):04d}-{int(month):02d}"
                + (f"-{int(day):02d}" if day else "")
            )
    return value


def normalize_fields(result, evidence, raw_text):
    audit = {}
    brand = result["brand"]
    if brand:
        quote = evidence["brand"]["quote"]
        canonical = brand[:-1] if brand.endswith("牌") and not brand.endswith("品牌") else brand
        source_form = canonical + "牌"
        explicit_label = re.search(r"品牌\s*[:：]?\s*" + re.escape(brand) + r"(?:\s|$)", raw_text)
        # 只处理“倍思牌BS-...型号”这类修饰形式；明确品牌栏里的末尾“牌”保留。
        if (
            canonical
            and not explicit_label
            and any(source_form + model in quote for model in result["models"])
        ):
            result["brand"] = canonical
            audit["brand"] = {
                "original": source_form,
                "normalized": canonical,
                "rule": "brand_designator_before_model",
            }
    details, models = [], []
    for value in result["models"]:
        quote = evidence["models"]["quote"]
        match = next(
            (m for m in ARTICLE_MODEL.finditer(quote) if value in (m.group(), m["model"])), None
        )
        model = match["model"] if match else value
        if model not in models:
            models.append(model)
        if match:
            details.append(
                {
                    "original": match.group(),
                    "normalized": model,
                    "article_number": match["article"],
                    "rule": "explicit_article_number",
                }
            )
    result["models"] = models
    if details:
        audit["models"] = details
    result["field_normalization"] = audit
    return result
