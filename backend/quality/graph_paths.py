import hashlib
import json
import time
from collections import defaultdict, deque
from datetime import date
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from quality.domain import Actor, Role
from quality.models import EvidenceChunk, GraphEntityLink, GraphExtraction
from quality.retrieval import _index_lock, eligible_query, result_hit


class PathRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    as_of: date
    case_id: str | None = None
    start_id: str = Field(min_length=1, max_length=160)
    end_id: str | None = Field(default=None, max_length=160)
    target_kind: (
        Literal["supplier", "product", "batch", "order", "rule", "organization", "document"] | None
    ) = None
    max_hops: int = Field(default=3, ge=1, le=3)
    limit: int = Field(default=10, ge=1, le=20)
    direction: Literal["out", "in", "both"] = "out"


class LinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    as_of: date
    case_id: str | None = None
    subject: str = Field(min_length=1, max_length=160)
    object: str = Field(min_length=1, max_length=160)
    note: str = Field(min_length=1, max_length=500)

    @field_validator("note")
    @classmethod
    def nonblank_note(cls, value):
        if not value.strip():
            raise ValueError("核验依据不能为空白")
        return value.strip()


def graph_signature(graph):
    return hashlib.sha256(
        json.dumps(
            {
                "nodes": sorted(graph["nodes"], key=lambda node: node["id"]),
                "edges": sorted(graph["edges"], key=lambda edge: json.dumps(edge, sort_keys=True)),
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def find_paths(graph, request: PathRequest, max_states=2000):
    nodes = {node["id"]: node for node in graph["nodes"]}
    if request.start_id not in nodes or (request.end_id and request.end_id not in nodes):
        raise HTTPException(404, "路径端点不存在或不在可见范围")
    adjacency = defaultdict(list)
    for edge in graph["edges"]:
        # 身份关联是对称的，其余边按请求方向遍历；仍保留原始边方向。
        if request.direction in ("out", "both") or edge["predicate"] == "same_entity":
            adjacency[edge["subject"]].append((edge["object"], edge, False))
        if request.direction in ("in", "both") or edge["predicate"] == "same_entity":
            adjacency[edge["object"]].append((edge["subject"], edge, True))
    paths, expanded = [], 0
    queue = deque([([request.start_id], [])])
    while queue:
        identifiers, edges = queue.popleft()
        if len(edges) >= request.max_hops:
            continue
        for neighbor, edge, reversed_edge in adjacency[identifiers[-1]]:
            if neighbor in identifiers:
                continue
            expanded += 1
            if expanded > max_states:
                return {
                    "paths": paths,
                    "truncated": True,
                    "stop_reason": "expansion_budget",
                    "expanded_states": expanded - 1,
                }
            path_ids = [*identifiers, neighbor]
            path_edges = [*edges, {**edge, "traversed_reverse": reversed_edge}]
            matches = (not request.end_id or neighbor == request.end_id) and (
                not request.target_kind or nodes[neighbor]["kind"] == request.target_kind
            )
            if matches:
                paths.append(
                    {
                        "nodes": [nodes[key] for key in path_ids],
                        "edges": path_edges,
                        "hops": len(path_edges),
                    }
                )
                if len(paths) >= request.limit:
                    return {
                        "paths": paths,
                        "truncated": True,
                        "stop_reason": "result_limit",
                        "expanded_states": expanded,
                    }
            if neighbor != request.end_id:
                queue.append((path_ids, path_edges))
    return {
        "paths": paths,
        "truncated": False,
        "stop_reason": "finished",
        "expanded_states": expanded,
    }


def register_path_routes(app, sessions, actor, visible_case, graph_reader):
    @app.post("/api/graph/identity-links")
    def link(request: LinkRequest, who: Actor = Depends(actor)):
        if who.role not in (Role.REVIEWER, Role.ADMIN):
            raise HTTPException(403, "只有平台身份可确认实体关联")
        with _index_lock, sessions.begin() as session:
            if request.case_id:
                case = visible_case(session, request.case_id, who)
                session.refresh(case, with_for_update=True)
            graph = graph_reader(as_of=request.as_of, case_id=request.case_id, who=who)
            nodes = {node["id"]: node for node in graph["nodes"]}
            if request.subject not in nodes or request.object not in nodes:
                raise HTTPException(404, "身份关联端点不在当前可用图中")
            left, right = nodes[request.subject], nodes[request.object]
            if left["kind"] != right["kind"] or left["id"] == right["id"]:
                raise HTTPException(422, "只可关联同类的不同来源实体")
            if all(node["origin"] == "business_record" for node in (left, right)):
                raise HTTPException(422, "不能把两个独立业务记录直接合并")
            case_ids = set()
            source_ids = []
            for node in (left, right):
                if node["origin"] == "reviewed_document":
                    extraction = session.get(GraphExtraction, node["id"].split(":")[1])
                    chunk = session.get(EvidenceChunk, extraction.chunk_id)
                    source_ids.append("document:" + chunk.document_id)
                    if extraction.case_id:
                        case_ids.add(extraction.case_id)
                else:
                    source_ids.append("business:" + node["id"])
            if source_ids[0] == source_ids[1]:
                raise HTTPException(422, "同一文档内部或同文档重复抽取不能作为跨来源身份关联")
            if len(case_ids) > 1:
                raise HTTPException(422, "跨案件私有实体需要另行核验，本单元不支持")
            case_id = next(iter(case_ids), None)
            subject, object_id = sorted((request.subject, request.object))
            identifier = str(uuid5(NAMESPACE_URL, subject + "|" + object_id + "|" + str(case_id)))
            existing = session.get(GraphEntityLink, identifier)
            if existing:
                return {"id": existing.id, "created": False, "note": existing.note}
            session.add(
                GraphEntityLink(
                    id=identifier,
                    subject=subject,
                    object=object_id,
                    case_id=case_id,
                    reviewed_by=who.user_id,
                    note=request.note,
                )
            )
            return {"id": identifier, "created": True, "note": request.note}

    @app.post("/api/graph/paths")
    def paths(request: PathRequest, who: Actor = Depends(actor)):
        started = time.perf_counter()
        expected_revision = None
        if request.case_id:
            with sessions() as session:
                expected_revision = visible_case(session, request.case_id, who).revision
        graph = graph_reader(as_of=request.as_of, case_id=request.case_id, who=who)
        traversal_started = time.perf_counter()
        result = find_paths(graph, request)
        traversal_ms = (time.perf_counter() - traversal_started) * 1000
        chunk_ids = {
            edge["provenance"]["chunk_id"]
            for path in result["paths"]
            for edge in path["edges"]
            if "chunk_id" in edge["provenance"]
        }
        chunk_ids.update(
            node["provenance"]["chunk_id"]
            for path in result["paths"]
            for node in path["nodes"]
            if "chunk_id" in node.get("provenance", {})
        )
        with sessions() as session:
            if request.case_id:
                visible_case(session, request.case_id, who)
            chunks = list(
                session.scalars(
                    eligible_query(request.as_of, request.case_id).where(
                        EvidenceChunk.id.in_(chunk_ids)
                    )
                )
            )
        if {chunk.id for chunk in chunks} != chunk_ids:
            raise HTTPException(409, "路径来源已变化，请重新检索")
        revalidate_started = time.perf_counter()
        latest = graph_reader(as_of=request.as_of, case_id=request.case_id, who=who)
        if request.case_id:
            with sessions() as session:
                if visible_case(session, request.case_id, who).revision != expected_revision:
                    raise HTTPException(409, "案件版本已变化，请重新检索")
        if graph_signature(latest) != graph_signature(graph):
            raise HTTPException(409, "路径图快照已变化，请重新检索")
        revalidate_ms = (time.perf_counter() - revalidate_started) * 1000
        return {
            **result,
            "evidence": [result_hit(chunk, 0, "graph_path") for chunk in chunks],
            "timing_ms": {
                "graph_load": graph["elapsed_ms"],
                "traversal": traversal_ms,
                "revalidate": revalidate_ms,
                "total": (time.perf_counter() - started) * 1000,
            },
            "notice": "路径是取证关系，不能代替风险匹配、合规审批或处置",
        }

    return paths
