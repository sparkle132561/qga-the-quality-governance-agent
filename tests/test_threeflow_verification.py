import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "threeflows", Path(__file__).parents[1] / "scripts/verify-live-threeflows.py"
)
verification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verification)


def analysis():
    return {
        "recommendation": "needs_info",
        "missing_fields": ["document:test_report"],
        "branches": {
            name: {"status": "needs_info", "engine_mode": "live", "error_code": None}
            for name in ("rules", "product", "material", "history")
        },
    }


def test_expected_missing_document_is_not_protocol_failure():
    result = analysis()
    assert verification.protocol_failures(result) == []
    assert not verification.protocol_only(result)


@pytest.mark.parametrize("mutation", ["absent", "failed", "error", "fake"])
def test_expected_missing_document_cannot_hide_protocol_failure(mutation):
    result = analysis()
    branch = result["branches"]["material"]
    if mutation == "absent":
        del result["branches"]["material"]
    elif mutation == "failed":
        branch["status"] = "failed"
    elif mutation == "error":
        branch["error_code"] = "invalid_response"
    else:
        branch["engine_mode"] = "fake"
    assert verification.protocol_failures(result) == ["material"]
    assert not verification.protocol_only(result)
    result["missing_fields"] = ["branch_review:material"]
    assert verification.protocol_only(result)


def test_substantive_branch_risk_is_not_retryable_protocol_failure():
    result = analysis()
    result["missing_fields"] = ["整改有效性待核实"]
    assert not verification.protocol_only(result)
