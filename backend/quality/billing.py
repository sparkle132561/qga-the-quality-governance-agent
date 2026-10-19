"""供应商用量与公开费率估算；估算不等于实际账单。"""
from decimal import Decimal


def token_count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def estimate_charge(model, usage, pricebook):
    price = pricebook["models"].get(model)
    result = {"model": model, "actual_amount": None, "actual_bill_verified": False,
              "estimated_current": None, "list_price_min": None, "list_price_max": None,
              "unknown_fields": []}
    if price is None:
        result["unknown_fields"].append("model_price")
        return result
    prompt = token_count(usage.get("prompt_tokens"))
    output = token_count(usage.get("completion_tokens"))
    input_details, output_details = usage.get("prompt_tokens_details"), usage.get(
        "completion_tokens_details")
    cached = (token_count(input_details.get("cached_tokens"))
              if isinstance(input_details, dict) else None)
    reasoning = (token_count(output_details.get("reasoning_tokens"))
                 if isinstance(output_details, dict) else None)
    total = token_count(usage.get("total_tokens"))
    if cached is not None and (prompt is None or cached > prompt):
        cached = None
        result["unknown_fields"].append("invalid_cache_count")
    if reasoning is not None and (output is None or reasoning > output):
        reasoning = None
        result["unknown_fields"].append("invalid_reasoning_count")
    result.update(currency=price["currency"], price_source=price["source"],
                  price_checked_date=pricebook["checked_date"],
                  prompt_tokens=prompt, completion_tokens=output, cached_input_tokens=cached,
                  reasoning_tokens=reasoning, reported_total_tokens=total,
                  usage_arithmetic_consistent=(total == prompt + output)
                  if None not in (prompt, output, total) else None)
    for name, value in (("prompt_tokens", prompt), ("completion_tokens", output),
                        ("cached_input_tokens", cached)):
        if value is None:
            result["unknown_fields"].append(name)

    def bounds(rates):
        if rates is None:
            return None, None
        values = {key: Decimal(value) for key, value in rates.items()}
        if any(not value.is_finite() or value < 0 for value in values.values()):
            raise ValueError("费率必须为非负有限数")
        if all(value == 0 for value in values.values()):
            return "0", "0"
        if prompt is None or output is None:
            return None, None
        # cached属于prompt，reasoning属于completion，均不能重复加一次。
        if cached is None:
            minimum = Decimal(prompt) * min(values["cached_input"], values["input"])
            maximum = Decimal(prompt) * max(values["cached_input"], values["input"])
        else:
            minimum = maximum = (Decimal(prompt - cached) * values["input"]
                                 + Decimal(cached) * values["cached_input"])
        completion = Decimal(output) * values["output"]
        return (format((minimum + completion) / Decimal(1_000_000), "f"),
                format((maximum + completion) / Decimal(1_000_000), "f"))
    current_min, current_max = bounds(price["current"])
    result["estimated_current"] = current_min if current_min == current_max else None
    result["list_price_min"], result["list_price_max"] = bounds(price["list"])
    if price["list"] is None:
        result["unknown_fields"].append("list_price_not_published")
    result["assumption"] = "published_requested_model_price_account_eligibility_unverified"
    return result
