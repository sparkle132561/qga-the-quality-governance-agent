import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from quality.config import Settings
from quality.main import create_app
from quality.models import Base
from sqlalchemy import create_engine


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        jwt_secret="test-only-" * 6,
        upload_dir=tmp_path / "uploads",
        agnes_api_key="",
        siliconflow_api_key="",
        model_mode="fake",
    )
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    app = create_app(settings, InMemorySaver())
    with TestClient(app) as test_client:
        yield test_client


def headers(client, username="merchant-a"):
    response = client.post("/api/auth/login", json={"username": username, "password": "demo-only"})
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["access_token"]}
