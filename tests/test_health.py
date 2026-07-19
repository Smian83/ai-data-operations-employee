"""Tests for the GET /health endpoint."""
from fastapi.testclient import TestClient


def test_health_returns_200(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200


def test_health_returns_exact_payload(client: TestClient) -> None:
    response = client.get("/health")
    assert response.json() == {"status": "healthy"}
