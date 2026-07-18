from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app import main
from app import research
from app.db import Database


@pytest.fixture
def client(tmp_path, monkeypatch):
    database = Database(f"sqlite:///{tmp_path / 'research.db'}")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(research, "DATABASE", database)
    monkeypatch.setattr(main.settings, "api_key", None)
    main.app.state.database = database

    with TestClient(main.app) as test_client:
        yield test_client

    database.engine.dispose()


def test_research_source_run_provenance_and_deduplication(client, monkeypatch):
    account = client.post(
        "/api/accounts",
        json={"name": "Acme", "domain": "acme.example"},
    )
    assert account.status_code == 201
    account_id = account.json()["id"]

    source = client.post(
        "/api/research/sources",
        json={
            "account_id": account_id,
            "source_type": "rss",
            "name": "Acme newsroom",
            "url": "https://acme.example/news.xml",
        },
    )
    assert source.status_code == 201
    source_id = source.json()["id"]

    async def fake_collect(source, account, http_client):
        del source, account, http_client
        return [
            research.Document(
                title="Acme raised a Series B to open a new facility",
                url="https://acme.example/news/series-b",
                text="Acme raised new funding and announced an expansion.",
                published_at=datetime.now(UTC).replace(tzinfo=None),
            )
        ]

    monkeypatch.setattr(research, "collect", fake_collect)

    first = client.post(
        "/api/research/run",
        json={"account_id": account_id, "source_ids": [source_id]},
    )
    assert first.status_code == 200
    assert first.json()["status"] == "completed"
    assert first.json()["sources_checked"] == 1
    assert first.json()["documents_found"] == 1
    assert first.json()["signals_created"] >= 1

    documents = client.get(
        "/api/research/documents",
        params={"account_id": account_id},
    )
    assert documents.status_code == 200
    assert len(documents.json()) == 1
    assert documents.json()[0]["url"].endswith("series-b")

    signals = client.get("/api/signals")
    assert signals.status_code == 200
    assert {item["signal_type"] for item in signals.json()} & {"funding", "expansion"}

    second = client.post(
        "/api/research/run",
        json={"account_id": account_id, "source_ids": [source_id]},
    )
    assert second.status_code == 200
    assert second.json()["documents_found"] == 0
    assert second.json()["signals_created"] == 0

    status = client.get("/api/research/status")
    assert status.status_code == 200
    assert status.json()["source_count"] == 1
    assert status.json()["run_count"] == 2
    assert status.json()["document_count"] == 1


def test_private_network_targets_are_blocked():
    with pytest.raises(ValueError, match="Private or reserved"):
        research.ensure_public_url("http://127.0.0.1/internal")
