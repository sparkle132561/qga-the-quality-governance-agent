"""将追加日志按物理请求去重；失败、未完成和缺失用量不视为免费账单。"""
from collections import Counter
from decimal import Decimal

from quality.billing import estimate_charge, token_count


def build_ledger(events, pricebook):
    calls, attempts = {}, {}
    for event in events:
        meta = event["metadata"]
        call_id = meta["local_call_id"]
        call = calls.setdefault(call_id, {})
        call.update({key: value for key, value in meta.items() if key != "attempt_records"})
        if event["event"] == "call_finished":
            call["call_finished"] = True
        for record in meta.get("attempt_records", []):
            identifier = record["local_attempt_id"]
            row = attempts.setdefault(identifier, {"local_call_id": call_id})
            if row["local_call_id"] != call_id:
                raise ValueError("物理请求ID对应多个逻辑调用")
            row.update(record)
    rows = []
    for row in attempts.values():
        call = calls[row["local_call_id"]]
        row.update({key: call.get(key) for key in ("model", "provider", "endpoint", "engine_mode")})
        usage = row.get("usage", {})
        row["usage_complete"] = all(token_count(usage.get(key)) is not None
                                    for key in ("prompt_tokens", "completion_tokens"))
        row["attempt_finished"] = bool(row.get("finished_at"))
        row["http_success"] = (isinstance(row.get("status_code"), int)
                               and 200 <= row["status_code"] < 300)
        charge = estimate_charge(row["model"], usage, pricebook)
        if not row["usage_complete"] or not row["attempt_finished"]:
            # 供应商可能对失败/超时请求收费；没有usage时连免费估算也不充当账单。
            charge.update(estimated_current=None, list_price_min=None, list_price_max=None)
        row["charge"] = charge
        rows.append(row)
    by_currency = {}
    for row in rows:
        charge = row["charge"]
        currency = charge.get("currency", "UNKNOWN")
        totals = by_currency.setdefault(currency, {
            "attempts": 0, "current_unknown_attempts": 0, "list_unknown_attempts": 0,
            "known_current_subtotal": Decimal(0), "known_list_min": Decimal(0),
            "known_list_max": Decimal(0), "actual_amount": None})
        totals["attempts"] += 1
        if charge["estimated_current"] is None:
            totals["current_unknown_attempts"] += 1
        else:
            totals["known_current_subtotal"] += Decimal(charge["estimated_current"])
        if charge["list_price_min"] is None:
            totals["list_unknown_attempts"] += 1
        else:
            totals["known_list_min"] += Decimal(charge["list_price_min"])
            totals["known_list_max"] += Decimal(charge["list_price_max"])
    for totals in by_currency.values():
        for key, value in list(totals.items()):
            if isinstance(value, Decimal):
                totals[key] = format(value, "f")
        totals["estimated_current_total"] = (totals["known_current_subtotal"]
                                              if not totals["current_unknown_attempts"] else None)
        totals["list_total_min"] = (totals["known_list_min"]
                                    if not totals["list_unknown_attempts"] else None)
        totals["list_total_max"] = (totals["known_list_max"]
                                    if not totals["list_unknown_attempts"] else None)
    known_tokens = sum(token_count(row.get("usage", {}).get("total_tokens")) or 0 for row in rows)
    return {"schema_version": 1, "logical_calls": len(calls), "physical_attempts": len(rows),
            "known_total_tokens": known_tokens,
            "missing_total_token_attempts": sum(
                token_count(row.get("usage", {}).get("total_tokens")) is None for row in rows),
            "missing_usage_attempts": sum(not row["usage_complete"] for row in rows),
            "unfinished_attempts": sum(not row["attempt_finished"] for row in rows),
            "unfinished_calls": sum(not call.get("call_finished") for call in calls.values()),
            "http_status_counts": dict(Counter(str(row.get("status_code")) for row in rows)),
            "engine_mode_counts": dict(Counter(row["engine_mode"] for row in rows)),
            "by_currency": by_currency, "actual_bill_verified": False,
            "calls": calls, "attempts": rows}
