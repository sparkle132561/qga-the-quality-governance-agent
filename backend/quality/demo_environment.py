"""专用Fake演示库归属校验；不能把其他工作区/库当可重置数据。"""

from quality.config import ROOT


def verify_marker(connection, workspace=ROOT):
    if connection.execute("SELECT current_database()").fetchone()[0] != "quality_demo":
        raise RuntimeError("拒绝操作非quality_demo数据库")
    if not connection.execute("SELECT to_regclass('public.codex_demo_environment')").fetchone()[0]:
        raise RuntimeError("已有专用库缺少归属标记，拒绝覆盖")
    row = connection.execute(
        "SELECT workspace_root,mode FROM codex_demo_environment WHERE singleton=1"
    ).fetchone()
    if not row or row[0].casefold() != str(workspace.resolve()).casefold() or row[1] != "fake_only":
        raise RuntimeError("专用库不属于当前工作区，拒绝操作")
