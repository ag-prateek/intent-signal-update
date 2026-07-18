from __future__ import annotations

import asyncio
import socket
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main, research
from app.db import Database


@pytest.fixture
def client(tmp_path, monkeypatch):
    database = Database(f"sqlite:///{tmp_path / 'thorough.db'}")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(research, "DATABASE", database)
    monkeypatch.setattr(research, "SETTINGS", main.settings)
    monkeypatch.setattr(main.settings, "api_key", None)
    main.app.state.database = database

    with TestClient(main.app) as test_client:
        yield test_client

    database.engine.dispose()


def create_account(client, name="Acme", domain="acme.example"):
    response = client.post("/api/accounts", json={"name": name, "domain": domain})
    assert response.status_code == 201
    return response.json()["id"]


def add_source(client, account_id, source_type, name, url=None, config=None, enabled=True):
    payload = {
        "account_id": account_id,
        "source_type": source_type,
        "name": name,
        "config": config or {},
        "enabled": enabled,
    }
    if url is not None:
        payload["url"] = url
    return client.post("/api/research/sources", json=payload)


def test_source_validation_duplicate_handling_and_account_checks(client):
    missing_account = add_source(
        client,
        999999,
        "rss",
        "Missing account feed",
        "https://example.com/feed.xml",
    )
    assert missing_account.status_code == 404

    account_id = create_account(client)
    invalid_type = add_source(
        client,
        account_id,
        "linkedin_scraper",
        "Unsupported source",
        "https://example.com",
    )
    assert invalid_type.status_code == 422

    first = add_source(
        client,
        account_id,
        "rss",
        "News feed",
        "https://example.com/feed.xml",
    )
    assert first.status_code == 201
    duplicate = add_source(
        client,
        account_id,
        "rss",
        "Same feed",
        "https://example.com/feed.xml",
    )
    assert duplicate.status_code == 409


def test_sources_that_require_urls_reject_missing_urls(client):
    account_id = create_account(client)
    response = add_source(client, account_id, "rss", "URL-less RSS")
    assert response.status_code == 422


def test_null_url_provider_sources_are_deduplicated(client):
    account_id = create_account(client)
    first = add_source(
        client,
        account_id,
        "tavily",
        "Tavily search",
        config={"query": "Acme funding"},
    )
    assert first.status_code == 201
    duplicate = add_source(
        client,
        account_id,
        "tavily",
        "Duplicate Tavily search",
        config={"query": "Acme funding"},
    )
    assert duplicate.status_code == 409


def test_run_isolates_failures_skips_disabled_sources_and_records_errors(
    client,
    monkeypatch,
):
    account_id = create_account(client)
    ok_source = add_source(
        client,
        account_id,
        "rss",
        "Working source",
        "https://example.com/ok.xml",
    ).json()
    add_source(
        client,
        account_id,
        "rss",
        "Failing source",
        "https://example.com/fail.xml",
    )
    add_source(
        client,
        account_id,
        "rss",
        "Disabled source",
        "https://example.com/disabled.xml",
        enabled=False,
    )

    async def fake_collect(source, account, http_client):
        del account, http_client
        if source.name == "Failing source":
            raise RuntimeError("upstream unavailable")
        return [
            research.Document(
                title="Acme raised a Series C",
                url=f"https://example.com/{source.id}",
                text="Acme raised funding for expansion.",
            )
        ]

    monkeypatch.setattr(research, "collect", fake_collect)
    response = client.post("/api/research/run", json={"account_id": account_id})
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed_with_errors"
    assert payload["sources_checked"] == 2
    assert payload["documents_found"] == 1
    assert payload["signals_created"] >= 1
    assert payload["errors"] == [
        {"source_id": ok_source["id"] + 1, "error": "upstream unavailable"}
    ]


def test_rss_supports_standard_rfc822_publication_dates(monkeypatch):
    xml = b"""<?xml version='1.0'?>
    <rss><channel><item>
      <title>Acme raised a Series B</title>
      <link>https://example.com/acme-series-b</link>
      <description>Funding for expansion</description>
      <pubDate>Wed, 02 Oct 2002 13:00:00 GMT</pubDate>
    </item></channel></rss>"""

    class Response:
        content = xml

    async def fake_fetch(client, url, robots=False):
        del client, url, robots
        return Response()

    monkeypatch.setattr(research, "fetch", fake_fetch)
    source = SimpleNamespace(source_type="rss", url="https://example.com/feed.xml", config={})
    account = SimpleNamespace(name="Acme", domain="acme.example")
    documents = asyncio.run(research.collect(source, account, None))
    assert len(documents) == 1
    assert documents[0].published_at == datetime(2002, 10, 2, 13, 0)


def test_atom_feeds_are_supported(monkeypatch):
    xml = b"""<?xml version='1.0'?>
    <feed xmlns='http://www.w3.org/2005/Atom'>
      <entry>
        <title>Acme opens a new facility</title>
        <link href='https://example.com/new-facility'/>
        <summary>Acme announced an expansion.</summary>
        <updated>2026-07-18T05:00:00Z</updated>
      </entry>
    </feed>"""

    class Response:
        content = xml

    async def fake_fetch(client, url, robots=False):
        del client, url, robots
        return Response()

    monkeypatch.setattr(research, "fetch", fake_fetch)
    source = SimpleNamespace(source_type="rss", url="https://example.com/atom.xml", config={})
    account = SimpleNamespace(name="Acme", domain="acme.example")
    documents = asyncio.run(research.collect(source, account, None))
    assert len(documents) == 1
    assert documents[0].url == "https://example.com/new-facility"
    assert documents[0].published_at == datetime(2026, 7, 18, 5, 0)


@pytest.mark.parametrize(
    ("source_type", "payload", "expected_title"),
    [
        (
            "greenhouse",
            {
                "jobs": [
                    {
                        "title": "Director of Talent Acquisition",
                        "absolute_url": "https://jobs.example.com/1",
                        "content": "Build a global recruiting team",
                        "updated_at": "2026-07-18T05:00:00Z",
                    }
                ]
            },
            "Director of Talent Acquisition",
        ),
        (
            "lever",
            [
                {
                    "text": "VP People",
                    "hostedUrl": "https://jobs.example.com/2",
                    "categories": {"team": "People"},
                }
            ],
            "VP People",
        ),
        (
            "ashby",
            {
                "jobs": [
                    {
                        "title": "HR Systems Lead",
                        "jobUrl": "https://jobs.example.com/3",
                        "descriptionPlain": "Implement a new HR platform",
                        "publishedAt": "2026-07-18T05:00:00Z",
                    }
                ]
            },
            "HR Systems Lead",
        ),
    ],
)
def test_job_board_adapters_map_jobs_to_documents(
    source_type,
    payload,
    expected_title,
    monkeypatch,
):
    class Response:
        def json(self):
            return payload

    async def fake_fetch(client, url, robots=False):
        del client, url, robots
        return Response()

    monkeypatch.setattr(research, "fetch", fake_fetch)
    source = SimpleNamespace(
        source_type=source_type,
        url=f"https://jobs.example.com/{source_type}/acme",
        config={},
    )
    account = SimpleNamespace(name="Acme", domain="acme.example")
    documents = asyncio.run(research.collect(source, account, None))
    assert len(documents) == 1
    assert documents[0].title == expected_title
    signals = research.extract(SimpleNamespace(id=1), documents[0], source_type)
    assert signals[0].signal_type == "job_posting"


def test_document_updates_at_same_url_are_detected(client, monkeypatch):
    account_id = create_account(client)
    source_id = add_source(
        client,
        account_id,
        "rss",
        "Update feed",
        "https://example.com/update.xml",
    ).json()["id"]
    calls = 0

    async def fake_collect(source, account, http_client):
        nonlocal calls
        del source, account, http_client
        calls += 1
        text = (
            "Acme raised a Series B."
            if calls == 1
            else "Acme acquired RivalCo in a material acquisition."
        )
        return [
            research.Document(
                title="Acme corporate update",
                url="https://example.com/corporate-update",
                text=text,
                published_at=datetime.now(UTC).replace(tzinfo=None)
                + timedelta(minutes=calls),
            )
        ]

    monkeypatch.setattr(research, "collect", fake_collect)
    first = client.post(
        "/api/research/run",
        json={"account_id": account_id, "source_ids": [source_id]},
    ).json()
    second = client.post(
        "/api/research/run",
        json={"account_id": account_id, "source_ids": [source_id]},
    ).json()
    assert first["documents_found"] == 1
    assert second["documents_found"] == 1
    assert second["signals_created"] >= 1


def test_unrelated_provider_results_do_not_create_account_signals(client, monkeypatch):
    account_id = create_account(client)
    source_id = add_source(
        client,
        account_id,
        "tavily",
        "Open web",
        config={"query": "Acme company news"},
    ).json()["id"]

    async def fake_collect(source, account, http_client):
        del source, account, http_client
        return [
            research.Document(
                title="OtherCo raised a Series D",
                url="https://other.example/funding",
                text="OtherCo raised funding to expand its operations.",
            )
        ]

    monkeypatch.setattr(research, "collect", fake_collect)
    run = client.post(
        "/api/research/run",
        json={"account_id": account_id, "source_ids": [source_id]},
    )
    assert run.status_code == 200
    assert run.json()["signals_created"] == 0


def test_redirects_to_private_networks_are_blocked(monkeypatch):
    def fake_getaddrinfo(host, port):
        del host, port
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(research.socket, "getaddrinfo", fake_getaddrinfo)

    def handler(request):
        if request.url.host == "public.example":
            return httpx.Response(
                302,
                headers={"location": "http://127.0.0.1/private"},
                request=request,
            )
        return httpx.Response(200, text="internal data", request=request)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            await research.fetch(http_client, "https://public.example/start")

    with pytest.raises(ValueError, match="Private or reserved"):
        asyncio.run(exercise())


def test_robots_disallow_is_visible_as_a_source_error(client, monkeypatch):
    account_id = create_account(client)
    source_id = add_source(
        client,
        account_id,
        "company_site",
        "Company site",
        "https://acme.example",
        config={"paths": ["/private"]},
    ).json()["id"]

    async def blocked_fetch(client, url, robots=False):
        del client, url, robots
        raise PermissionError("robots.txt disallows this URL")

    monkeypatch.setattr(research, "fetch", blocked_fetch)
    run = client.post(
        "/api/research/run",
        json={"account_id": account_id, "source_ids": [source_id]},
    )
    assert run.status_code == 200
    assert run.json()["status"] == "completed_with_errors"
    assert "robots.txt disallows" in run.json()["errors"][0]["error"]


def test_every_adapter_enforces_the_local_document_limit(monkeypatch):
    articles = [
        {"title": f"Acme update {index}", "url": f"https://example.com/{index}"}
        for index in range(5)
    ]

    class Response:
        def json(self):
            return {"articles": articles}

    async def fake_fetch(client, url, robots=False):
        del client, url, robots
        return Response()

    monkeypatch.setattr(research, "fetch", fake_fetch)
    monkeypatch.setattr(
        research,
        "SETTINGS",
        SimpleNamespace(max_documents_per_source=2),
    )
    source = SimpleNamespace(source_type="gdelt", url=None, config={})
    account = SimpleNamespace(name="Acme", domain="acme.example")
    documents = asyncio.run(research.collect(source, account, None))
    assert len(documents) == 2


def test_missing_provider_key_is_recorded_without_crashing_run(client, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    account_id = create_account(client)
    source_id = add_source(
        client,
        account_id,
        "tavily",
        "Tavily",
        config={"query": "Acme funding"},
    ).json()["id"]
    run = client.post(
        "/api/research/run",
        json={"account_id": account_id, "source_ids": [source_id]},
    )
    assert run.status_code == 200
    assert run.json()["status"] == "completed_with_errors"
    assert "TAVILY_API_KEY" in run.json()["errors"][0]["error"]
