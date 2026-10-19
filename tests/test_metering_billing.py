import json

import httpx
from quality import metering
from quality.billing import estimate_charge
from quality.billing_ledger import build_ledger
from quality.config import ROOT
from quality.providers import SiliconFlowClient


def prices():
    return json.loads((ROOT / "tests/fixtures/pricing-snapshot.json").read_text(encoding="utf-8"))


def test_cache_and_reasoning_tokens_are_subsets_not_extra_charges():
    usage = {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100,
             "prompt_tokens_details": {"cached_tokens": 200},
             "completion_tokens_details": {"reasoning_tokens": 80}}
    result = estimate_charge("agnes-2.5-flash", usage, prices())
    assert result["list_price_min"] == result["list_price_max"] == "0.000056"
    assert result["estimated_current"] == "0" and result["actual_amount"] is None
    assert result["usage_arithmetic_consistent"] is True


def test_missing_cache_gives_a_range_missing_usage_is_not_zero_tokens():
    result = estimate_charge("agnes-2.5-flash",
                             {"prompt_tokens": 1000, "completion_tokens": 100}, prices())
    assert result["list_price_min"] == "0.00002" and result["list_price_max"] == "0.000065"
    result = estimate_charge("BAAI/bge-reranker-v2-m3", {}, prices())
    assert result["prompt_tokens"] is None and result["actual_amount"] is None
    assert result["list_price_min"] is None


def test_rerank_meta_and_each_retry_are_metered_without_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr(metering, "ROOT", tmp_path)
    target = tmp_path / "artifacts/benchmarks/events.jsonl"
    monkeypatch.setenv("QUALITY_METERING_FILE", str(target))
    calls = []
    def respond(request):
        calls.append(request)
        headers = {"x-siliconcloud-trace-id": request.headers["X-Trace-Id"]}
        if len(calls) == 1:
            headers["Retry-After"] = "0.01"
            return httpx.Response(429, headers=headers, json={"id": "first-attempt"})
        return httpx.Response(200, headers=headers, json={"id": "rerank-second",
            "results": [{"index": 0, "relevance_score": 0.9}],
            "meta": {"tokens": {"input_tokens": 120, "output_tokens": 0, "image_tokens": 0},
                     "billed_units": {"input_tokens": 120, "search_units": 1}}})
    result = SiliconFlowClient("never-log-this-credential", transport=httpx.MockTransport(respond)
                               ).rerank("certificate", ["sensitive-document"], 1)
    assert result["usage"]["total_tokens"] == 120
    meta = result["request_meta"]
    assert meta["attempts"] == 2 and meta["backoff_ms"] >= 0
    assert [row["status_code"] for row in meta["attempt_records"]] == [429, 200]
    assert meta["provider_meta"]["billed_units"]["search_units"] == 1
    lines = target.read_text(encoding="utf-8")
    assert "never-log-this-credential" not in lines and "sensitive-document" not in lines
    events = [json.loads(line) for line in lines.splitlines()]
    assert events[0]["event"] == "attempt_started" and events[-1]["event"] == "usage_recorded"
    assert all(row["metadata"]["engine_mode"] == "contract_fixture" for row in events)
    ledger = build_ledger(events + events, prices())
    assert ledger["logical_calls"] == 1 and ledger["physical_attempts"] == 2
    assert ledger["known_total_tokens"] == 120 and ledger["missing_usage_attempts"] == 1
    assert ledger["by_currency"]["CNY"]["estimated_current_total"] is None
    assert ledger["unfinished_calls"] == ledger["unfinished_attempts"] == 0


def test_inflight_requests_and_currencies_are_not_hidden_by_zero_price():
    events = []
    for model, identifier, usage in (
        ("agnes-2.5-flash", "llm", {"prompt_tokens": 1000, "completion_tokens": 100,
                                   "total_tokens": 1100}),
        ("BAAI/bge-m3", "embed", {"prompt_tokens": 40, "completion_tokens": 0,
                                 "total_tokens": 40}),
    ):
        events.append({"event": "call_finished", "metadata": {
            "local_call_id": identifier, "model": model, "engine_mode": "live",
            "attempt_records": [{"local_attempt_id": identifier, "status_code": 200,
                                 "finished_at": "2026-10-08", "usage": usage}]}})
    events.append({"event": "attempt_started", "metadata": {
        "local_call_id": "unfinished", "model": "agnes-2.5-flash", "engine_mode": "live",
        "attempt_records": [{"local_attempt_id": "started-only"}]}})
    ledger = build_ledger(events, prices())
    assert ledger["known_total_tokens"] == 1140
    assert ledger["unfinished_calls"] == ledger["unfinished_attempts"] == 1
    assert ledger["by_currency"]["USD"]["estimated_current_total"] is None
    assert ledger["by_currency"]["CNY"]["estimated_current_total"] == "0"
    assert ledger["by_currency"]["USD"]["known_list_min"] == "0.00002"
    assert ledger["by_currency"]["USD"]["known_list_max"] == "0.000065"


def test_malformed_optional_usage_does_not_crash_or_double_charge():
    result = estimate_charge("agnes-2.5-flash", {
        "prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1000,
        "prompt_tokens_details": "unavailable",
        "completion_tokens_details": {"reasoning_tokens": 101}}, prices())
    assert result["cached_input_tokens"] is None and result["reasoning_tokens"] is None
    assert result["usage_arithmetic_consistent"] is False
    assert "invalid_reasoning_count" in result["unknown_fields"]
    assert result["list_price_min"] == "0.00002" and result["list_price_max"] == "0.000065"
