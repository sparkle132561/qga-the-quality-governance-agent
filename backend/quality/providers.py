import hashlib
import math
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

import httpx

from quality.metering import emit_meter_event
from quality.request_gate import MODEL_REQUEST_GATE, GateUnavailable

# 429无Retry-After时的保守退避秒数；只在请求时间预算内重试一次。
RATE_LIMIT_BACKOFF_SECONDS = 8.0

EMBEDDING_MODEL = "BAAI/bge-m3"
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
EMBEDDING_DIMENSIONS = 1024


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str, attempted=None, request_meta=None):
        self.code = code
        self.attempted = attempted
        self.request_meta = request_meta or {}
        super().__init__(message)


def post_read_request(url, key, payload, timeout, transport=None, control=None):
    try:
        response, meta = _post_read_request(url, key, payload, timeout, transport, control)
    except ProviderError as error:
        emit_meter_event("call_finished", {**error.request_meta, "error_code": error.code})
        raise
    emit_meter_event("call_finished", meta)
    return response, meta


def _post_read_request(url, key, payload, timeout, transport=None, control=None):
    """只读模型请求最多两次；429必须有可等待的短Retry-After，业务写入不重放。"""
    started = time.monotonic()
    attempts, queue_ms = 0, 0.0
    backoff_ms = 0.0
    records = []
    call_id = str(uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    endpoint = httpx.URL(url)

    def metadata():
        return {"metering_schema_version": 1, "local_call_id": call_id,
                "model": payload.get("model"), "provider": endpoint.host,
                "endpoint": endpoint.path, "started_at": started_at,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "engine_mode": "contract_fixture" if transport else "live",
                "attempts": attempts, "retries": max(0, attempts - 1), "queue_ms": queue_ms,
                "backoff_ms": backoff_ms,
                "request_elapsed_ms": (time.monotonic() - started) * 1000,
                "attempt_records": records}

    for attempt in range(2):
        try:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise ProviderError(
                    "request_deadline", "请求时间预算已用尽", attempts > 0, metadata()
                )
            waiting = time.monotonic()
            permit = nullcontext() if transport else MODEL_REQUEST_GATE.permit(remaining, control)
            with permit:
                queue_ms += (time.monotonic() - waiting) * 1000
                if control and control():
                    raise ProviderError(
                        "analysis_cancelled", "排队中的请求已取消", attempts > 0, metadata()
                    )
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise ProviderError(
                        "request_deadline", "排队后请求预算已用尽", attempts > 0, metadata()
                    )
                attempts += 1
                record = {"local_attempt_id": str(uuid4()), "status_code": None,
                          "started_at": datetime.now(timezone.utc).isoformat()}
                records.append(record)
                sending = time.monotonic()
                try:
                    headers = {"Authorization": "Bearer " + key}
                    if endpoint.host == "api.siliconflow.cn":
                        headers["X-Trace-Id"] = record["local_attempt_id"]
                    with httpx.Client(timeout=remaining, transport=transport) as client:
                        request = client.build_request("POST", url, headers=headers, json=payload)
                        record.update(request_json_bytes=len(request.content),
                                      input_sha256=hashlib.sha256(request.content).hexdigest())
                        emit_meter_event("attempt_started", metadata())
                        sending = time.monotonic()
                        response = client.send(request)
                        record.update(status_code=response.status_code,
                                      response_bytes=len(response.content),
                                      provider_trace_id=response.headers.get("x-siliconcloud-trace-id")
                                      or response.headers.get("x-request-id"),
                                      retry_after=response.headers.get("retry-after"))
                        try:
                            data = response.json()
                        except ValueError:
                            data = None
                        if isinstance(data, dict):
                            record.update(response_usage_fields(data))
                            error = data.get("error")
                            if isinstance(error, dict):
                                record["provider_error"] = {
                                    key: str(error[key])[:120] for key in ("code", "type")
                                    if isinstance(error.get(key), (str, int))}
                except httpx.HTTPError as error:
                    record["transport_error"] = type(error).__name__
                    raise
                finally:
                    record.update(elapsed_ms=(time.monotonic() - sending) * 1000,
                                  finished_at=datetime.now(timezone.utc).isoformat())
                    emit_meter_event("attempt_finished", metadata())
            delay = 0.1
            if response.status_code == 429:
                try:
                    delay = float(response.headers.get("Retry-After", "nan"))
                except ValueError:
                    delay = float("nan")
                if not (math.isfinite(delay) and 0 <= delay):
                    # 免费档429常不带Retry-After；用保守固定退避，仅在预算内重试一次。
                    delay = RATE_LIMIT_BACKOFF_SECONDS
                retry = 0 <= delay <= 30
            else:
                retry = response.status_code in {502, 503, 504}
            if not retry or attempt == 1:
                return response, metadata()
        except GateUnavailable as error:
            queue_ms += (time.monotonic() - waiting) * 1000
            raise ProviderError(
                str(error), "本地请求容量不足或运行已取消", attempts > 0, metadata()
            ) from None
        except httpx.HTTPError:
            if attempt == 1:
                raise ProviderError(
                    "unavailable",
                    "模型网络请求失败",
                    True,
                    metadata(),
                ) from None
            delay = 0.1
        if time.monotonic() - started + delay >= timeout:
            raise ProviderError(
                "request_deadline", "重试超过当前请求预算", attempts > 0, metadata()
            )
        until = time.monotonic() + delay
        sleeping = time.monotonic()
        while time.monotonic() < until:
            if control and control():
                backoff_ms += (time.monotonic() - sleeping) * 1000
                raise ProviderError(
                    "analysis_cancelled", "重试前运行已取消", attempts > 0, metadata()
                )
            time.sleep(min(0.05, max(0, until - time.monotonic())))
        backoff_ms += (time.monotonic() - sleeping) * 1000


def response_usage_fields(data):
    """只抽取计量字段；重试中的失败响应也保留，不记录正文或认证头。"""
    vendor = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    source = "usage" if usage else "unknown"
    tokens = vendor.get("tokens", {})
    if not usage and isinstance(tokens, dict):
        values = {"prompt_tokens": tokens.get("input_tokens"),
                  "completion_tokens": tokens.get("output_tokens")}
        usage = {key: value for key, value in values.items()
                 if isinstance(value, int) and not isinstance(value, bool) and value >= 0}
        if len(usage) == 2 and tokens.get("image_tokens", 0) == 0:
            usage["total_tokens"] = sum(usage.values())
        if usage:
            source = "meta.tokens"
    return {"usage": usage, "usage_source": source,
            "provider_response_id": data.get("id") if isinstance(data.get("id"), str) else None,
            "provider_reported_model": data.get("model")
            if isinstance(data.get("model"), str) else None,
            "provider_meta": {key: vendor[key]
                              for key in ("tokens", "billed_units") if key in vendor}}


def record_response_usage(data, meta):
    meta.update(response_usage_fields(data))
    emit_meter_event("usage_recorded", meta)
    return meta["usage"]


@dataclass
class EmbeddingBatch:
    vectors: list[list[float]]
    usage: dict
    elapsed_ms: float
    request_meta: dict = field(default_factory=dict)


class SiliconFlowClient:
    def __init__(self, key: str, base_url: str = "https://api.siliconflow.cn/v1", transport=None):
        self.key = key
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.control = None
        self.timeout_seconds = 25

    def _post(self, endpoint: str, payload: dict) -> tuple[dict, float]:
        if not self.key:
            raise ProviderError(
                "missing_key",
                "硅基流动Key尚未配置；没有执行真实调用",
                False,
                {"attempts": 0, "retries": 0, "queue_ms": 0},
            )
        started = time.perf_counter()
        meta = {"attempts": 0, "retries": 0, "queue_ms": 0}
        try:
            response, meta = post_read_request(
                self.base_url + endpoint, self.key, payload, self.timeout_seconds, self.transport,
                self.control,
            )
            if response.status_code >= 400:
                raise ProviderError(
                    f"http_{response.status_code}",
                    f"模型服务返回HTTP {response.status_code}",
                    True,
                    meta,
                )
            result = response.json()
        except (httpx.HTTPError, ValueError):
            raise ProviderError("unavailable", "模型服务不可达或响应不是有效JSON",
                                meta["attempts"] > 0, meta) from None
        if not isinstance(result, dict):
            raise ProviderError("invalid_response", "模型服务响应结构无效", True, meta)
        record_response_usage(result, meta)
        result["request_meta"] = meta
        return result, (time.perf_counter() - started) * 1000

    def embed(self, texts: list[str]) -> EmbeddingBatch:
        if (
            not texts
            or len(texts) > 32
            or any(not text.strip() or len(text) > 6000 for text in texts)
        ):
            raise ValueError("Embedding输入需1～32条，每条1～6000字符")
        response, elapsed = self._post(
            "/embeddings", {"model": EMBEDDING_MODEL, "input": texts, "encoding_format": "float"}
        )
        try:
            values = response["data"]
            if response.get("model", EMBEDDING_MODEL) != EMBEDDING_MODEL:
                raise ValueError("返回的模型不符")
            if any(
                isinstance(row["index"], bool) or not isinstance(row["index"], int)
                for row in values
            ):
                raise ValueError("非法输入索引")
            if len(values) != len(texts) or {row["index"] for row in values} != set(
                range(len(texts))
            ):
                raise ValueError("数量或索引不匹配")
            vectors = [row["embedding"] for row in sorted(values, key=lambda row: row["index"])]
            for vector in vectors:
                if (
                    len(vector) != EMBEDDING_DIMENSIONS
                    or any(
                        isinstance(number, bool)
                        or not isinstance(number, (int, float))
                        or not math.isfinite(number)
                        for number in vector
                    )
                    or not any(number != 0 for number in vector)
                ):
                    raise ValueError("维度、数值或零向量无效")
        except (KeyError, TypeError, ValueError):
            raise ProviderError("invalid_response", "Embedding数量、索引或向量不符合契约",
                                True, response["request_meta"]) from None
        return EmbeddingBatch(vectors, response["request_meta"]["usage"], elapsed,
                              response["request_meta"])

    def rerank(self, query: str, documents: list[str], top_n: int) -> dict:
        if (
            not query.strip()
            or not documents
            or len(documents) > 80
            or not 1 <= top_n <= len(documents)
        ):
            raise ValueError("重排输入或top_n无效")
        response, elapsed = self._post(
            "/rerank",
            {
                "model": RERANK_MODEL,
                "query": query,
                "documents": documents,
                "top_n": top_n,
                "return_documents": False,
            },
        )
        try:
            results = response["results"]
            indices = [row["index"] for row in results]
            if (
                len(results) > top_n
                or len(set(indices)) != len(indices)
                or any(
                    isinstance(index, bool)
                    or not isinstance(index, int)
                    or not 0 <= index < len(documents)
                    for index in indices
                )
                or any(not math.isfinite(row["relevance_score"]) for row in results)
            ):
                raise ValueError("非法候选索引或分数")
        except (KeyError, TypeError, ValueError):
            raise ProviderError("invalid_response", "重排索引或分数不符合契约",
                                True, response["request_meta"]) from None
        return {
            "results": results,
            "usage": response["request_meta"]["usage"],
            "elapsed_ms": elapsed,
            "request_meta": response["request_meta"],
        }


class AgnesClient:
    def __init__(
        self,
        key: str,
        base_url: str = "https://apihub.agnes-ai.com/v1",
        model: str = "agnes-3.0-flash",
        transport=None,
    ):
        if model not in {"agnes-3.0-flash", "agnes-2.5-flash"}:
            raise ValueError("首版仅允许已选免费Flash候选，不自动切换收费模型")
        self.key, self.base_url, self.model = key, base_url.rstrip("/"), model
        self.transport = transport
        # 平滑门禁下分支请求需排队；排队预算与分支90秒预算对齐。
        self.timeout_seconds = 90
        self.control = None
        self.tool_choice = "auto"

    def complete(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        if not self.key:
            raise ProviderError(
                "missing_agnes_key",
                "Agnes Key尚未配置；没有执行真实模型调用",
                False,
                {"attempts": 0, "retries": 0, "queue_ms": 0},
            )
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": 2048,
            "temperature": 0,
            "stream": False,
        }
        if tools:
            payload.update(tools=tools, tool_choice=self.tool_choice)
        started = time.perf_counter()
        meta = {"attempts": 0, "retries": 0, "queue_ms": 0}
        try:
            response, meta = post_read_request(
                self.base_url + "/chat/completions",
                self.key,
                payload,
                self.timeout_seconds,
                self.transport,
                self.control,
            )
            if response.status_code >= 400:
                raise ProviderError(
                    f"http_{response.status_code}",
                    f"Agnes返回HTTP {response.status_code}",
                    True,
                    meta,
                )
            content = response.json()
            if isinstance(content, dict):
                record_response_usage(content, meta)
            message = content["choices"][0]["message"]
            if not isinstance(message, dict):
                raise ValueError("消息不是对象")
            if message.get("content") is not None and not isinstance(message["content"], str):
                raise ValueError("内容不是文本")
            calls = message.get("tool_calls") or []
            if not isinstance(calls, list):
                raise ValueError("工具调用不是列表")
            if len(calls) > 8:
                raise ValueError("单次工具调用数量超过首版限制")
            call_ids = set()
            for call in calls:
                if (
                    not isinstance(call, dict)
                    or call.get("type") != "function"
                    or not isinstance(call.get("id"), str)
                    or not call["id"]
                    or call["id"] in call_ids
                    or not isinstance(call.get("function"), dict)
                    or not isinstance(call["function"].get("name"), str)
                    or not isinstance(call["function"].get("arguments"), str)
                ):
                    raise ValueError("工具调用信封无效")
                call_ids.add(call["id"])
            if message.get("role") != "assistant" or not (
                (isinstance(message.get("content"), str) and message["content"].strip())
                or message.get("tool_calls")
            ):
                raise ValueError("没有可用的assistant回复")
            # 只保留可观察回答和工具调用，不保存reasoning_content等隐藏思维字段。
            message = {
                key: message[key] for key in ("role", "content", "tool_calls") if key in message
            }
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            raise ProviderError(
                "invalid_agnes_response", "Agnes响应不可达或不符合消息契约",
                meta["attempts"] > 0, meta,
            ) from None
        return {
            "message": message,
            "usage": content.get("usage", {}),
            "request_meta": meta,
            "elapsed_ms": (time.perf_counter() - started) * 1000,
            "engine_mode": "contract_fixture" if self.transport else "live",
        }
