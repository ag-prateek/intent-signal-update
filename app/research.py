from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import socket
from email.utils import parsedate_to_datetime
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from html import unescape
from ipaddress import ip_address
from urllib.parse import quote, urljoin, urlparse
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree as ET

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base
from app.models import Account
from app.schemas import SignalCreate
from app.services import create_signal

router = APIRouter(prefix="/api/research", tags=["external research"])
DATABASE = None
SETTINGS = None


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class ExternalSource(Base):
    __tablename__ = "external_research_sources"
    __table_args__ = (UniqueConstraint("account_id", "source_type", "url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    source_type: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str | None] = mapped_column(String(2000))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ResearchRun(Base):
    __tablename__ = "external_research_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(30), default="running", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    sources_checked: Mapped[int] = mapped_column(Integer, default=0)
    documents_found: Mapped[int] = mapped_column(Integer, default=0)
    signals_created: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list] = mapped_column(JSON, default=list)


class ResearchDocument(Base):
    __tablename__ = "external_research_documents"
    __table_args__ = (UniqueConstraint("fingerprint"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("external_research_runs.id", ondelete="CASCADE"), index=True
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    source_type: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[str] = mapped_column(String(2000))
    text: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    fingerprint: Mapped[str] = mapped_column(String(64))
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SourceCreate(BaseModel):
    account_id: int
    source_type: str = Field(
        pattern="^(company_site|rss|greenhouse|lever|ashby|gdelt|tavily|firecrawl)$"
    )
    name: str
    url: HttpUrl | None = None
    config: dict = Field(default_factory=dict)
    enabled: bool = True


class RunRequest(BaseModel):
    account_id: int | None = None
    source_ids: list[int] | None = None


@dataclass
class Document:
    title: str
    url: str
    text: str
    published_at: datetime | None = None
    raw: dict | None = None


PATTERNS = [
    (
        "funding",
        re.compile(r"\b(raised|funding|series [a-z]|investment|venture round)\b", re.I),
        0.90,
    ),
    (
        "executive_hire",
        re.compile(
            r"\b(appointed|named|joins? as|new (chief|ceo|cfo|coo|cro|cpo|cto|cio|vp))\b",
            re.I,
        ),
        0.86,
    ),
    (
        "expansion",
        re.compile(
            r"\b(expand|expansion|new office|new facility|new location|entering the)\b",
            re.I,
        ),
        0.82,
    ),
    (
        "acquisition",
        re.compile(r"\b(acquir(?:e|ed|es|ing)|merger|acquisition)\b", re.I),
        0.90,
    ),
    (
        "product_launch",
        re.compile(
            r"\b(launch(?:es|ed)?|introduc(?:es|ed)|new product|general availability)\b",
            re.I,
        ),
        0.76,
    ),
    (
        "technology_change",
        re.compile(
            r"\b(migrat(?:e|ed|ing)|implements?|adopts?|transformation|modernization|new platform)\b",
            re.I,
        ),
        0.72,
    ),
    (
        "compliance_event",
        re.compile(r"\b(compliance|regulatory|audit|breach|security incident)\b", re.I),
        0.78,
    ),
    (
        "partnership",
        re.compile(r"\b(partner(?:ship|ed|s)?|alliance|integration with)\b", re.I),
        0.68,
    ),
]


def strip_html(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", unescape(value)).strip()


def parse_date(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    parsers = (
        lambda item: datetime.fromisoformat(item.replace("Z", "+00:00")),
        parsedate_to_datetime,
    )
    for parser in parsers:
        try:
            parsed = parser(text)
        except (ValueError, TypeError, IndexError, AttributeError):
            continue
        if parsed.tzinfo is None:
            return parsed
        return parsed.astimezone(UTC).replace(tzinfo=None)
    return None


def normalize_content(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def document_fingerprint(account_id: int, document: Document) -> str:
    content = normalize_content(f"{document.title} {document.text}")
    return hashlib.sha256(
        f"{account_id}|{document.url}|{document.title}|{content}".encode()
    ).hexdigest()


def references_account(account: Account, *parts: str) -> bool:
    haystack = normalize_content(" ".join(part for part in parts if part))
    if not haystack:
        return False

    account_name = normalize_content(account.name)
    if account_name and re.search(rf"(?<![a-z0-9]){re.escape(account_name)}(?![a-z0-9])", haystack):
        return True

    domain = normalize_content(account.domain or "")
    if domain and domain in haystack:
        return True

    label = domain.split(".", 1)[0]
    if len(label) >= 4 and re.search(rf"(?<![a-z0-9]){re.escape(label)}(?![a-z0-9])", haystack):
        return True

    return False


def ensure_public_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only public HTTP(S) URLs are allowed")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        raise ValueError("Hostname could not be resolved") from exc
    for address in addresses:
        ip = ip_address(address[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError("Private or reserved network targets are blocked")
    return url


async def fetch(
    client: httpx.AsyncClient,
    url: str,
    *,
    robots: bool = False,
) -> httpx.Response:
    current_url = ensure_public_url(url)
    user_agent = getattr(SETTINGS, "user_agent", "IntentSignalResearchBot/1.0")
    if robots:
        parsed = urlparse(current_url)
        robot_url = ensure_public_url(f"{parsed.scheme}://{parsed.netloc}/robots.txt")
        response = await client.get(
            robot_url,
            headers={"user-agent": user_agent},
            follow_redirects=False,
        )
        if response.status_code == 404:
            robot_lines = []
        else:
            response.raise_for_status()
            robot_lines = response.text.splitlines()
        parser = RobotFileParser()
        parser.parse(robot_lines)
        if not parser.can_fetch(user_agent, current_url):
            raise PermissionError("robots.txt disallows this URL")

    redirect_limit = int(getattr(SETTINGS, "max_redirects", 5))
    for _ in range(redirect_limit + 1):
        response = await client.get(
            current_url,
            headers={"user-agent": user_agent},
            follow_redirects=False,
        )
        if response.status_code not in {301, 302, 303, 307, 308}:
            response.raise_for_status()
            return response
        location = response.headers.get("location")
        if not location:
            response.raise_for_status()
            return response
        current_url = ensure_public_url(urljoin(current_url, location))
    raise httpx.TooManyRedirects("Redirect limit exceeded")


async def collect(
    source: ExternalSource,
    account: Account,
    client: httpx.AsyncClient,
) -> list[Document]:
    limit = int(getattr(SETTINGS, "max_documents_per_source", 50))
    kind = source.source_type

    if kind == "company_site":
        documents = []
        errors = []
        for path in source.config.get("paths", ["/news", "/press", "/blog", "/careers"]):
            url = urljoin(source.url or f"https://{account.domain}", path)
            try:
                response = await fetch(client, url, robots=True)
            except Exception as exc:
                errors.append(f"{url}: {exc}")
                continue
            title = re.search(r"(?is)<title[^>]*>(.*?)</title>", response.text)
            documents.append(
                Document(
                    strip_html(title.group(1)) if title else url,
                    url,
                    strip_html(response.text)[:30000],
                )
            )
        if not documents and errors:
            raise RuntimeError("; ".join(errors)[:500])
        return documents

    if kind == "rss":
        response = await fetch(client, source.url or "")
        root = ET.fromstring(response.content)
        documents = []
        for item in root.findall(".//item")[:limit]:
            title = item.findtext("title") or "Untitled"
            documents.append(
                Document(
                    title,
                    item.findtext("link") or source.url or "",
                    strip_html((item.findtext("description") or "") + " " + title),
                    parse_date(item.findtext("pubDate")),
                )
            )
        atom_entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")
        remaining = limit - len(documents)
        for entry in atom_entries[: max(remaining, 0)]:
            title = entry.findtext("{http://www.w3.org/2005/Atom}title") or "Untitled"
            link = source.url or ""
            for candidate in entry.findall("{http://www.w3.org/2005/Atom}link"):
                rel = candidate.attrib.get("rel", "alternate")
                href = candidate.attrib.get("href")
                if href and rel == "alternate":
                    link = href
                    break
                if href:
                    link = href
            summary = (
                entry.findtext("{http://www.w3.org/2005/Atom}summary")
                or entry.findtext("{http://www.w3.org/2005/Atom}content")
                or ""
            )
            published = entry.findtext("{http://www.w3.org/2005/Atom}published") or entry.findtext(
                "{http://www.w3.org/2005/Atom}updated"
            )
            documents.append(
                Document(title, link, strip_html(f"{summary} {title}"), parse_date(published))
            )
        return documents

    if kind == "greenhouse":
        token = source.config.get("board_token") or (source.url or "").rstrip("/").split("/")[-1]
        url = f"https://boards-api.greenhouse.io/v1/boards/{quote(token)}/jobs?content=true"
        jobs = (await fetch(client, url)).json().get("jobs", [])
        return [
            Document(
                job.get("title", "Job"),
                job.get("absolute_url", url),
                strip_html((job.get("content") or "") + " " + job.get("title", "")),
                parse_date(job.get("updated_at")),
                job,
            )
            for job in jobs[:limit]
        ]

    if kind == "lever":
        site = source.config.get("site") or (source.url or "").rstrip("/").split("/")[-1]
        url = f"https://api.lever.co/v0/postings/{quote(site)}?mode=json"
        jobs = (await fetch(client, url)).json()
        return [
            Document(
                job.get("text", "Job"),
                job.get("hostedUrl", url),
                strip_html(json.dumps(job)),
                None,
                job,
            )
            for job in jobs[:limit]
        ]

    if kind == "ashby":
        board = source.config.get("board") or (source.url or "").rstrip("/").split("/")[-1]
        url = f"https://api.ashbyhq.com/posting-api/job-board/{quote(board)}"
        jobs = (await fetch(client, url)).json().get("jobs", [])
        return [
            Document(
                job.get("title", "Job"),
                job.get("jobUrl", url),
                strip_html((job.get("descriptionPlain") or "") + " " + job.get("title", "")),
                parse_date(job.get("publishedAt")),
                job,
            )
            for job in jobs[:limit]
        ]

    if kind == "gdelt":
        query = source.config.get("query") or f'"{account.name}"'
        url = (
            "https://api.gdeltproject.org/api/v2/doc/doc"
            f"?query={quote(query)}&mode=ArtList&format=json&maxrecords={limit}&sort=HybridRel"
        )
        articles = (await fetch(client, url)).json().get("articles", [])
        return [
            Document(
                article.get("title", "News"),
                article.get("url", url),
                strip_html(article.get("title") or ""),
                parse_date(article.get("seendate")),
                article,
            )
            for article in articles[:limit]
            if references_account(account, article.get("title", ""), article.get("url", ""))
        ]

    if kind == "tavily":
        api_key = os.getenv("TAVILY_API_KEY")
        if not api_key:
            raise ValueError("TAVILY_API_KEY is not configured")
        query = source.config.get("query") or (
            f'"{account.name}" funding hiring expansion acquisition technology'
        )
        response = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "search_depth": "advanced",
                "max_results": limit,
            },
        )
        response.raise_for_status()
        return [
            Document(
                result.get("title", "Web result"),
                result.get("url", ""),
                result.get("content", ""),
                None,
                result,
            )
            for result in response.json().get("results", [])[:limit]
            if references_account(
                account, result.get("title", ""), result.get("url", ""), result.get("content", "")
            )
        ]

    api_key = os.getenv("FIRECRAWL_API_KEY")
    if not api_key:
        raise ValueError("FIRECRAWL_API_KEY is not configured")
    query = source.config.get("query") or (f'"{account.name}" company news jobs funding expansion')
    response = await client.post(
        "https://api.firecrawl.dev/v1/search",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"query": query, "limit": limit},
    )
    response.raise_for_status()
    return [
        Document(
            result.get("title", "Web result"),
            result.get("url", ""),
            result.get("description") or result.get("markdown") or "",
            None,
            result,
        )
        for result in response.json().get("data", [])[:limit]
        if references_account(
            account,
            result.get("title", ""),
            result.get("url", ""),
            result.get("description") or result.get("markdown") or "",
        )
    ]


def extract(
    account: Account,
    document: Document,
    source_type: str,
) -> list[SignalCreate]:
    text = f"{document.title} {document.text}"[:40000]
    event_date = document.published_at or utcnow()
    candidates = []

    if source_type in {"greenhouse", "lever", "ashby"}:
        candidates.append(
            SignalCreate(
                account_id=account.id,
                signal_type="job_posting",
                title=document.title,
                summary=document.text[:500],
                source_name=source_type,
                source_url=document.url,
                event_date=event_date,
                confidence=0.9,
                source_reliability=0.9,
                raw_payload=document.raw or {},
            )
        )

    for signal_type, pattern, confidence in PATTERNS:
        if pattern.search(text):
            candidates.append(
                SignalCreate(
                    account_id=account.id,
                    signal_type=signal_type,
                    title=document.title,
                    summary=document.text[:500],
                    source_name=source_type,
                    source_url=document.url,
                    event_date=event_date,
                    confidence=confidence,
                    source_reliability=(
                        0.85
                        if source_type in {"company_site", "greenhouse", "lever", "ashby"}
                        else 0.75
                    ),
                    raw_payload=document.raw or {},
                )
            )
    return candidates[:3]


def session_dep():
    with DATABASE.session_factory() as session:
        yield session


async def execute(
    session: Session,
    account_id: int | None = None,
    source_ids: list[int] | None = None,
) -> ResearchRun:
    run = ResearchRun(account_id=account_id)
    session.add(run)
    session.commit()
    session.refresh(run)

    query = select(ExternalSource).where(ExternalSource.enabled.is_(True))
    if account_id is not None:
        query = query.where(ExternalSource.account_id == account_id)
    if source_ids:
        query = query.where(ExternalSource.id.in_(source_ids))

    errors = []
    timeout = float(getattr(SETTINGS, "request_timeout_seconds", 15.0))
    async with httpx.AsyncClient(timeout=timeout) as client:
        for source in session.scalars(query):
            run.sources_checked += 1
            account = session.get(Account, source.account_id)
            try:
                documents = await collect(source, account, client)
                for document in documents:
                    if not document.url:
                        continue
                    if source.source_type in {
                        "gdelt",
                        "tavily",
                        "firecrawl",
                    } and not references_account(
                        account, document.title, document.url, document.text
                    ):
                        continue
                    fingerprint = document_fingerprint(account.id, document)
                    existing = session.scalar(
                        select(ResearchDocument).where(ResearchDocument.fingerprint == fingerprint)
                    )
                    if existing:
                        continue
                    session.add(
                        ResearchDocument(
                            run_id=run.id,
                            account_id=account.id,
                            source_type=source.source_type,
                            title=document.title,
                            url=document.url,
                            text=document.text,
                            published_at=document.published_at,
                            fingerprint=fingerprint,
                            raw_payload=document.raw or {},
                        )
                    )
                    session.commit()
                    run.documents_found += 1
                    for candidate in extract(account, document, source.source_type):
                        _, created = create_signal(session, candidate)
                        run.signals_created += int(created)
                source.last_run_at = utcnow()
                session.add(source)
                session.commit()
            except Exception as exc:
                errors.append({"source_id": source.id, "error": str(exc)[:500]})

    run.errors = errors
    run.status = "completed_with_errors" if errors else "completed"
    run.completed_at = utcnow()
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


@router.post("/sources", status_code=201)
def add_source(payload: SourceCreate, session: Session = Depends(session_dep)):
    if not session.get(Account, payload.account_id):
        raise HTTPException(status_code=404, detail="Account not found")
    configured_without_url = {
        "greenhouse": "board_token",
        "lever": "site",
        "ashby": "board",
    }
    url_required = {"company_site", "rss"}
    missing_url = payload.source_type in url_required and not payload.url
    missing_url_or_config = (
        payload.source_type in configured_without_url
        and not payload.url
        and not payload.config.get(configured_without_url[payload.source_type])
    )
    if payload.enabled and (missing_url or missing_url_or_config):
        raise HTTPException(status_code=422, detail="URL or provider configuration is required")
    duplicate = session.scalar(
        select(ExternalSource).where(
            ExternalSource.account_id == payload.account_id,
            ExternalSource.source_type == payload.source_type,
            ExternalSource.url == (str(payload.url) if payload.url else None),
        )
    )
    if duplicate:
        raise HTTPException(status_code=409, detail="Source already exists or is invalid")
    source = ExternalSource(
        account_id=payload.account_id,
        source_type=payload.source_type,
        name=payload.name,
        url=str(payload.url) if payload.url else None,
        config=payload.config,
        enabled=payload.enabled,
    )
    session.add(source)
    try:
        session.commit()
    except Exception as exc:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Source already exists or is invalid",
        ) from exc
    session.refresh(source)
    return serialize_source(source)


@router.get("/sources")
def list_sources(session: Session = Depends(session_dep)):
    sources = session.scalars(select(ExternalSource).order_by(ExternalSource.id.desc()))
    return [serialize_source(source) for source in sources]


@router.post("/run")
async def run_research(
    payload: RunRequest,
    session: Session = Depends(session_dep),
):
    return serialize_run(await execute(session, payload.account_id, payload.source_ids))


@router.get("/runs")
def list_runs(session: Session = Depends(session_dep)):
    runs = session.scalars(select(ResearchRun).order_by(ResearchRun.id.desc()).limit(50))
    return [serialize_run(run) for run in runs]


@router.get("/documents")
def list_documents(
    account_id: int | None = None,
    session: Session = Depends(session_dep),
):
    query = select(ResearchDocument).order_by(ResearchDocument.id.desc()).limit(200)
    if account_id is not None:
        query = query.where(ResearchDocument.account_id == account_id)
    return [
        {
            "id": document.id,
            "account_id": document.account_id,
            "source_type": document.source_type,
            "title": document.title,
            "url": document.url,
            "published_at": (document.published_at.isoformat() if document.published_at else None),
        }
        for document in session.scalars(query)
    ]


@router.get("/status")
def status(session: Session = Depends(session_dep)):
    return {
        "scheduler_enabled": bool(getattr(SETTINGS, "scheduler_enabled", False)),
        "source_count": session.scalar(select(func.count(ExternalSource.id))) or 0,
        "run_count": session.scalar(select(func.count(ResearchRun.id))) or 0,
        "document_count": session.scalar(select(func.count(ResearchDocument.id))) or 0,
        "providers": {
            "gdelt": True,
            "tavily": bool(os.getenv("TAVILY_API_KEY")),
            "firecrawl": bool(os.getenv("FIRECRAWL_API_KEY")),
        },
    }


def serialize_source(source: ExternalSource):
    return {
        "id": source.id,
        "account_id": source.account_id,
        "source_type": source.source_type,
        "name": source.name,
        "url": source.url,
        "config": source.config,
        "enabled": source.enabled,
        "last_run_at": source.last_run_at.isoformat() if source.last_run_at else None,
    }


def serialize_run(run: ResearchRun):
    return {
        "id": run.id,
        "account_id": run.account_id,
        "status": run.status,
        "sources_checked": run.sources_checked,
        "documents_found": run.documents_found,
        "signals_created": run.signals_created,
        "errors": run.errors,
        "started_at": run.started_at.isoformat(),
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    }


async def scheduler_loop():
    interval = max(
        60,
        int(getattr(SETTINGS, "scheduler_interval_minutes", 60)) * 60,
    )
    while True:
        await asyncio.sleep(interval)
        with DATABASE.session_factory() as session:
            await execute(session)


def install(app, database, settings):
    global DATABASE, SETTINGS
    DATABASE = database
    SETTINGS = settings
    app.include_router(router)
    original = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(application):
        async with original(application):
            database.create_all()
            task = (
                asyncio.create_task(scheduler_loop())
                if getattr(settings, "scheduler_enabled", False)
                else None
            )
            try:
                yield
            finally:
                if task:
                    task.cancel()

    app.router.lifespan_context = combined_lifespan
