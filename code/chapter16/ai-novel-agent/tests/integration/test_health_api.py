from fastapi.testclient import TestClient

from novel_agent.main import create_app


def test_liveness_returns_ok() -> None:
    client = TestClient(create_app())

    response = client.get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_liveness_openapi_uses_health_tag() -> None:
    schema = create_app().openapi()

    operation = schema["paths"]["/api/v1/health/live"]["get"]

    assert operation["tags"] == ["health"]
