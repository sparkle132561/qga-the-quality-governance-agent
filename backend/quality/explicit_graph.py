"""只覆盖单记录表和明确已交付句式；歧义、多记录、未来或缺字段交回模型／人工。"""

import re

SUPPLY = re.compile(
    r"(?P<supplier>[^，,。；;\n]{2,60})已向(?P<customer>[^，,。；;\n]{2,60}?)"
    r"(?:供应|供货|配套)\s*\d+(?:\.\d+)?\s*(?:GWh|kWh|MWh|台|个|件|吨|kg|套|颗|块|只)"
    r"\s*(?P<product>[^，,。；;\n]{1,60})",
    re.IGNORECASE,
)


def explicit_graph(text):
    matches = list(SUPPLY.finditer(text))
    if len(matches) == 1:
        match = matches[0]
        supplier, product = match["supplier"].strip(), match["product"].strip()
        if not re.search(r"未|不|没有|无法|预计|拟|计划|将|传闻", supplier):
            return {
                "entities": [
                    {"key": "supplier", "kind": "supplier", "name": supplier},
                    {"key": "product", "kind": "product", "name": product},
                ],
                "relations": [
                    {
                        "subject": "supplier",
                        "predicate": "supplies",
                        "object": "product",
                        "quote": match.group(),
                    }
                ],
            }
    lines = [
        (match.group().strip(), match.start(), match.end())
        for match in re.finditer(r"[^\r\n]+", text)
        if match.group().strip()
    ]
    labels = [value.replace(" ", "") for value, _, _ in lines]
    product_rows = [i for i, label in enumerate(labels) if label == "产品名称"]
    batch_rows = [
        i
        for i, label in enumerate(labels)
        if label in {"生产批号/批次", "生产批次", "生产批号", "批次", "批号"}
    ]
    if len(product_rows) != 1 or len(batch_rows) != 1:
        return None
    pi, bi = product_rows[0], batch_rows[0]
    if not pi + 1 < bi or bi + 1 >= len(lines):
        return None
    product, batch = lines[pi + 1][0], lines[bi + 1][0]
    if batch in {"/", "／", "无", "未提供"} or len(product) > 120 or len(batch) > 120:
        return None
    quote = text[lines[pi + 1][1] : lines[bi + 1][2]].strip()
    if len(quote) > 300:
        return None
    qualifiers = {}
    for label, key in [("品牌", "brand"), ("型号/规格", "model"), ("型号", "model")]:
        indices = [i for i, value in enumerate(labels) if value == label]
        if len(indices) == 1 and indices[0] + 1 < len(lines):
            qualifiers[key] = lines[indices[0] + 1][0]
    return {
        "entities": [
            {"key": "product", "kind": "product", "name": product, "qualifiers": qualifiers},
            {"key": "batch", "kind": "batch", "name": batch},
        ],
        "relations": [
            {"subject": "product", "predicate": "has_batch", "object": "batch", "quote": quote}
        ],
    }
