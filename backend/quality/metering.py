"""可选追加式计量事件；不记录请求正文、认证头或隐藏思维。"""
import json
import os
import threading
from pathlib import Path

from quality.config import ROOT

_write_lock = threading.Lock()


def emit_meter_event(event, metadata):
    target = os.environ.get("QUALITY_METERING_FILE")
    if not target:
        return
    path = Path(target)
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    allowed = (ROOT / "artifacts/benchmarks", ROOT / "data/runtime")
    if path.suffix != ".jsonl" or not any(path.is_relative_to(base.resolve()) for base in allowed):
        raise ValueError("计量日志只能追加到本项目benchmarks/runtime下的jsonl")
    line = json.dumps({"event": event, "metadata": metadata}, ensure_ascii=False)
    with _write_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as output:
            output.write(line + "\n")
