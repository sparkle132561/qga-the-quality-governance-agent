from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from quality.domain import Actor, Role
from quality.models import AssistantRunControl, AuditEvent, ReviewCase


def register_assistant_controls(app, sessions, actor, visible_case):
    def visible(session, case_id, identifier, who, lock=False):
        visible_case(session, case_id, who)
        query = select(AssistantRunControl).where(
            AssistantRunControl.id == identifier,
            AssistantRunControl.case_id == case_id,
        )
        row = session.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise HTTPException(404, "助手请求不存在")
        return row

    def output(row):
        return {
            "id": row.id,
            "revision": row.revision,
            "thread_id": row.thread_id,
            "status": row.status,
            "cancel_requested": row.cancel_requested,
        }

    @app.get("/api/cases/{case_id}/assistant-requests/{identifier}")
    def status(case_id: str, identifier: str, who: Actor = Depends(actor)):
        with sessions() as session:
            return output(visible(session, case_id, identifier, who))

    def operator(row, who):
        if row.actor_id != who.user_id and who.role != Role.REVIEWER:
            raise HTTPException(403, "只能管理自己发起的助手请求")

    @app.post("/api/cases/{case_id}/assistant-requests/{identifier}/cancel", status_code=202)
    def cancel(case_id: str, identifier: str, who: Actor = Depends(actor)):
        with sessions.begin() as session:
            row = visible(session, case_id, identifier, who, lock=True)
            operator(row, who)
            if row.status != "running":
                raise HTTPException(409, "助手请求已结束")
            if not row.cancel_requested:
                row.cancel_requested = True
                session.add(
                    AuditEvent(
                        case_id=case_id,
                        actor_id=who.user_id,
                        action="assistant_cancel_requested",
                        details={"run_id": row.id},
                    )
                )
            return output(row)

    @app.post("/api/cases/{case_id}/assistant-requests/{identifier}/recover")
    def recover(case_id: str, identifier: str, who: Actor = Depends(actor)):
        with sessions.begin() as session:
            row = visible(session, case_id, identifier, who, lock=True)
            operator(row, who)
            if row.status != "running" or row.worker_id == app.state.worker_id:
                raise HTTPException(409, "没有旧进程遗留的助手请求")
            row.status = "cancelled" if row.cancel_requested else "interrupted"
            session.add(
                AuditEvent(
                    case_id=case_id,
                    actor_id=who.user_id,
                    action="assistant_recovered",
                    details={"run_id": row.id, "status": row.status},
                )
            )
            return output(row)

    def begin(case_id, who, identifier, revision, thread_id):
        try:
            with sessions.begin() as session:
                visible_case(session, case_id, who, lock=True)
                if session.get(AssistantRunControl, identifier):
                    raise HTTPException(409, "此助手请求标识已使用，请创建新请求")
                session.add(
                    AssistantRunControl(
                        id=identifier,
                        case_id=case_id,
                        actor_id=who.user_id,
                        revision=revision,
                        thread_id=thread_id,
                        worker_id=app.state.worker_id,
                    )
                )
                session.add(
                    AuditEvent(
                        case_id=case_id,
                        actor_id=who.user_id,
                        action="assistant_started",
                        details={"run_id": identifier},
                    )
                )
        except IntegrityError:
            raise HTTPException(409, "助手请求标识重复或来源已变化") from None

    def checker(identifier):
        def check():
            with sessions() as session:
                row = session.get(AssistantRunControl, identifier)
                if row is None or row.status != "running":
                    return "run_superseded"
                if row.cancel_requested:
                    return "analysis_cancelled"
                case = session.get(ReviewCase, row.case_id)
                return "snapshot_changed" if case.revision != row.revision else None

        return check

    def finish(identifier, result_status):
        with sessions.begin() as session:
            row = session.scalar(
                select(AssistantRunControl)
                .where(
                    AssistantRunControl.id == identifier,
                )
                .with_for_update()
            )
            if row.status == "running":
                row.status = "cancelled" if row.cancel_requested else result_status
                session.add(
                    AuditEvent(
                        case_id=row.case_id,
                        actor_id=row.actor_id,
                        action="assistant_finished",
                        details={"run_id": row.id, "status": row.status},
                    )
                )
            return row.status

    return begin, checker, finish
