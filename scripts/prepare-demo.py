"""只创建/重置有本工作区归属标记的专用quality_demo；不操作quality/test库或上传文件。"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from quality.demo_environment import verify_marker  # noqa: E402
from quality.models import Base  # noqa: E402

CONNECT = {
    "host": "127.0.0.1",
    "port": 55432,
    "user": "quality",
    "password": "quality_demo",
    "connect_timeout": 5,
}


def prepare(reset=False, postgres_port=55432):
    if not 1 <= postgres_port <= 65535:
        raise ValueError("数据库端口不合法")
    connect = {**CONNECT, "port": postgres_port}
    demo_url = f"postgresql+psycopg://quality:quality_demo@127.0.0.1:{postgres_port}/quality_demo"
    with psycopg.connect(dbname="postgres", autocommit=True, **connect) as admin:
        exists = admin.execute("SELECT 1 FROM pg_database WHERE datname='quality_demo'").fetchone()
        if not exists:
            if reset:
                raise RuntimeError("请先启动专用演示库，不能重置不存在的库")
            admin.execute("CREATE DATABASE quality_demo OWNER quality")
    with psycopg.connect(dbname="quality_demo", **connect) as connection:
        if not exists:
            connection.execute(
                "CREATE TABLE codex_demo_environment "
                "(singleton integer PRIMARY KEY CHECK(singleton=1), "
                "workspace_root text NOT NULL, mode text NOT NULL)"
            )
            connection.execute(
                "INSERT INTO codex_demo_environment VALUES(1,%s,%s)",
                (str(ROOT.resolve()), "fake_only"),
            )
        verify_marker(connection)
    env = {**os.environ, "DATABASE_URL": demo_url, "AGNES_API_KEY": "", "SILICONFLOW_API_KEY": ""}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env, check=True
    )
    if reset:
        with psycopg.connect(dbname="quality_demo", **connect) as connection:
            verify_marker(connection)
            existing = {
                row[0]
                for row in connection.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public'"
                )
            }
            names = sorted(
                (
                    set(Base.metadata.tables)
                    | {"checkpoints", "checkpoint_blobs", "checkpoint_writes"}
                )
                & existing
            )
            if names:
                identifiers = sql.SQL(",").join(sql.Identifier("public", name) for name in names)
                connection.execute(
                    sql.SQL("TRUNCATE {} RESTART IDENTITY RESTRICT").format(identifiers)
                )
    print(
        json.dumps(
            {
                "database": "quality_demo",
                "reset": reset,
                "workspace_marker_verified": True,
                "uploads_deleted": False,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--confirm-demo-reset", action="store_true")
    parser.add_argument("--postgres-port", type=int, default=55432)
    args = parser.parse_args()
    if args.reset and not args.confirm_demo_reset:
        parser.error("重置需显式传--confirm-demo-reset，仅清空专用演示库记录")
    prepare(args.reset, args.postgres_port)
