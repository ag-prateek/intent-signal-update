from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import app
from app.scoring import score_account, score_signal


def test_health_and_demo(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    app.state.database = database
    database.create_all()
    with TestClient(app) as client:
        assert client.get("/api/health").json() == {"status": "ok"}
        result = client.post("/api/ingest/demo", json={})
        assert result.status_code == 200
        assert result.json()["created"] == 4
        dashboard = client.get("/api/dashboard").json()
        assert dashboard["total_accounts"] == 3
        assert dashboard["active_signals"] == 4


def test_scoring_decay_and_reinforcement():
    now = datetime.now(UTC).replace(tzinfo=None)
    recent, _ = score_signal(signal_type="funding", event_date=now - timedelta(days=2), confidence=.95, source_reliability=.95, evidence_kind="direct", now=now)
    stale, _ = score_signal(signal_type="funding", event_date=now - timedelta(days=240), confidence=.7, source_reliability=.7, evidence_kind="inferred", now=now)
    assert recent > stale
    single, _ = score_account([72], ["funding"], .9)
    combined, band = score_account([72, 60], ["funding", "executive_hire"], .9)
    assert combined > single
    assert band in {"warm", "hot"}
