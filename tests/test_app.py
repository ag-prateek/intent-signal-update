from __future__ import annotations

import warnings
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import main
from app.db import Database
from app.scoring import score_account, score_signal


@pytest.fixture
def client(tmp_path, monkeypatch):
    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main.settings, "api_key", None)
    main.app.state.database = database

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with TestClient(main.app) as test_client:
            yield test_client

    database.engine.dispose()
    lifecycle_warnings = [
        warning
        for warning in caught
        if "on_event is deprecated" in str(warning.message)
    ]
    assert lifecycle_warnings == []


def test_lifespan_initializes_database_and_core_flow(client):
    assert main.app.router.on_startup == []
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/").status_code == 200

    seeded = client.post("/api/ingest/demo")
    assert seeded.status_code == 200
    assert seeded.json() == {"created": 4, "duplicates": 0}

    repeated = client.post("/api/ingest/demo")
    assert repeated.json() == {"created": 0, "duplicates": 4}

    dashboard = client.get("/api/dashboard")
    assert dashboard.status_code == 200
    payload = dashboard.json()
    assert payload["total_accounts"] == 3
    assert payload["active_signals"] == 4

    account_id = payload["accounts"][0]["id"]
    brief = client.get(f"/api/accounts/{account_id}/brief")
    assert brief.status_code == 200
    assert brief.json()["top_signals"]

    draft = client.post(
        f"/api/accounts/{account_id}/outreach",
        json={"use_ai": False, "persona": "COO"},
    )
    assert draft.status_code == 201
    assert draft.json()["generated_by"] == "template"

    signals = client.get("/api/signals").json()
    assert len(signals) == 4
    assert client.post(f"/api/signals/{signals[0]['id']}/rescore").status_code == 200


def test_signal_endpoint_rejects_duplicates(client):
    event_date = datetime.now(UTC).isoformat()
    payload = {
        "account_name": "Acme",
        "account_domain": "acme.example",
        "signal_type": "funding",
        "title": "Acme raised a Series A",
        "source_name": "Company newsroom",
        "source_url": "https://example.com/acme-series-a",
        "event_date": event_date,
        "confidence": 0.95,
        "source_reliability": 0.95,
    }
    assert client.post("/api/signals", json=payload).status_code == 201
    duplicate = client.post("/api/signals", json=payload)
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["message"] == "Duplicate signal"


def test_csv_ingestion_reports_errors_and_duplicates(client):
    csv_body = "\n".join(
        [
            "account_name,account_domain,signal_type,title,event_date,confidence,source_reliability",
            "Atlas,atlas.example,expansion,Atlas opened a new office,2026-07-17T12:00:00Z,0.9,0.9",
            "Broken,broken.example,funding,,not-a-date,0.9,0.9",
        ]
    )
    first = client.post(
        "/api/ingest/csv",
        files={"file": ("signals.csv", csv_body, "text/csv")},
    )
    assert first.status_code == 200
    assert first.json()["created"] == 1
    assert len(first.json()["errors"]) == 1

    second = client.post(
        "/api/ingest/csv",
        files={"file": ("signals.csv", csv_body, "text/csv")},
    )
    assert second.json()["duplicates"] == 1


def test_api_key_guard(client, monkeypatch):
    monkeypatch.setattr(main.settings, "api_key", "secret")
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/dashboard").status_code == 401
    assert client.get(
        "/api/dashboard",
        headers={"x-api-key": "secret"},
    ).status_code == 200


def test_scoring_decay_and_reinforcement():
    now = datetime.now(UTC).replace(tzinfo=None)
    recent, _ = score_signal(
        signal_type="funding",
        event_date=now - timedelta(days=2),
        confidence=0.95,
        source_reliability=0.95,
        evidence_kind="direct",
        now=now,
    )
    stale, _ = score_signal(
        signal_type="funding",
        event_date=now - timedelta(days=240),
        confidence=0.7,
        source_reliability=0.7,
        evidence_kind="inferred",
        now=now,
    )
    assert recent > stale

    single, _ = score_account([72], ["funding"], 0.9)
    combined, band = score_account(
        [72, 60],
        ["funding", "executive_hire"],
        0.9,
    )
    assert combined > single
    assert band in {"warm", "hot"}
