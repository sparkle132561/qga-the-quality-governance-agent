"""显式使用本机.env.live启动正常开发库8000；不使用fake_only演示库。"""

import argparse
import os
import sys
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from quality import providers  # noqa: E402
from quality.config import Settings  # noqa: E402
from quality.main import create_app  # noqa: E402
from quality.request_gate import RequestGate  # noqa: E402

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-port", type=int, default=55432)
    parser.add_argument(
        "--request-interval",
        type=float,
        default=float(os.environ.get("QUALITY_REQUEST_INTERVAL", "4")),
        help="真实请求最小间隔秒数（免费档平滑门禁），0恢复默认闸门",
    )
    args = parser.parse_args()
    if not 1 <= args.postgres_port <= 65535:
        parser.error("数据库端口不合法")
    if not (ROOT / ".env.live").exists():
        raise SystemExit("请先配置本机.env.live，密钥不应提交")
    settings = Settings(_env_file=ROOT / ".env.live", model_mode="live",
                        database_url="postgresql+psycopg://quality:quality_demo@127.0.0.1:"
                                     + str(args.postgres_port) + "/quality")
    if not settings.agnes_api_key or settings.agnes_base_url != "https://apihub.agnes-ai.com/v1":
        raise SystemExit("缺少Agnes Key或未使用已核验官方入口")
    if args.request_interval > 0:
        # 免费档429高发；沿用评测验证过的单并发平滑门禁，串行化分支并发请求。
        providers.MODEL_REQUEST_GATE = RequestGate(
            concurrent=1, starts=1, window=args.request_interval
        )
    uvicorn.run(create_app(settings), host="127.0.0.1", port=8000)
