"""显式协议演示服务：仅用指定测试/专用演示库，不调用模型；所有回答标fake。"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import psycopg  # noqa: E402
import uvicorn  # noqa: E402
from quality.config import Settings  # noqa: E402
from quality.demo_environment import verify_marker  # noqa: E402
from quality.main import create_app  # noqa: E402
from quality.providers import AgnesClient  # noqa: E402

FIXTURE_DELAY_SECONDS = 0


def protocol_reply(self, messages, tools=None):
    time.sleep(FIXTURE_DELAY_SECONDS)
    tool_messages = [message for message in messages if message["role"] == "tool"]
    if not tool_messages:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "protocol-read",
                    "type": "function",
                    "function": {
                        "name": "read_branch_evidence"
                        if any(
                            tool["function"]["name"] == "read_branch_evidence"
                            for tool in tools or []
                        )
                        else "read_current_case",
                        "arguments": "{}",
                    },
                }
            ],
        }
    else:
        hits = json.loads(tool_messages[-1]["content"])["data"].get("hits", [])
        advice = {
            "recommendation": "review_ready" if hits else "needs_info",
            "explanation": "协议替身：仅测试工具与流程，不是模型判断",
            "evidence_ids": [hit["id"] for hit in hits],
            "missing_fields": [],
        }
        if "material_assessments" in messages[0]["content"]:
            advice["material_assessments"] = []
            specialty = json.loads(messages[1]["content"])["facts"]["specialty"]
            if specialty == "material":
                for hit in hits:
                    text = hit["text"]
                    kind = (
                        "certificate"
                        if "CERTIFICATE" in text
                        else "label"
                        if "LABEL" in text
                        else "test_report"
                    )
                    advice["material_assessments"].append(
                        {"source_id": hit["id"], "document_type": kind, "quote": text[:300]}
                    )
        if '"assessments"' in messages[0]["content"]:
            specialty = json.loads(messages[1]["content"])["facts"]["specialty"]
            advice["assessments"] = (
                [{"source_id": hit["id"], "quote": hit["text"][:300]} for hit in hits]
                if specialty == "material"
                else []
            )
        message = {"role": "assistant", "content": json.dumps(advice)}
    return {"message": message, "engine_mode": "fake", "elapsed_ms": 0, "usage": {}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fake协议服务，不调用模型")
    parser.add_argument("--delay-seconds", type=float, default=0)
    parser.add_argument(
        "--database",
        choices=["quality_test", "quality_ui_test", "quality_demo"],
        default="quality_test",
    )
    parser.add_argument("--port", type=int, choices=[8001, 8002, 8050], default=8001)
    parser.add_argument("--postgres-port", type=int, default=55432)
    args = parser.parse_args()
    if not 0 <= args.delay_seconds <= 3:
        parser.error("协议延迟仅支持0～3秒")
    if not 1 <= args.postgres_port <= 65535:
        parser.error("数据库端口不合法")
    FIXTURE_DELAY_SECONDS = args.delay_seconds
    settings = Settings(
        database_url=(f"postgresql+psycopg://quality:quality_demo@127.0.0.1:{args.postgres_port}/"
                      + args.database),
        agnes_api_key="",
        siliconflow_api_key="",
        model_mode="fake",
        upload_dir=Path(__file__).resolve().parents[1] / "data/runtime/protocol-demo/uploads"
        if args.database == "quality_demo"
        else Settings().upload_dir,
    )
    if args.database == "quality_demo":
        with psycopg.connect(
            f"postgresql://quality:quality_demo@127.0.0.1:{args.postgres_port}/quality_demo",
            connect_timeout=5,
        ) as connection:
            verify_marker(connection)
    AgnesClient.complete = protocol_reply
    uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port)
