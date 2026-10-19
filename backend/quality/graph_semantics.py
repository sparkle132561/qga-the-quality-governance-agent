"""当前业务图的保守语义门禁；未覆盖的表达不推断为确定关系。"""

import re


class GraphSemanticError(ValueError):
    def __init__(self, reason, message):
        self.reason = reason
        super().__init__(message)


def relation_semantics(relation, subject, obj):
    types = {
        "supplies": ("supplier", "product"),
        "has_batch": ("product", "batch"),
        "ordered_from": ("batch", "order"),
    }
    if relation.predicate in types and (subject.kind, obj.kind) != types[relation.predicate]:
        raise GraphSemanticError("relation_endpoint_types", "关系端点类型不符合本项目业务图定义")
    quote = relation.quote
    if relation.predicate == "supplies":
        supplier, product = re.escape(subject.name), re.escape(obj.name)
        verb = r"(?:供应(?!商)|供货|配套|提供|suppl(?:y|ies|ied))"
        patterns = [
            rf"(?<![向为给]){supplier}[^。；;\n]{{0,100}}?{verb}[^。；;\n]{{0,40}}?{product}",
            rf"(?<![向为给]){supplier}[^。；;\n]{{0,60}}?为\s*{product}\s*供货",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, quote, re.IGNORECASE):
                claim = match.group()[len(subject.name) :]
                action = re.search(verb, claim, re.IGNORECASE)
                prefix = claim[: action.start()].replace(obj.name, "") if action else claim
                if re.search(r"将|拟|计划|约定|\bwill\b|\bintend", prefix, re.IGNORECASE):
                    continue
                if re.search(r"未|不|没有|并非|\bnot\b|\bnever\b", prefix, re.IGNORECASE):
                    continue
                return
        raise GraphSemanticError(
            "supply_not_reported_for_object",
            "原文未明确报告向该产品的实际供货；制造、用途或未来协议不能替代",
        )
    if relation.predicate == "ordered_from":
        if (
            not re.search(r"订单|\border\b", quote, re.IGNORECASE)
            or not re.search(r"\d|[A-Za-z]{2,}[-_][A-Za-z0-9_-]+", obj.name)
            or re.search(r"将|拟|计划|约定|未|没有|\bwill\b|\bnot\b", quote, re.IGNORECASE)
        ):
            raise GraphSemanticError("order_not_actual", "没有具体已发生订单依据，不能将协议当订单")
    if relation.predicate == "has_batch":
        if (
            obj.name in {"/", "／", "无", "unknown"}
            or not re.search(r"批次|批号|\bbatch\b", quote, re.IGNORECASE)
            or re.search(r"没有|不含|不包含|\bnot\b", quote, re.IGNORECASE)
        ):
            raise GraphSemanticError("batch_not_explicit", "原文没有该产品的明确批次依据")
