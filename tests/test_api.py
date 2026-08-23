from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pagecapture import api
from pagecapture.config import settings
from pagecapture.db import Database


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    data = tmp_path / "data"
    output = tmp_path / "output"
    data.mkdir()
    output.mkdir()
    monkeypatch.setattr(settings, "data_dir", data)
    monkeypatch.setattr(settings, "output_root", output)
    monkeypatch.setattr(settings, "api_token", "")

    api.db = Database(data / "page-scraper.sqlite3")
    monkeypatch.setattr(api.worker, "db", api.db)

    async def _noop() -> None:
        return None

    monkeypatch.setattr(api.worker, "start", _noop)
    monkeypatch.setattr(api.worker, "stop", _noop)

    with TestClient(api.app) as test_client:
        yield test_client


def test_health(client: TestClient):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_create_job_rejects_bad_url(client: TestClient):
    resp = client.post(
        "/api/jobs",
        json={"urls": ["https://example.com/", "javascript:alert(1)"]},
    )
    assert resp.status_code == 400


def test_create_job_defaults_https(client: TestClient):
    resp = client.post("/api/jobs", json={"urls": ["example.com/a"]})
    assert resp.status_code == 201
    detail = client.get(f"/api/jobs/{resp.json()['id']}").json()
    assert detail["items"][0]["url"] == "https://example.com/a"


def test_create_job_and_list(client: TestClient):
    resp = client.post(
        "/api/jobs",
        json={"urls": ["https://example.com/", "https://example.org/"], "name": "two"},
    )
    assert resp.status_code == 201
    job = resp.json()
    assert job["name"] == "two"
    assert job["total"] == 2
    assert job["status"] == "queued"
    assert job["unsaved"] == 0

    listed = client.get("/api/jobs").json()
    assert listed["counts"]["queued"] == 1
    assert listed["jobs"][0]["id"] == job["id"]

    detail = client.get(f"/api/jobs/{job['id']}").json()
    assert [item["url"] for item in detail["items"]] == [
        "https://example.com/",
        "https://example.org/",
    ]


def test_rerun_clones_a_new_job(client: TestClient):
    created = client.post("/api/jobs", json={"urls": ["https://example.com/"]}).json()
    resp = client.post("/api/jobs/rerun", json={"ids": [created["id"]]})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["created"]) == 1
    assert body["created"][0] != created["id"]
    jobs = client.get("/api/jobs").json()["jobs"]
    assert len(jobs) == 2


def test_retry_without_failures_is_conflict(client: TestClient):
    created = client.post("/api/jobs", json={"urls": ["https://example.com/"]}).json()
    bulk = client.post("/api/jobs/retry", json={"ids": [created["id"]]})
    assert bulk.status_code == 200
    assert bulk.json()["skipped"][0]["reason"] == "nothing to retry"
    single = client.post(f"/api/jobs/{created['id']}/retry")
    assert single.status_code == 409


def test_delete_job(client: TestClient):
    created = client.post("/api/jobs", json={"urls": ["https://example.com/"]}).json()
    resp = client.delete(f"/api/jobs/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 1
    assert client.get(f"/api/jobs/{created['id']}").status_code == 404


def test_api_token_required(client: TestClient, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "secret")
    assert client.get("/api/jobs").status_code == 401
    ok = client.get("/api/jobs", headers={"Authorization": "Bearer secret"})
    assert ok.status_code == 200
