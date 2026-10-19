"""规范JSON响应包装；schema与引用校验仍由各调用方执行。"""

import json
import re

from quality.providers import AgnesClient


def json_payload(content: str | None) -> str:
    text = (content or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", text, flags=re.DOTALL)
    return fenced[1] if fenced else text


def complete_structured(model, messages, schema, function_name):
    if not isinstance(model, AgnesClient):
        return model.complete(messages)
    model.tool_choice = {"type": "function", "function": {"name": function_name}}
    return model.complete(
        messages,
        [
            {
                "type": "function",
                "function": {
                    "name": function_name,
                    "description": "提交结构化抽取候选；不执行任何业务操作",
                    "parameters": schema,
                },
            }
        ],
    )


def structured_payload(response, function_name):
    message = response["message"]
    calls = message.get("tool_calls") or []
    if calls:
        if (
            len(calls) != 1
            or calls[0].get("type") != "function"
            or calls[0].get("function", {}).get("name") != function_name
        ):
            raise ValueError("结构化抽取必须仅提交指定函数")
        arguments = calls[0]["function"].get("arguments")
        if not isinstance(arguments, str):
            raise ValueError("结构化函数参数必须是JSON文本")
        value = json.loads(arguments)
        if not isinstance(value, dict):
            raise ValueError("结构化函数参数必须是对象")
        # 真实Agnes函数返回曾把整个证据对象二次JSON编码；只解码已观察到的容器。
        # 不改品牌、日期等字段值，不猜测或修复非法JSON，之后仍执行严格schema与原文校验。
        if isinstance(value.get("field_evidence"), str):
            evidence = json.loads(value["field_evidence"])
            if not isinstance(evidence, dict):
                raise ValueError("field_evidence必须是JSON对象")
            value["field_evidence"] = evidence
        return json.dumps(value, ensure_ascii=False)
    return json_payload(message.get("content"))
