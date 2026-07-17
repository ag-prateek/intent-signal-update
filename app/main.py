from __future__ import annotations

import csv
import io
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import Database
from app.models import Account, Signal
from app.schemas import AccountCreate, OutreachRequest, SignalCreate
from app.services import (
    account_implications,
    account_personas,
    create_account,
    create_signal,
    recompute_signal,
    save_outreach,
    template_outreach,
)

settings = get_settings()
database = Database(settings.database_url)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Initialize application resources without deprecated startup events."""
    database.create_all()
    yield


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.state.database = database
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def session_dep():
    with database.session_factory() as session:
        yield session


SessionDep = Annotated[Session, Depends(session_dep)]


@app.middleware("http")
async def api_key_guard(request: Request, call_next):
    if (
        settings.api_key
        and request.url.path.startswith("/api")
        and request.url.path != "/api/health"
        and request.headers.get("x-api-key") != settings.api_key
    ):
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing API key"},
        )
    return await call_next(request)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/dashboard")
def dashboard(session: SessionDep):
    accounts = list(
        session.scalars(
            select(Account).order_by(Account.intent_score.desc()).limit(100)
        )
    )
    signals = list(
        session.scalars(
            select(Signal).order_by(Signal.discovered_at.desc()).limit(50)
        )
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    return {
        "total_accounts": session.scalar(select(func.count(Account.id))) or 0,
        "hot_accounts": session.scalar(
            select(func.count(Account.id)).where(Account.intent_band == "hot")
        )
        or 0,
        "warm_accounts": session.scalar(
            select(func.count(Account.id)).where(Account.intent_band == "warm")
        )
        or 0,
        "active_signals": session.scalar(
            select(func.count(Signal.id)).where(
                (Signal.expires_at.is_(None)) | (Signal.expires_at >= now)
            )
        )
        or 0,
        "accounts": [serialize_account(item) for item in accounts],
        "recent_signals": [serialize_signal(item) for item in signals],
    }


@app.post("/api/accounts", status_code=201)
def add_account(payload: AccountCreate, session: SessionDep):
    try:
        return serialize_account(create_account(session, payload))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/accounts/{account_id}/brief")
def account_brief(account_id: int, session: SessionDep):
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    signals = list(
        session.scalars(
            select(Signal)
            .where(Signal.account_id == account_id)
            .order_by(Signal.score.desc())
            .limit(8)
        )
    )
    reliability = (
        sum(item.source_reliability for item in signals) / len(signals)
        if signals
        else 0
    )
    timing = {"hot": "Act now", "warm": "Review this week"}.get(
        account.intent_band,
        "Monitor",
    )
    return {
        "account": serialize_account(account),
        "top_signals": [serialize_signal(item) for item in signals],
        "observed_changes": [item.title for item in signals],
        "likely_implications": account_implications(signals),
        "recommended_personas": account_personas(signals),
        "recommended_timing": timing,
        "evidence_quality": (
            "high" if reliability >= 0.85 else "medium" if signals else "none"
        ),
    }


@app.post("/api/accounts/{account_id}/outreach", status_code=201)
def outreach(account_id: int, payload: OutreachRequest, session: SessionDep):
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    signal = session.scalar(
        select(Signal)
        .where(Signal.account_id == account_id)
        .order_by(Signal.score.desc())
    )
    if not signal:
        raise HTTPException(status_code=422, detail="Account has no signals")

    subject, body = template_outreach(account, signal, payload)
    generated_by = "template"
    if payload.use_ai and settings.openai_api_key:
        try:
            from openai import OpenAI

            prompt = (
                f"Write one concise B2B {payload.channel} message using only this "
                f"verified event: {signal.title}. Phrase any business impact as a "
                f"possibility, not a fact. Persona: {payload.persona}. "
                f"CTA: {payload.call_to_action}. No hype, no fake familiarity, "
                "no em dash. Return JSON with subject and body."
            )
            response = OpenAI(api_key=settings.openai_api_key).responses.create(
                model=settings.openai_model,
                input=prompt,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "draft",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {
                                "subject": {"type": ["string", "null"]},
                                "body": {"type": "string"},
                            },
                            "required": ["subject", "body"],
                            "additionalProperties": False,
                        },
                    }
                },
            )
            parsed = json.loads(response.output_text)
            subject = parsed.get("subject")
            body = parsed["body"]
            generated_by = "openai"
        except Exception:
            generated_by = "template_fallback"

    draft = save_outreach(
        session,
        account=account,
        signal=signal,
        channel=payload.channel,
        subject=subject,
        body=body,
        generated_by=generated_by,
    )
    return {
        "id": draft.id,
        "account_id": draft.account_id,
        "signal_id": draft.signal_id,
        "channel": draft.channel,
        "subject": draft.subject,
        "body": draft.body,
        "generated_by": draft.generated_by,
        "created_at": draft.created_at.isoformat(),
    }


@app.get("/api/signals")
def list_signals(session: SessionDep):
    signals = session.scalars(
        select(Signal).order_by(Signal.event_date.desc()).limit(500)
    )
    return [serialize_signal(item) for item in signals]


@app.post("/api/signals", status_code=201)
def add_signal(payload: SignalCreate, session: SessionDep):
    try:
        signal, created = create_signal(session, payload)
    except (ValueError, LookupError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not created:
        raise HTTPException(
            status_code=409,
            detail={"message": "Duplicate signal", "signal_id": signal.id},
        )
    return serialize_signal(signal)


@app.post("/api/signals/{signal_id}/rescore")
def rescore(signal_id: int, session: SessionDep):
    signal = session.get(Signal, signal_id)
    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found")
    return serialize_signal(recompute_signal(session, signal))


@app.post("/api/ingest/demo")
def ingest_demo(session: SessionDep):
    now = datetime.now(UTC).replace(tzinfo=None)
    payloads = [
        SignalCreate(
            account_name="Northstar Health",
            account_domain="northstar-health.example",
            signal_type="executive_hire",
            title="Northstar Health appointed a new Chief People Officer",
            summary="The mandate covers workforce operations.",
            source_name="Company newsroom",
            source_url="https://example.com/northstar-cpo",
            event_date=now,
            confidence=0.95,
            source_reliability=0.95,
        ),
        SignalCreate(
            account_name="Northstar Health",
            account_domain="northstar-health.example",
            signal_type="job_posting",
            title="Northstar Health opened 42 clinical and operations roles",
            source_name="Careers page",
            source_url="https://example.com/northstar-careers",
            event_date=now,
            confidence=0.90,
            source_reliability=0.90,
        ),
        SignalCreate(
            account_name="ForgeWorks",
            account_domain="forgeworks.example",
            signal_type="expansion",
            title="ForgeWorks announced a second manufacturing facility in Texas",
            source_name="Company announcement",
            source_url="https://example.com/forgeworks",
            event_date=now,
            confidence=0.92,
            source_reliability=0.90,
        ),
        SignalCreate(
            account_name="OrbitPay",
            account_domain="orbitpay.example",
            signal_type="funding",
            title="OrbitPay raised a Series B to expand enterprise operations",
            source_name="Funding announcement",
            source_url="https://example.com/orbitpay",
            event_date=now,
            confidence=0.95,
            source_reliability=0.90,
        ),
    ]
    created = duplicates = 0
    for payload in payloads:
        _, was_created = create_signal(session, payload)
        created += int(was_created)
        duplicates += int(not was_created)
    return {"created": created, "duplicates": duplicates}


@app.post("/api/ingest/csv")
async def ingest_csv(session: SessionDep, file: UploadFile = File(...)):
    content = await file.read()
    if len(content) > 5_000_000:
        raise HTTPException(status_code=413, detail="CSV is too large")
    created = duplicates = 0
    errors = []
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    for row_number, row in enumerate(reader, start=2):
        try:
            payload = SignalCreate(
                account_name=row.get("account_name"),
                account_domain=row.get("account_domain"),
                signal_type=row["signal_type"],
                title=row["title"],
                summary=row.get("summary") or None,
                source_name=row.get("source_name") or "CSV import",
                source_url=row.get("source_url") or None,
                event_date=datetime.fromisoformat(
                    row["event_date"].replace("Z", "+00:00")
                ),
                evidence_kind=row.get("evidence_kind") or "direct",
                confidence=float(row.get("confidence") or 0.75),
                source_reliability=float(row.get("source_reliability") or 0.75),
            )
            _, was_created = create_signal(session, payload)
            created += int(was_created)
            duplicates += int(not was_created)
        except (KeyError, TypeError, ValueError) as exc:
            errors.append({"row": row_number, "error": str(exc)})
    return {"created": created, "duplicates": duplicates, "errors": errors[:50]}


def serialize_account(item: Account):
    return {
        "id": item.id,
        "name": item.name,
        "domain": item.domain,
        "industry": item.industry,
        "country": item.country,
        "employee_count": item.employee_count,
        "icp_fit": item.icp_fit,
        "watchlisted": item.watchlisted,
        "intent_score": item.intent_score,
        "intent_band": item.intent_band,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def serialize_signal(item: Signal):
    return {
        "id": item.id,
        "account_id": item.account_id,
        "signal_type": item.signal_type,
        "title": item.title,
        "summary": item.summary,
        "source_name": item.source_name,
        "source_url": item.source_url,
        "event_date": item.event_date.isoformat(),
        "discovered_at": item.discovered_at.isoformat(),
        "evidence_kind": item.evidence_kind,
        "confidence": item.confidence,
        "source_reliability": item.source_reliability,
        "score": item.score,
        "expires_at": item.expires_at.isoformat() if item.expires_at else None,
        "raw_payload": item.raw_payload,
    }


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    return HTMLResponse(
        """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Intent Signal Update</title><style>
body{font-family:system-ui;max-width:960px;margin:40px auto;padding:0 20px}
.muted{color:#667}button{padding:10px;margin:4px}table{width:100%;border-collapse:collapse}
td,th{padding:10px;border-bottom:1px solid #ddd;text-align:left}
</style></head><body><h1>Intent Signal Update</h1>
<p class="muted">Detect observable changes, rank accounts, and create evidence-based outreach.</p>
<button id="seed">Load demo data</button><button id="refresh">Refresh</button>
<div id="summary"></div><table><thead><tr><th>Account</th><th>Score</th><th>Band</th></tr></thead>
<tbody id="rows"></tbody></table><script>
const summary=document.getElementById('summary');const rows=document.getElementById('rows');
async function load(){const d=await fetch('/api/dashboard').then(r=>r.json());
summary.textContent=`${d.total_accounts} accounts · ${d.active_signals} active signals`;
rows.replaceChildren(...d.accounts.map(a=>{const tr=document.createElement('tr');
[a.name,String(a.intent_score),a.intent_band].forEach(v=>{const td=document.createElement('td');
td.textContent=v;tr.appendChild(td)});return tr}))}
document.getElementById('seed').onclick=async()=>{await fetch('/api/ingest/demo',{method:'POST'});load()};
document.getElementById('refresh').onclick=load;load();</script></body></html>"""
    )
