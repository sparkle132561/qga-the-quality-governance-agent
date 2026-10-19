import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class GraphState(TypedDict, total=False):
    case_id: str
    revision: int
    rules_result: dict
    product_result: dict
    material_result: dict
    summary: dict


def build_graph(checkpointer):
    """单元2使用显式 stub 分支，只验证并行状态与恢复，不产出真实审核意见。"""
    builder = StateGraph(GraphState)

    def branch(name):
        def run(state):
            started = time.perf_counter()
            time.sleep(0.02)
            return {
                f"{name}_result": {
                    "status": "stub",
                    "case_id": state["case_id"],
                    "evidence": [],
                    "message": "取证尚未接入",
                    "started": started,
                    "finished": time.perf_counter(),
                }
            }

        return run

    for name in ("rules", "product", "material"):
        builder.add_node(name, branch(name))
        builder.add_edge(START, name)

    def coordinate(state):
        return {
            "summary": {
                "recommendation": "needs_info",
                "engine_mode": "stub",
                "branches": {
                    name: state[f"{name}_result"] for name in ("rules", "product", "material")
                },
            }
        }

    builder.add_node("coordinate", coordinate)
    builder.add_edge(["rules", "product", "material"], "coordinate")
    builder.add_edge("coordinate", END)
    return builder.compile(checkpointer=checkpointer)
