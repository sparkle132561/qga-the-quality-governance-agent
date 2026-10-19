import json

import httpx
import pytest
from quality.json_response import complete_structured, structured_payload
from quality.providers import AgnesClient


def test_extraction_forces_named_output_function_without_executing_tools():
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "selection-1",
                                    "type": "function",
                                    "function": {
                                        "name": "submit_incident",
                                        "arguments": '{"brand":"万利达"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    model = AgnesClient(
        "test-not-secret", model="agnes-2.5-flash", transport=httpx.MockTransport(respond)
    )
    result = complete_structured(
        model, [{"role": "user", "content": "品牌：万利达"}], {"type": "object"}, "submit_incident"
    )
    assert requests[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "submit_incident"},
    }
    assert json.loads(structured_payload(result, "submit_incident")) == {"brand": "万利达"}


@pytest.mark.parametrize("names", [["wrong_name"], ["submit_incident", "submit_incident"]])
def test_output_channel_rejects_wrong_or_multiple_function_calls(names):
    response = {
        "message": {
            "tool_calls": [
                {"type": "function", "function": {"name": name, "arguments": "{}"}}
                for name in names
            ]
        }
    }
    with pytest.raises(ValueError):
        structured_payload(response, "submit_incident")


def test_legacy_text_remains_strict_json_and_does_not_extract_from_prose():
    response = {"message": {"content": '答案是：{"brand":"万利达"}'}}
    with pytest.raises(ValueError):
        json.loads(structured_payload(response, "submit_incident"))


def test_function_json_string_container_is_decoded_without_changing_field_values():
    arguments = {
        "brand": "万利达",
        "field_evidence": json.dumps({"brand": {"line_start": 2, "line_end": 2}}),
    }
    response = {
        "message": {
            "tool_calls": [
                {
                    "type": "function",
                    "function": {"name": "submit_incident", "arguments": json.dumps(arguments)},
                }
            ]
        }
    }
    result = json.loads(structured_payload(response, "submit_incident"))
    assert result["field_evidence"] == {"brand": {"line_start": 2, "line_end": 2}}
    assert result["brand"] == "万利达"


@pytest.mark.parametrize("encoded", ["[1,2]", "不是JSON"])
def test_encoded_evidence_must_be_a_complete_json_object(encoded):
    response = {
        "message": {
            "tool_calls": [
                {
                    "type": "function",
                    "function": {
                        "name": "submit_incident",
                        "arguments": json.dumps({"field_evidence": encoded}),
                    },
                }
            ]
        }
    }
    with pytest.raises(ValueError):
        structured_payload(response, "submit_incident")
