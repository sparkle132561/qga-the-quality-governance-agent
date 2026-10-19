"""两个业务工作流共用的有界专业取证Runtime；业务判断由各自协调器负责。"""

import time
from dataclasses import dataclass
from datetime import date
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from quality.agent_core import Tool, ToolExecutor
from quality.case_summary import build_case_summary
from quality.domain import ToolResult
from quality.models import ReviewCase
from quality.providers import AgnesClient
from quality.retrieval import keyword_search
from quality.retrieval_agent import (
    EmptyArguments,
    EvidenceSearch,
    FinalAdvice,
    build_retrieval_agent,
)

BRANCHES = ("rules", "product", "material", "history")


@dataclass(frozen=True)
class WorkflowProfile:
    name: str
    tasks: dict[str, str]
    final_type: type[FinalAdvice]
    required_facts: tuple[str, ...]


class WorkflowState(TypedDict, total=False):
    case_id: str
    revision: int
    actor: dict
    run_id: str
    facts: dict
    source_hits: dict
    checks: dict
    rules_result: dict
    product_result: dict
    material_result: dict
    history_result: dict
    summary: dict
    approval_decision: dict
    reference_memory: dict
    case_summary: dict


def build_specialist_graph(settings, checkpointer, sessions, profile, coordinate, snapshot_fn=None):
    builder = StateGraph(WorkflowState)

    def branch(name):
        def execute(state):
            started = time.perf_counter()
            if name == "history" and not state["source_hits"][name]:
                output = {
                    "status": "no_history",
                    "engine_mode": "deterministic",
                    "evidence": [],
                    "message": "近50条同商家分析记录中未发现本任务相关历史；不表示全局没有风险",
                    "trace": [],
                }
            elif any(not state["facts"].get(field) for field in profile.required_facts):
                output = {
                    "status": "needs_info",
                    "engine_mode": "not_called",
                    "evidence": [],
                    "message": "请补充此流程必需字段后继续取证",
                    "trace": [],
                }
            else:

                def read(arguments, acting):
                    hits = state["source_hits"][name]
                    return ToolResult(
                        status="ok" if hits else "not_found",
                        data={"hits": hits},
                        evidence_refs=[hit["id"] for hit in hits],
                    )

                tools = [
                    Tool(
                        "read_branch_evidence",
                        "读取本专业分支的固定来源快照",
                        EmptyArguments,
                        frozenset({name}),
                        read,
                    )
                ]
                if name == "rules":

                    def search(arguments, acting):
                        with sessions() as session:
                            allowed = {
                                hit["location"]["rule_id"]
                                for hit in state["source_hits"]["rules"]
                                if "rule_id" in hit["location"]
                            }
                            found = keyword_search(
                                session,
                                arguments.query,
                                date.fromisoformat(state["facts"]["as_of"]),
                                arguments.limit,
                                None,
                            )
                            hits = [hit for hit in found if hit["rule_id"] in allowed]
                            return ToolResult(
                                status="ok" if hits else "not_found",
                                data={"hits": hits},
                                evidence_refs=[hit["id"] for hit in hits],
                            )

                    tools.append(
                        Tool(
                            "search_rule_evidence",
                            "检索日期内准入规则原文缓存，保留来源和页码",
                            EvidenceSearch,
                            frozenset({"rules"}),
                            search,
                        )
                    )
                executor = ToolExecutor(tools)
                model = AgnesClient(
                    settings.agnes_api_key, settings.agnes_base_url, settings.agnes_model
                )

                def control():
                    with sessions() as session:
                        case = session.get(ReviewCase, state["case_id"])
                        active = case.active_run if case else None
                        if not active or active.get("run_id") != state["run_id"]:
                            return "run_superseded"
                        if active.get("cancel_requested"):
                            return "analysis_cancelled"
                        if state.get("case_summary") and snapshot_fn:
                            current = build_case_summary(session, case, snapshot_fn(session, case))
                            if current != state["case_summary"]:
                                return "snapshot_changed"
                        return None

                graph = build_retrieval_agent(
                    model, executor, checkpointer, profile.final_type, control
                )
                thread_id = (
                    f"{state['case_id']}:{state['revision']}:{profile.name}:"
                    f"{state['run_id']}:{name}"
                )
                result = graph.invoke(
                    {
                        "case_id": state["case_id"],
                        "task": profile.tasks[name],
                        "facts": {**state["facts"], "specialty": name},
                        "reference_memory": state.get("reference_memory", {"items": []}),
                        "case_summary": state.get("case_summary"),
                        "actor": state["actor"],
                        "branch": name,
                        "messages": [],
                        "trace": [],
                        "evidence": {},
                        "fingerprints": [],
                        "rounds": 0,
                        "tool_count": 0,
                        "max_rounds": 3,
                        "max_tools": 4,
                        "context_chars": 24000,
                        "started": started,
                        "budget_seconds": 90,
                        "status": "running",
                    },
                    {"configurable": {"thread_id": thread_id}},
                )
                advice = result.get("final", {})
                output = {
                    "status": "failed"
                    if result["status"] == "failed"
                    else "ready"
                    if result["status"] == "completed"
                    and advice.get("recommendation") == "review_ready"
                    else "needs_info",
                    "engine_mode": result.get("engine_mode", "not_called"),
                    "message": advice.get("explanation", "专业分支未完成模型取证"),
                    "error_code": result.get("error_code"),
                    "evidence": list(result["evidence"].values()),
                    "missing_fields": advice.get("missing_fields", []),
                    "advice": advice,
                    "trace": result["trace"],
                    "thread_id": thread_id,
                }
            output.update(started=started, finished=time.perf_counter())
            return {f"{name}_result": output}

        def run(state):
            started = time.perf_counter()
            try:
                return execute(state)
            except Exception:
                # 隔离分支运行失败，保留其余取证；不能推断模型是否已被调用。
                return {
                    f"{name}_result": {
                        "status": "failed",
                        "engine_mode": "unknown",
                        "evidence": [],
                        "message": "本分支运行失败，需重试或人工处理",
                        "error_code": "branch_execution_failed",
                        "trace": [{"event": "branch_error", "code": "branch_execution_failed"}],
                        "started": started,
                        "finished": time.perf_counter(),
                    }
                }

        return run

    for name in BRANCHES:
        builder.add_node(name, branch(name))
        builder.add_edge(START, name)

    builder.add_node("coordinate", coordinate)
    builder.add_edge(list(BRANCHES), "coordinate")

    def human_gate(state):
        if state["summary"]["recommendation"] not in {"review_ready", "reject_proposed"}:
            return {}
        decision = interrupt(
            {
                "case_id": state["case_id"],
                "revision": state["revision"],
                "run_id": state["run_id"],
                "recommendation": state["summary"]["recommendation"],
                "notice": "等待审核员决定；模型不得批准或执行处置",
            }
        )
        if (
            not isinstance(decision, dict)
            or decision.get("case_id") != state["case_id"]
            or decision.get("revision") != state["revision"]
            or decision.get("run_id") != state["run_id"]
            or decision.get("actor", {}).get("role") != "reviewer"
            or decision.get("decision") not in {"approve", "reject", "request_info"}
        ):
            raise ValueError("审批恢复信封与当前运行或权限不符")
        return {"approval_decision": decision}

    builder.add_node("human_gate", human_gate)
    builder.add_edge("coordinate", "human_gate")
    builder.add_edge("human_gate", END)
    return builder.compile(checkpointer=checkpointer)
