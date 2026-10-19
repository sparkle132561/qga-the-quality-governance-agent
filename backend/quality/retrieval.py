import hashlib
import json
import re
import threading
import time
from datetime import date
from functools import lru_cache
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

import jieba
from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from rank_bm25 import BM25Okapi
from sqlalchemy import delete, or_, select

from quality.config import ROOT
from quality.domain import Actor, Role
from quality.models import EvidenceChunk, Material, RuleVersion, SourceRecord
from quality.parsing import parse_material
from quality.providers import EMBEDDING_MODEL, AgnesClient, ProviderError, SiliconFlowClient
from quality.query_planning import rerank_hits, rewrite_query

STOP_WORDS = {"的", "和", "与", "了", "吗", "请", "我", "这个", "什么", "怎么", "需要"}
_index_lock = threading.Lock()


@lru_cache(maxsize=4096)
def tokenize(text: str) -> tuple[str, ...]:
    tokens = []
    for segment in re.findall(r"[a-z0-9][a-z0-9._-]*|[\u4e00-\u9fff]+", text.lower()):
        candidates = jieba.lcut(segment) if re.search(r"[\u4e00-\u9fff]", segment) else [segment]
        tokens.extend(token for token in candidates if token.strip() and token not in STOP_WORDS)
    return tuple(tokens)


def split_text(text: str, max_chars: int = 700, overlap: int = 80):
    if max_chars <= 0 or not 0 <= overlap < max_chars:
        raise ValueError("分块大小与重叠不合法")
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            boundary = max(
                text.rfind("\n", start + max_chars // 2, end),
                text.rfind("。", start + max_chars // 2, end),
            )
            if boundary >= 0:
                end = boundary + 1
        yield text[start:end], start, end
        if end == len(text):
            break
        start = max(start + 1, end - overlap)


def index_units(
    session, document_id, units, sha256, is_demo, rule_id=None, case_id=None, material_id=None
):
    ids = []
    added = 0
    for unit in units:
        for text, start, end in split_text(unit["text"]):
            if not text.strip():
                continue
            location = {**unit["location"], "char_start": start, "char_end": end}
            chunk_id = str(
                uuid5(
                    NAMESPACE_URL,
                    document_id
                    + sha256
                    + text
                    + json.dumps(location, sort_keys=True, ensure_ascii=False),
                )
            )
            ids.append(chunk_id)
            if session.get(EvidenceChunk, chunk_id) is None:
                session.add(
                    EvidenceChunk(
                        id=chunk_id,
                        document_id=document_id,
                        rule_id=rule_id,
                        case_id=case_id,
                        material_id=material_id,
                        text=text,
                        location=location,
                        source_sha256=sha256,
                        is_demo=is_demo,
                    )
                )
                added += 1
    session.flush()
    # 索引是可重建投影，移除同文档旧快照，避免旧向量混入新证据。
    session.execute(
        delete(EvidenceChunk).where(
            EvidenceChunk.document_id == document_id, EvidenceChunk.id.not_in(ids)
        )
    )
    return added


def index_rule_documents(session):
    added = 0
    missing = []
    for rule in session.scalars(select(RuleVersion)):
        source = session.get(SourceRecord, rule.source_id)
        if not rule.is_demo:
            path = ROOT / "data/runtime/sources" / (rule.source_id + ".pdf")
            if not path.is_file():
                missing.append(rule.source_id)
                continue
            data = path.read_bytes()
            try:
                parsed = parse_material(data, ".pdf")
            except Exception:
                raise HTTPException(422, "官方规则缓存无法解析，请重新下载源文件") from None
            units = parsed["units"]
            digest = hashlib.sha256(data).hexdigest()
        else:
            units = [{"text": rule.content, "location": {"kind": "demo_rule", "rule_id": rule.id}}]
            digest = hashlib.sha256(rule.content.encode()).hexdigest()
        units = [
            {
                **unit,
                "location": {
                    **unit["location"],
                    "source_id": rule.source_id,
                    "source_url": source.url,
                    "rule_version": rule.version,
                    "source_kind": source.source_kind,
                },
            }
            for unit in units
        ]
        added += index_units(
            session, "rule:" + rule.id, units, digest, rule.is_demo, rule_id=rule.id
        )
    chunks = list(
        session.scalars(
            select(EvidenceChunk)
            .where(EvidenceChunk.rule_id.is_not(None))
            .order_by(EvidenceChunk.id)
        )
    )
    manifest = "\n".join(chunk.id + ":" + chunk.source_sha256 for chunk in chunks)
    return {
        "added_chunks": added,
        "missing_sources": missing,
        "total_chunks": len(chunks),
        "snapshot_sha256": hashlib.sha256(manifest.encode()).hexdigest(),
    }


def eligible_query(as_of: date, case_id: str | None):
    valid_rules = select(RuleVersion.id).where(
        RuleVersion.effective_from <= as_of,
        or_(RuleVersion.effective_until.is_(None), RuleVersion.effective_until > as_of),
    )
    public = EvidenceChunk.rule_id.in_(valid_rules)
    scope = public if case_id is None else or_(public, EvidenceChunk.case_id == case_id)
    return select(EvidenceChunk).where(scope)


def result_hit(chunk, score: float, method: str):
    return {
        "id": chunk.id,
        "text": chunk.text,
        "location": chunk.location,
        "document_id": chunk.document_id,
        "rule_id": chunk.rule_id,
        "material_id": chunk.material_id,
        "case_id": chunk.case_id,
        "source_sha256": chunk.source_sha256,
        "is_demo": chunk.is_demo,
        "score": score,
        "method": method,
    }


def keyword_search(session, query: str, as_of: date, limit: int, case_id=None):
    documents = list(
        session.scalars(eligible_query(as_of, case_id).order_by(EvidenceChunk.id).limit(2001))
    )
    if len(documents) > 2000:
        raise HTTPException(409, "首版关键词语料超过2000块，请缩小案件或资料范围")
    if not documents:
        return []
    corpus = [list(tokenize(document.text)) for document in documents]
    terms = list(tokenize(query))
    if not terms:
        return []
    nonempty = [index for index, tokens in enumerate(corpus) if tokens]
    if not nonempty:
        return []
    ranker = BM25Okapi([corpus[index] for index in nonempty])
    scores = ranker.get_scores(terms)
    candidates = [
        (documents[index], float(score))
        for index, score in zip(nonempty, scores)
        if set(terms).intersection(corpus[index])
    ]
    candidates.sort(key=lambda item: (-item[1], item[0].id))
    return [result_hit(chunk, score, "bm25") for chunk, score in candidates[:limit]]


def vector_search(session, vector, as_of, limit, case_id=None, model=EMBEDDING_MODEL):
    query = eligible_query(as_of, case_id).where(
        EvidenceChunk.embedding.is_not(None), EvidenceChunk.embedding_model == model
    )
    distance = EvidenceChunk.embedding.cosine_distance(vector)
    rows = session.execute(
        query.add_columns(distance.label("distance"))
        .order_by(distance, EvidenceChunk.id)
        .limit(limit)
    )
    return [result_hit(chunk, 1 - float(distance), "vector") for chunk, distance in rows]


def rrf_fuse(rankings: dict[str, list[dict]], limit: int, rank_constant: int = 60):
    if limit < 1 or rank_constant < 1:
        raise ValueError("RRF限制和排名常数必须为正")
    merged = {}
    for method, hits in rankings.items():
        seen = set()
        for rank, hit in enumerate(hits, start=1):
            if hit["id"] in seen:
                continue
            seen.add(hit["id"])
            candidate = merged.setdefault(
                hit["id"],
                {**hit, "score": 0.0, "method": "hybrid", "ranks": {}, "component_scores": {}},
            )
            candidate["score"] += 1 / (rank_constant + rank)
            candidate["ranks"][method] = rank
            candidate["component_scores"][method] = hit["score"]
    return sorted(merged.values(), key=lambda hit: (-hit["score"], hit["id"]))[:limit]


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=2000)
    as_of: date
    limit: int = Field(default=5, ge=1, le=20)
    case_id: str | None = None
    method: Literal["bm25", "vector", "hybrid"] = "bm25"
    rewrite: bool = False
    rerank: bool = False

    @field_validator("query")
    @classmethod
    def nonblank_query(cls, value):
        if not value.strip():
            raise ValueError("检索问题不能为空白")
        return value.strip()


def register_retrieval_routes(app, settings, sessions, actor, visible_case):
    @app.post("/api/admin/index-rules")
    def index_rules(who: Actor = Depends(actor)):
        if who.role != Role.ADMIN:
            raise HTTPException(403, "只有管理员可以重建规则索引")
        with _index_lock, sessions.begin() as session:
            return index_rule_documents(session)

    @app.post("/api/cases/{case_id}/index-materials")
    def index_materials(case_id: str, who: Actor = Depends(actor)):
        with _index_lock, sessions.begin() as session:
            visible_case(session, case_id, who)
            added = 0
            for material in session.scalars(
                select(Material).where(Material.case_id == case_id, Material.status != "failed")
            ):
                added += index_units(
                    session,
                    "material:" + material.id,
                    material.parsed["units"],
                    material.sha256,
                    True,
                    case_id=case_id,
                    material_id=material.id,
                )
            return {"added_chunks": added, "model_mode": "not_called"}

    def search_impl(request, who, control=None, deadline=None):
        started = time.perf_counter()
        def budget(client, default):
            if control and control():
                raise ProviderError("analysis_cancelled", "检索已取消或来源变化", False)
            remaining = deadline - time.monotonic() if deadline else default
            if remaining <= 0:
                raise ProviderError("request_deadline", "检索预算已用尽", False)
            client.timeout_seconds = min(default, remaining)
            client.control = control
            return client
        with sessions() as session:
            if request.case_id:
                visible_case(session, request.case_id, who)
            embedding_ms = 0
            keyword_ms = 0
            vector_ms = 0
            fusion_ms = 0
            query_plan = {
                "original_query": request.query,
                "queries": [request.query],
                "status": "disabled",
                "elapsed_ms": 0,
                "usage": {},
            }
            rerank_plan = {"status": "disabled", "elapsed_ms": 0, "usage": {}}
            query_runs = []
            embedding_requests = []
            stage = "retrieval"
            try:
                if request.rewrite:
                    stage = "rewrite"
                    query_plan = rewrite_query(
                        request.query,
                        budget(AgnesClient(
                            settings.agnes_api_key, settings.agnes_base_url, settings.agnes_model
                        ), 35),
                    )
                query_rankings = {}
                candidate_limit = (
                    request.limit * 3
                    if (request.rerank or request.rewrite or request.method == "hybrid")
                    else request.limit
                )
                client = SiliconFlowClient(
                    settings.siliconflow_api_key, settings.siliconflow_base_url
                )
                for query_number, query in enumerate(query_plan["queries"]):
                    if request.method == "bm25":
                        stage_started = time.perf_counter()
                        hits = keyword_search(
                            session, query, request.as_of, candidate_limit, request.case_id
                        )
                        keyword_ms += (time.perf_counter() - stage_started) * 1000
                    else:
                        stage = "embedding"
                        batch = budget(client, 25).embed([query])
                        embedding_requests.append(
                            {"request_meta": batch.request_meta, "usage": batch.usage}
                        )
                        embedding_ms += batch.elapsed_ms
                        stage_started = time.perf_counter()
                        hits = vector_search(
                            session,
                            batch.vectors[0],
                            request.as_of,
                            candidate_limit,
                            request.case_id,
                        )
                        vector_ms += (time.perf_counter() - stage_started) * 1000
                        if request.method == "hybrid":
                            stage_started = time.perf_counter()
                            lexical = keyword_search(
                                session, query, request.as_of, candidate_limit, request.case_id
                            )
                            keyword_ms += (time.perf_counter() - stage_started) * 1000
                            stage_started = time.perf_counter()
                            hits = rrf_fuse({"bm25": lexical, "vector": hits}, candidate_limit)
                            fusion_ms += (time.perf_counter() - stage_started) * 1000
                    query_rankings[f"query_{query_number}"] = hits
                    query_runs.append(
                        {
                            "query": query,
                            "candidates": [
                                {"id": hit["id"], "score": hit["score"], "ranks": hit.get("ranks")}
                                for hit in hits
                            ],
                        }
                    )
                if len(query_rankings) > 1:
                    stage_started = time.perf_counter()
                    hits = rrf_fuse(query_rankings, candidate_limit)
                    for hit in hits:
                        hit["method"] = "multi_query_" + request.method
                    fusion_ms += (time.perf_counter() - stage_started) * 1000
                if request.rerank:
                    stage = "rerank"
                    hits, rerank_plan = rerank_hits(
                        request.query, hits, budget(client, 25), request.limit
                    )
                else:
                    hits = hits[: request.limit]
            except ProviderError as exc:
                raise HTTPException(503, {
                    "code": exc.code, "message": str(exc), "failed_stage": stage,
                    "request_meta": exc.request_meta,
                    "elapsed_ms": (time.perf_counter() - started) * 1000,
                    "partial_usage": {"rewrite": query_plan.get("usage", {}),
                                      "rewrite_request_meta": query_plan.get("request_meta", {}),
                                      "embedding": embedding_requests},
                }) from None
            return {
                "hits": hits,
                "timing_ms": {
                    "embedding": embedding_ms,
                    "keyword": keyword_ms,
                    "vector_query": vector_ms,
                    "fusion": fusion_ms,
                    "rewrite": query_plan["elapsed_ms"],
                    "rerank": rerank_plan["elapsed_ms"],
                    "total": (time.perf_counter() - started) * 1000,
                },
                "method": request.method,
                "provider_requests": {
                    "embedding": embedding_requests,
                    "rewrite": query_plan.get("request_meta", {}),
                    "rerank": rerank_plan.get("request_meta", {}),
                },
                "query_plan": query_plan,
                "query_runs": query_runs,
                "rerank_plan": rerank_plan,
                "fusion_config": {"rank_constant": 60, "candidate_multiplier": 3}
                if request.method == "hybrid"
                else None,
                "notice": "只返回证据，不生成审核结论",
            }

    @app.post("/api/search")
    def search(request: SearchRequest, who: Actor = Depends(actor)):
        return search_impl(request, who)

    @app.post("/api/admin/embed-rules")
    def embed_rules(who: Actor = Depends(actor)):
        if who.role != Role.ADMIN:
            raise HTTPException(403, "只有管理员可以构建规则向量")
        client = SiliconFlowClient(settings.siliconflow_api_key, settings.siliconflow_base_url)
        with _index_lock, sessions.begin() as session:
            chunks = list(
                session.scalars(
                    select(EvidenceChunk)
                    .where(EvidenceChunk.rule_id.is_not(None), EvidenceChunk.embedding.is_(None))
                    .order_by(EvidenceChunk.id)
                    .limit(32)
                )
            )
            if not chunks:
                return {"embedded": 0, "usage": {}, "notice": "没有待处理规则分块"}
            try:
                batch = client.embed([chunk.text for chunk in chunks])
            except ProviderError as exc:
                raise HTTPException(503, {"code": exc.code, "message": str(exc)}) from None
            for chunk, vector in zip(chunks, batch.vectors):
                chunk.embedding = vector
                chunk.embedding_model = EMBEDDING_MODEL
            return {"embedded": len(chunks), "usage": batch.usage, "elapsed_ms": batch.elapsed_ms,
                    "request_meta": batch.request_meta}

    return search_impl
