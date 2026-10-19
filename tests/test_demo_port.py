import importlib.util
from pathlib import Path

import pytest


def module():
    path = Path(__file__).resolve().parents[1] / "scripts/prepare-demo.py"
    spec = importlib.util.spec_from_file_location("prepare_demo_port", path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


class ExistingDatabase:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, *args):
        return self

    def fetchone(self):
        return (1,)


def test_custom_port_reaches_both_connections_and_migration_with_keys_scrubbed(monkeypatch):
    loaded = module()
    connections, migrations, verified = [], [], []
    def connect(**kwargs):
        connections.append(kwargs)
        return ExistingDatabase()
    monkeypatch.setattr(loaded.psycopg, "connect", connect)
    monkeypatch.setattr(loaded, "verify_marker", lambda connection: verified.append(connection))
    monkeypatch.setattr(loaded.subprocess, "run", lambda *args, **kwargs: migrations.append(kwargs))
    loaded.prepare(postgres_port=56543)
    assert [row["port"] for row in connections] == [56543, 56543]
    assert [row["dbname"] for row in connections] == ["postgres", "quality_demo"]
    assert len(verified) == 1
    env = migrations[0]["env"]
    assert env["DATABASE_URL"].endswith(":56543/quality_demo")
    assert env["AGNES_API_KEY"] == "" and env["SILICONFLOW_API_KEY"] == ""


def test_marker_rejection_still_blocks_migration(monkeypatch):
    loaded = module()
    migrations = []
    monkeypatch.setattr(loaded.psycopg, "connect", lambda **kwargs: ExistingDatabase())
    def deny(connection):
        raise RuntimeError("wrong workspace")
    monkeypatch.setattr(loaded, "verify_marker", deny)
    monkeypatch.setattr(loaded.subprocess, "run", lambda *args, **kwargs: migrations.append(kwargs))
    with pytest.raises(RuntimeError, match="wrong workspace"):
        loaded.prepare(postgres_port=56543)
    assert migrations == []


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_invalid_port_is_rejected_before_connect(monkeypatch, port):
    loaded = module()
    def forbidden(**kwargs):
        raise AssertionError("must validate before connecting")
    monkeypatch.setattr(loaded.psycopg, "connect", forbidden)
    with pytest.raises(ValueError):
        loaded.prepare(postgres_port=port)
