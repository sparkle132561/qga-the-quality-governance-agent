import time
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy import select

from quality.agent_core import CaseLookup, Tool, ToolExecutor
from quality.approvals import register_approval_routes
from quality.auth import authenticate, login
from quality.case_summary import build_case_summary, register_summary_routes
from quality.catalog import register_catalog_routes
from quality.config import Settings
from quality.db import create_database, seed_merchants
from quality.domain import (
    Actor,
    AnalysisCancel,
    AnalysisOutput,
    CaseCreate,
    CaseRead,
    CaseUpdate,
    LoginRequest,
    Role,
    ToolResult,
)
from quality.evaluation import register_evaluation_snapshot
from quality.graph import build_graph
from quality.graph_paths import register_path_routes
from quality.impact import build_impact_graph, impact_snapshot
from quality.incidents import register_incident_routes
from quality.intents import register_intent_routes
from quality.knowledge_graph import register_graph_routes
from quality.materials import register_material_routes
from quality.memory import register_memory_routes, select_memories
from quality.models import AuditEvent, Merchant, ReviewCase, WorkOrder
from quality.qualification import build_qualification_graph, qualification_snapshot
from quality.remediation import (
    build_remediation_graph,
    register_work_order_routes,
    remediation_snapshot,
)
from quality.retrieval import register_retrieval_routes
from quality.retrieval_agent import register_agent_routes
from quality.task_routing import register_routing_routes
from quality.vision import register_vision_routes


def create_app(settings: Settings | None = None, checkpointer=None) -> FastAPI:
    settings = settings or Settings()
    engine, sessions = create_database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app):
        # 迁移由命令行完成，应用启动不隐式修改业务表结构。
        seed_merchants(sessions)
        if checkpointer is None:
            conn_string = settings.database_url.replace("postgresql+psycopg://", "postgresql://")
            with ConnectionPool(
                conn_string,
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            ) as pool:
                saver = PostgresSaver(pool)
                saver.setup()
                app.state.checkpointer = saver
                app.state.graph = build_graph(saver)
                app.state.qualification_graph = build_qualification_graph(settings, saver, sessions)
                app.state.impact_graph = build_impact_graph(settings, saver, sessions)
                app.state.remediation_graph = build_remediation_graph(settings, saver, sessions)
                yield
        else:
            app.state.checkpointer = checkpointer
            app.state.graph = build_graph(checkpointer)
            app.state.qualification_graph = build_qualification_graph(
                settings, checkpointer, sessions
            )
            app.state.impact_graph = build_impact_graph(settings, checkpointer, sessions)
            app.state.remediation_graph = build_remediation_graph(settings, checkpointer, sessions)
            yield
        engine.dispose()

    app = FastAPI(title="商品质量治理 Agent", lifespan=lifespan)
    app.state.sessions = sessions
    app.state.settings = settings
    app.state.worker_id = str(uuid4())
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["Authorization", "Content-Type"],
    )
    bearer = HTTPBearer(auto_error=False)

    def actor(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Actor:
        if credentials is None:
            raise HTTPException(401, "请先登录")
        return authenticate(credentials.credentials, settings)

    def visible_case(session, case_id: str, who: Actor, lock=False):
        query = select(ReviewCase).where(ReviewCase.id == case_id)
        if who.role == Role.MERCHANT:
            query = query.where(ReviewCase.merchant_id == who.merchant_id)
        if lock:
            query = query.with_for_update()
        found = session.scalar(query)
        if found is None:
            raise HTTPException(404, "案件不存在")
        return found

    def workflow_snapshot(session, found, kind):
        if kind == "qualification":
            return qualification_snapshot(session, found)
        if kind == "impact":
            return impact_snapshot(session, found)
        if kind == "remediation":
            return remediation_snapshot(session, found)
        return {}

    def workflow_graph(kind):
        return {
            "qualification": app.state.qualification_graph,
            "impact": app.state.impact_graph,
            "stub": app.state.graph,
            "remediation": app.state.remediation_graph,
        }[kind]

    def memory_source_snapshots(session, found, kind):
        current = workflow_snapshot(session, found, kind)
        state = workflow_graph(kind).get_state(
            {
                "configurable": {
                    "thread_id": (found.latest_analysis or {}).get("thread_id", "missing"),
                }
            }
        )
        if any(key not in state.values for key in ("facts", "source_hits", "checks")):
            raise HTTPException(409, "记忆来源checkpoint缺失，需重新核验")
        original = {key: state.values[key] for key in ("facts", "source_hits", "checks")}
        return current, original

    app.state.workflow_snapshot = workflow_snapshot
    app.state.memory_source_snapshots = memory_source_snapshots

    def read_case_tool(arguments: CaseLookup, who: Actor):
        with sessions() as session:
            try:
                found = visible_case(session, arguments.case_id, who)
            except HTTPException:
                return ToolResult(status="not_found", error="案件不存在或无权访问")
            return ToolResult(
                status="ok", data=CaseRead.model_validate(found).model_dump(mode="json")
            )

    app.state.tools = ToolExecutor(
        [
            Tool(
                "read_case",
                "读取有权限的案件字段",
                CaseLookup,
                frozenset({"rules", "product", "material"}),
                read_case_tool,
            )
        ]
    )

    @app.get("/api/health")
    def health():
        return {"status": "ok", "model_mode": settings.model_mode, "is_demo": True}

    @app.post("/api/auth/login")
    def sign_in(request: LoginRequest):
        token, who = login(request.username, request.password, settings)
        return {"access_token": token, "token_type": "bearer", "actor": who}

    @app.get("/api/auth/me")
    def me(who: Actor = Depends(actor)):
        return who

    @app.get("/api/merchants")
    def merchants(who: Actor = Depends(actor)):
        with sessions() as session:
            query = select(Merchant)
            if who.role == Role.MERCHANT:
                query = query.where(Merchant.id == who.merchant_id)
            return [{"id": item.id, "name": item.name} for item in session.scalars(query)]

    @app.post("/api/cases", response_model=CaseRead, status_code=201)
    def create_case(request: CaseCreate, who: Actor = Depends(actor)):
        owner = who.merchant_id if who.role == Role.MERCHANT else request.merchant_id
        if owner is None:
            raise HTTPException(422, "请选择所属商家")
        if who.role == Role.MERCHANT and request.merchant_id not in (None, owner):
            raise HTTPException(403, "不能为其他商家创建案件")
        with sessions.begin() as session:
            if session.get(Merchant, owner) is None:
                raise HTTPException(422, "商家不存在")
            found = ReviewCase(
                title=request.title.strip(),
                merchant_id=owner,
                workflow=request.workflow.value,
                payload=request.payload,
            )
            if not found.title:
                raise HTTPException(422, "案件标题不能为空")
            session.add(found)
            session.flush()
            session.add(
                AuditEvent(
                    case_id=found.id,
                    actor_id=who.user_id,
                    action="create",
                    details={"revision": found.revision},
                )
            )
            result = CaseRead.model_validate(found)
        return result

    @app.get("/api/cases", response_model=list[CaseRead])
    def list_cases(
        who: Actor = Depends(actor),
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
        merchant_id: str | None = Query(None, max_length=80),
    ):
        with sessions() as session:
            query = select(ReviewCase).order_by(ReviewCase.created_at.desc())
            if who.role == Role.MERCHANT:
                if merchant_id not in (None, who.merchant_id):
                    raise HTTPException(403, "不能查询其他商家的案件")
                query = query.where(ReviewCase.merchant_id == who.merchant_id)
            elif merchant_id:
                query = query.where(ReviewCase.merchant_id == merchant_id)
            return [
                CaseRead.model_validate(item)
                for item in session.scalars(query.limit(limit).offset(offset))
            ]

    @app.get("/api/cases/{case_id}", response_model=CaseRead)
    def get_case(case_id: str, who: Actor = Depends(actor)):
        with sessions() as session:
            return CaseRead.model_validate(visible_case(session, case_id, who))

    @app.post("/api/cases/{case_id}/submit", response_model=CaseRead)
    def submit(case_id: str, who: Actor = Depends(actor)):
        if who.role == Role.ADMIN:
            raise HTTPException(403, "管理员不能代替业务审核操作")
        with sessions.begin() as session:
            found = visible_case(session, case_id, who, lock=True)
            if found.status not in ("draft", "needs_info"):
                raise HTTPException(409, "当前状态不能提交")
            found.status = "submitted"
            if found.workflow == "remediation" and found.payload.get("work_order_id"):
                order = session.scalar(
                    select(WorkOrder)
                    .where(
                        WorkOrder.id == found.payload["work_order_id"],
                        WorkOrder.merchant_id == found.merchant_id,
                    )
                    .with_for_update()
                )
                if (
                    order is None
                    or order.active_review_case_id != found.id
                    or order.revision != found.payload.get("work_order_revision")
                    or order.status == "closed"
                ):
                    raise HTTPException(409, "工单复核关系已变化")
                order.status = "waiting_review"
            session.add(
                AuditEvent(
                    case_id=case_id,
                    actor_id=who.user_id,
                    action="submit",
                    details={"revision": found.revision},
                )
            )
            return CaseRead.model_validate(found)

    @app.put("/api/cases/{case_id}/payload", response_model=CaseRead)
    def update_payload(case_id: str, request: CaseUpdate, who: Actor = Depends(actor)):
        if who.role == Role.ADMIN:
            raise HTTPException(403, "管理员不能代替业务资料编辑")
        with sessions.begin() as session:
            found = visible_case(session, case_id, who, lock=True)
            if (
                found.status not in ("draft", "needs_info")
                or found.revision != request.expected_revision
            ):
                raise HTTPException(409, "资料版本或状态已变化，请刷新后编辑")
            if found.workflow == "remediation" and found.payload.get("work_order_id"):
                for key in ("work_order_id", "work_order_revision"):
                    if request.payload.get(key) != found.payload[key]:
                        raise HTTPException(409, "工单绑定由服务器维护，不能编辑")
            found.payload = request.payload
            found.revision += 1
            found.latest_analysis = None
            session.add(
                AuditEvent(
                    case_id=case_id,
                    actor_id=who.user_id,
                    action="update_payload",
                    details={"revision": found.revision},
                )
            )
            return CaseRead.model_validate(found)

    @app.post("/api/cases/{case_id}/analyze", response_model=AnalysisOutput)
    def analyze(case_id: str, who: Actor = Depends(actor)):
        if who.role != Role.REVIEWER:
            raise HTTPException(403, "只有审核员可以运行分析")
        run_id = str(uuid4())
        with sessions.begin() as session:
            found = visible_case(session, case_id, who, lock=True)
            if found.status != "submitted":
                raise HTTPException(409, "案件必须先提交且不能重复启动分析")
            revision = found.revision
            graph_kind = {
                "qualification": "qualification",
                "incident": "impact",
                "remediation": "remediation",
            }.get(found.workflow, "stub")
            if found.workflow == "remediation" and not found.payload.get("work_order_id"):
                graph_kind = "stub"  # 兼容早期无工单的骨架验收记录。
            snapshot_input = workflow_snapshot(session, found, graph_kind)
            memory_input = select_memories(
                session, found.merchant_id, snapshot_input, memory_source_snapshots
            )
            thread_id = f"{case_id}:{revision}:{run_id}"
            found.status = "analyzing"
            found.latest_analysis = None
            found.active_run = {
                "run_id": run_id,
                "thread_id": thread_id,
                "worker_id": app.state.worker_id,
                "started_at": time.time(),
                "revision": revision,
                "graph_kind": graph_kind,
            }
            summary_input = build_case_summary(session, found, snapshot_input)
            session.add(
                AuditEvent(
                    case_id=case_id,
                    actor_id=who.user_id,
                    action="analysis_started",
                    details={"run_id": run_id},
                )
            )
        started = time.perf_counter()
        try:
            run_graph = workflow_graph(graph_kind)
            state = run_graph.invoke(
                {
                    "case_id": case_id,
                    "revision": revision,
                    "reference_memory": memory_input,
                    "case_summary": summary_input,
                    "actor": who.model_dump(mode="json"),
                    "run_id": run_id,
                    **snapshot_input,
                },
                {"configurable": {"thread_id": thread_id}},
            )
            output = AnalysisOutput(
                run_id=run_id,
                thread_id=thread_id,
                case_id=case_id,
                revision=revision,
                engine_mode=state["summary"]["engine_mode"],
                branches=state["summary"]["branches"],
                recommendation=state["summary"]["recommendation"],
                elapsed_ms=(time.perf_counter() - started) * 1000,
                missing_fields=state["summary"].get("missing_fields", []),
                conflicts=state["summary"].get("conflicts", []),
                impact=state["summary"].get("impact"),
            )
            with sessions.begin() as session:
                found = visible_case(session, case_id, who, lock=True)
                if (
                    found.revision != revision
                    or found.status != "analyzing"
                    or (found.active_run or {}).get("run_id") != run_id
                ):
                    raise HTTPException(409, "案件版本或状态已变化，分析不能写回")
                if (found.active_run or {}).get("cancel_requested"):
                    raise HTTPException(
                        409,
                        {
                            "code": "analysis_cancelled",
                            "run_id": run_id,
                            "message": "取消已生效，分析未写回",
                        },
                    )
                if (
                    graph_kind != "stub"
                    and workflow_snapshot(session, found, graph_kind) != snapshot_input
                ):
                    raise HTTPException(409, "取证来源已变化，请重新分析")
                if build_case_summary(session, found, snapshot_input) != summary_input:
                    raise HTTPException(409, "案件摘要的事实或处置记录已变化，请重新分析")
                if (
                    select_memories(
                        session, found.merchant_id, snapshot_input, memory_source_snapshots
                    )
                    != memory_input
                ):
                    raise HTTPException(409, "引用记忆已变化，请重新分析")
                found.latest_analysis = output.model_dump(mode="json")
                found.status = (
                    "waiting_review"
                    if output.recommendation in {"review_ready", "reject_proposed"}
                    else "needs_info"
                )
                found.active_run = None
                session.add(
                    AuditEvent(
                        case_id=case_id,
                        actor_id=who.user_id,
                        action="analyze",
                        details={"run_id": run_id, "engine_mode": output.engine_mode},
                    )
                )
        except HTTPException as failure:
            with sessions.begin() as session:
                found = visible_case(session, case_id, who, lock=True)
                if (found.active_run or {}).get("run_id") == run_id:
                    found.status = "submitted"
                    found.active_run = None
                    session.add(
                        AuditEvent(
                            case_id=case_id,
                            actor_id=who.user_id,
                            action="analysis_cancelled"
                            if isinstance(failure.detail, dict)
                            and failure.detail.get("code") == "analysis_cancelled"
                            else "analysis_invalidated",
                            details={"run_id": run_id},
                        )
                    )
            raise
        except Exception:
            with sessions.begin() as session:
                found = visible_case(session, case_id, who, lock=True)
                if (found.active_run or {}).get("run_id") == run_id:
                    found.status = "submitted"
                    found.active_run = None
                    session.add(
                        AuditEvent(
                            case_id=case_id,
                            actor_id=who.user_id,
                            action="analysis_failed",
                            details={"run_id": run_id},
                        )
                    )
            raise HTTPException(503, "分析失败，案件已恢复为可重试状态") from None
        return output

    @app.get("/api/cases/{case_id}/analysis-status")
    def analysis_status(case_id: str, who: Actor = Depends(actor)):
        with sessions() as session:
            found = visible_case(session, case_id, who)
            active = found.active_run or {}
            return {
                "case_status": found.status,
                "revision": found.revision,
                "run_id": active.get("run_id"),
                "cancel_requested": bool(active.get("cancel_requested")),
            }

    @app.post("/api/cases/{case_id}/cancel-analysis", status_code=202)
    def cancel_analysis(case_id: str, request: AnalysisCancel, who: Actor = Depends(actor)):
        if who.role != Role.REVIEWER:
            raise HTTPException(403, "只有审核员可以取消专业取证运行")
        with sessions.begin() as session:
            found = visible_case(session, case_id, who, lock=True)
            active = found.active_run or {}
            if (
                found.status != "analyzing"
                or found.revision != request.expected_revision
                or active.get("run_id") != request.analysis_run_id
            ):
                raise HTTPException(409, "当前运行版本已变化")
            repeated = bool(active.get("cancel_requested"))
            if not repeated:
                found.active_run = {**active, "cancel_requested": True}
                session.add(
                    AuditEvent(
                        case_id=case_id,
                        actor_id=who.user_id,
                        action="analysis_cancel_requested",
                        details={"run_id": request.analysis_run_id, "note": request.note},
                    )
                )
            return {
                "run_id": request.analysis_run_id,
                "cancel_requested": True,
                "idempotent": repeated,
            }

    @app.post("/api/cases/{case_id}/recover", response_model=CaseRead)
    def recover(case_id: str, who: Actor = Depends(actor)):
        if who.role != Role.REVIEWER:
            raise HTTPException(403, "只有审核员可以恢复中断分析")
        with sessions.begin() as session:
            found = visible_case(session, case_id, who, lock=True)
            active = found.active_run
            if found.status != "analyzing" or not active:
                raise HTTPException(409, "没有可恢复的分析")
            # 首版本地单进程：只允许恢复旧进程，禁止撤销本进程仍在执行的调用。
            if active["worker_id"] == app.state.worker_id:
                raise HTTPException(409, "当前进程仍负责此分析，请等待结果")
            kind = active.get("graph_kind", "stub")
            run_graph = workflow_graph(kind)
            snapshot = run_graph.get_state({"configurable": {"thread_id": active["thread_id"]}})
            if (
                not active.get("cancel_requested")
                and snapshot.values.get("summary")
                and (not snapshot.next or snapshot.next == ("human_gate",))
                and snapshot.values.get("revision") == found.revision
                and (
                    kind == "stub"
                    or workflow_snapshot(session, found, kind)
                    == {key: snapshot.values[key] for key in ("facts", "source_hits", "checks")}
                )
                and (
                    "reference_memory" not in snapshot.values
                    or snapshot.values["reference_memory"]
                    == select_memories(
                        session,
                        found.merchant_id,
                        {key: snapshot.values[key] for key in ("facts", "source_hits", "checks")}
                        if kind != "stub"
                        else {},
                        memory_source_snapshots,
                    )
                )
                and (
                    not snapshot.values.get("case_summary")
                    or snapshot.values["case_summary"] == build_case_summary(
                        session, found,
                        {key: snapshot.values[key] for key in ("facts", "source_hits", "checks")}
                        if kind != "stub" else {},
                    )
                )
            ):
                found.latest_analysis = AnalysisOutput(
                    run_id=active["run_id"],
                    thread_id=active["thread_id"],
                    case_id=case_id,
                    revision=found.revision,
                    engine_mode=snapshot.values["summary"]["engine_mode"],
                    branches=snapshot.values["summary"]["branches"],
                    recommendation=snapshot.values["summary"]["recommendation"],
                    elapsed_ms=None,
                    missing_fields=snapshot.values["summary"].get("missing_fields", []),
                    conflicts=snapshot.values["summary"].get("conflicts", []),
                    impact=snapshot.values["summary"].get("impact"),
                ).model_dump(mode="json")
                found.status = (
                    "waiting_review"
                    if snapshot.values["summary"]["recommendation"]
                    in {"review_ready", "reject_proposed"}
                    else "needs_info"
                )
            else:
                found.status = "submitted"
                found.latest_analysis = None
            found.active_run = None
            session.add(
                AuditEvent(
                    case_id=case_id,
                    actor_id=who.user_id,
                    action="recover",
                    details={"run_id": active["run_id"], "timing_unavailable": True},
                )
            )
            return CaseRead.model_validate(found)

    @app.get("/api/cases/{case_id}/audit")
    def audit(case_id: str, who: Actor = Depends(actor)):
        with sessions() as session:
            visible_case(session, case_id, who)
            return [
                {
                    "id": event.id,
                    "action": event.action,
                    "actor_id": event.actor_id,
                    "details": event.details,
                    "created_at": event.created_at.isoformat(),
                }
                for event in session.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.case_id == case_id)
                    .order_by(AuditEvent.created_at)
                )
            ]

    register_material_routes(app, settings, sessions, actor, visible_case)
    register_vision_routes(app, settings, sessions, actor, visible_case)
    register_work_order_routes(app, sessions, actor)
    register_memory_routes(app, sessions, actor, visible_case, memory_source_snapshots)
    register_catalog_routes(app, sessions, actor)
    register_evaluation_snapshot(app, sessions, actor)
    register_incident_routes(app, settings, sessions, actor)
    app.state.search_reader = register_retrieval_routes(
        app, settings, sessions, actor, visible_case
    )
    register_agent_routes(app, settings, sessions, actor, visible_case)
    register_intent_routes(app, settings, sessions, actor, visible_case)
    register_summary_routes(app, sessions, actor, visible_case)
    register_approval_routes(app, sessions, actor, visible_case, workflow_graph, workflow_snapshot)
    graph_reader = register_graph_routes(app, settings, sessions, actor, visible_case)
    app.state.graph_reader = graph_reader
    app.state.path_reader = register_path_routes(app, sessions, actor, visible_case, graph_reader)
    register_routing_routes(app, settings, sessions, actor, visible_case)
    return app


app = create_app()
