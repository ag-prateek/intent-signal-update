from __future__ import annotations

import csv
import io
from datetime import UTC, datetime

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
app = FastAPI(title=settings.app_name, version="0.1.0")
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


DB = Depends(session_dep)


@app.on_event("startup")
def startup() -> None:
    database.create_all()


@app.middleware("http")
async def api_key_guard(request: Request, call_next):
    if (
        settings.api_key
        and request.url.path.startswith("/api")
        and request.url.path != "/api/health"
        and request.headers.get("x-api-key") != settings.api_key
    ):
        return JSONResponse(status_code=401, content={"detail": "Invalid or missing API key"})
    return await call_next(request)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/dashboard")
def dashboard(session: Session = DB):
    accounts = list(session.scalars(select(Account).order_by(Account.intent_score.desc()).limit(100)))
    signals = list(session.scalars(select(Signal).order_by(Signal.discovered_at.desc()).limit(50)))
    now = datetime.now(UTC).replace(tzinfo=None)
    return {
        "total_accounts": session.scalar(select(func.count(Account.id))) or 0,
        "hot_accounts": session.scalar(select(func.count(Account.id)).where(Account.intent_band == "hot")) or 0,
        "warm_accounts": session.scalar(select(func.count(Account.id)).where(Account.intent_band == "warm")) or 0,
        "active_signals": session.scalar(select(func.count(Signal.id)).where((Signal.expires_at.is_(None)) | (Signal.expires_at >= now))) or 0,
        "accounts": [serialize_account(item) for item in accounts],
        "recent_signals": [serialize_signal(item) for item in signals],
    }


@app.post("/api/accounts", status_code=201)
def add_account(payload: AccountCreate, session: Session = DB):
    try:
        return serialize_account(create_account(session, payload))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/accounts/{account_id}/brief")
def account_brief(account_id: int, session: Session = DB):
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    signals = list(session.scalars(select(Signal).where(Signal.account_id == account_id).order_by(Signal.score.desc()).limit(8)))
    reliability = sum(item.source_reliability for item in signals) / len(signals) if signals else 0
    return {
        "account": serialize_account(account),
        "top_signals": [serialize_signal(item) for item in signals],
        "observed_changes": [item.title for item in signals],
        "likely_implications": account_implications(signals),
        "recommended_personas": account_personas(signals),
        "recommended_timing": "Act now" if account.intent_band == "hot" else "Review this week" if account.intent_band == "warm" else "Monitor",
        "evidence_quality": "high" if reliability >= 0.85 else "medium" if signals else "none",
    }


@app.post("/api/accounts/{account_id}/outreach", status_code=201)
def outreach(account_id: int, payload: OutreachRequest, session: Session = DB):
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    signal = session.scalar(select(Signal).where(Signal.account_id == account_id).order_by(Signal.score.desc()))
    if not signal:
        raise HTTPException(status_code=422, detail="Account has no signals")
    subject, body = template_outreach(account, signal, payload)
    generated_by = "template"
    if payload.use_ai and settings.openai_api_key:
        try:
            import json
            from openai import OpenAI

            prompt = (
                f"Write one concise B2B {payload.channel} message using only this verified event: "
                f"{signal.title}. Phrase any business impact as a possibility, not a fact. "
                f"Persona: {payload.persona}. CTA: {payload.call_to_action}. "
                "No hype, no fake familiarity, no em dash. Return JSON with subject and body."
            )
            response = OpenAI(api_key=settings.openai_api_key).responses.create(
                model=settings.openai_model,
                input=prompt,
                text={"format": {"type": "json_schema", "name": "draft", "strict": True, "schema": {"type": "object", "properties": {"subject": {"type": ["string", "null"]}, "body": {"type": "string"}}, "required": ["subject", "body"], "additionalProperties": False}}},
            )
            parsed = json.loads(response.output_text)
            subject, body, generated_by = parsed.get("subject"), parsed["body"], "openai"
        except Exception:
            generated_by = "template_fallback"
    draft = save_outreach(session, account=account, signal=signal, channel=payload.channel, subject=subject, body=body, generated_by=generated_by)
    return {"id": draft.id, "account_id": draft.account_id, "signal_id": draft.signal_id, "channel": draft.channel, "subject": draft.subject, "body": draft.body, "generated_by": draft.generated_by, "created_at": draft.created_at.isoformat()}


@app.get("/api/signals")
def list_signals(session: Session = DB):
    return [serialize_signal(item) for item in session.scalars(select(Signal).order_by(Signal.event_date.desc()).limit(500))]


@app.post("/api/signals", status_code=201)
def add_signal(payload: SignalCreate, session: Session = DB):
    try:
        signal, created = create_signal(session, payload)
    except (ValueError, LookupError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not created:
        raise HTTPException(status_code=409, detail={"message": "Duplicate signal", "signal_id": signal.id})
    return serialize_signal(signal)


@app.post("/api/signals/{signal_id}/rescore")
def rescore(signal_id: int, session: Session = DB):
    signal = session.get(Signal, signal_id)
    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found")
    return serialize_signal(recompute_signal(session, signal))


@app.post("/api/ingest/demo")
def ingest_demo(session: Session = DB):
    now = datetime.now(UTC).replace(tzinfo=None)
    payloads = [
        SignalCreate(account_name="Northstar Health", account_domain="northstar-health.example", signal_type="executive_hire", title="Northstar Health appointed a new Chief People Officer", summary="The mandate covers workforce operations.", source_name="Company newsroom", source_url="https://example.com/northstar-cpo", event_date=now, confidence=0.95, source_reliability=0.95),
        SignalCreate(account_name="Northstar Health", account_domain="northstar-health.example", signal_type="job_posting", title="Northstar Health opened 42 clinical and operations roles", source_name="Careers page", source_url="https://example.com/northstar-careers", event_date=now, confidence=0.90, source_reliability=0.90),
        SignalCreate(account_name="ForgeWorks", account_domain="forgeworks.example", signal_type="expansion", title="ForgeWorks announced a second manufacturing facility in Texas", source_name="Company announcement", source_url="https://example.com/forgeworks", event_date=now, confidence=0.92, source_reliability=0.90),
        SignalCreate(account_name="OrbitPay", account_domain="orbitpay.example", signal_type="funding", title="OrbitPay raised a Series B to expand enterprise operations", source_name="Funding announcement", source_url="https://example.com/orbitpay", event_date=now, confidence=0.95, source_reliability=0.90),
    ]
    created = duplicates = 0
    for payload in payloads:
        _, was_created = create_signal(session, payload)
        created += int(was_created)
        duplicates += int(not was_created)
    return {"created": created, "duplicates": duplicates}


@app.post("/api/ingest/csv")
async def ingest_csv(file: UploadFile = File(...), session: Session = DB):
    content = await file.read()
    if len(content) > 5_000_000:
        raise HTTPException(status_code=413, detail="CSV is too large")
    created = duplicates = 0
    errors = []
    for row_number, row in enumerate(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))), start=2):
        try:
            payload = SignalCreate(
                account_name=row.get("account_name"), account_domain=row.get("account_domain"),
                signal_type=row["signal_type"], title=row["title"], summary=row.get("summary") or None,
                source_name=row.get("source_name") or "CSV import", source_url=row.get("source_url") or None,
                event_date=datetime.fromisoformat(row["event_date"].replace("Z", "+00:00")),
                evidence_kind=row.get("evidence_kind") or "direct",
                confidence=float(row.get("confidence") or 0.75), source_reliability=float(row.get("source_reliability") or 0.75),
            )
            _, was_created = create_signal(session, payload)
            created += int(was_created)
            duplicates += int(not was_created)
        except Exception as exc:
            errors.append({"row": row_number, "error": str(exc)})
    return {"created": created, "duplicates": duplicates, "errors": errors[:50]}


def serialize_account(item: Account):
    return {"id": item.id, "name": item.name, "domain": item.domain, "industry": item.industry, "country": item.country, "employee_count": item.employee_count, "icp_fit": item.icp_fit, "watchlisted": item.watchlisted, "intent_score": item.intent_score, "intent_band": item.intent_band, "created_at": item.created_at.isoformat(), "updated_at": item.updated_at.isoformat()}


def serialize_signal(item: Signal):
    return {"id": item.id, "account_id": item.account_id, "signal_type": item.signal_type, "title": item.title, "summary": item.summary, "source_name": item.source_name, "source_url": item.source_url, "event_date": item.event_date.isoformat(), "discovered_at": item.discovered_at.isoformat(), "evidence_kind": item.evidence_kind, "confidence": item.confidence, "source_reliability": item.source_reliability, "score": item.score, "expires_at": item.expires_at.isoformat() if item.expires_at else None, "raw_payload": item.raw_payload}


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    return HTMLResponse("""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>Intent Signal Update</title><style>body{font-family:Inter,system-ui;background:#09100f;color:#eef7f2;margin:0}main{max-width:1100px;margin:auto;padding:40px 20px}h1{font-size:54px;letter-spacing:-3px;margin:0}.muted{color:#9bada5}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:26px 0}.card{background:#111b18;border:1px solid #263b34;border-radius:14px;padding:18px}button,input,select,textarea{font:inherit;padding:10px;border-radius:8px;border:1px solid #31483f;background:#14211d;color:#eef7f2}button{background:#72e5b5;color:#06110d;font-weight:800;cursor:pointer}.row{display:flex;gap:10px;flex-wrap:wrap}table{width:100%;border-collapse:collapse}td,th{padding:11px;border-bottom:1px solid #263b34;text-align:left}.panel{margin:14px 0}.score{font-size:30px;font-weight:800}@media(max-width:700px){.grid{grid-template-columns:1fr 1fr}h1{font-size:40px}}</style></head><body><main><p class='muted'>Revenue intelligence</p><h1>Intent Signal Update</h1><p class='muted'>Detect credible buying signals, rank accounts, and write outreach grounded in evidence.</p><div class='row'><button onclick='seed()'>Load demo</button><button onclick='load()'>Refresh</button></div><section class='grid'><div class='card'><span class='muted'>Accounts</span><div id='a' class='score'>0</div></div><div class='card'><span class='muted'>Hot</span><div id='h' class='score'>0</div></div><div class='card'><span class='muted'>Warm</span><div id='w' class='score'>0</div></div><div class='card'><span class='muted'>Signals</span><div id='s' class='score'>0</div></div></section><section class='card panel'><h2>Priority accounts</h2><table><thead><tr><th>Account</th><th>Score</th><th>Band</th><th></th></tr></thead><tbody id='rows'></tbody></table></section><section class='card panel'><h2>Add verified signal</h2><form id='f' class='row'><input name='account_name' placeholder='Account name' required><input name='account_domain' placeholder='domain.com' required><select name='signal_type'><option>funding</option><option>executive_hire</option><option>expansion</option><option>job_posting</option><option>technology_change</option><option>product_launch</option><option>compliance_event</option><option>first_party_engagement</option></select><input name='title' placeholder='What changed?' required style='min-width:260px'><input name='event_date' type='date' required><input name='source_name' value='Manual research'><input name='source_url' type='url' placeholder='https://source'><button>Add signal</button></form></section><section id='brief' class='card panel' style='display:none'></section></main><script>async function api(p,o={}){let r=await fetch(p,{headers:{'Content-Type':'application/json'},...o});let d=await r.json();if(!r.ok)throw Error(JSON.stringify(d.detail));return d}async function load(){let d=await api('/api/dashboard');a.textContent=d.total_accounts;h.textContent=d.hot_accounts;w.textContent=d.warm_accounts;s.textContent=d.active_signals;rows.innerHTML=d.accounts.map(x=>`<tr><td><b>${x.name}</b><br><span class=muted>${x.domain}</span></td><td>${x.intent_score.toFixed(1)}</td><td>${x.intent_band}</td><td><button onclick='openBrief(${x.id})'>Brief</button></td></tr>`).join('')}async function seed(){await api('/api/ingest/demo',{method:'POST',body:'{}'});load()}async function openBrief(id){let d=await api('/api/accounts/'+id+'/brief');brief.style.display='block';brief.innerHTML=`<h2>${d.account.name}</h2><p class=muted>${d.account.intent_band} intent · ${d.account.intent_score.toFixed(1)} · evidence ${d.evidence_quality}</p><h3>Observed</h3><ul>${d.observed_changes.map(x=>`<li>${x}</li>`).join('')}</ul><h3>Implications</h3><ul>${d.likely_implications.map(x=>`<li>${x}</li>`).join('')}</ul><p><b>Personas:</b> ${d.recommended_personas.join(', ')}</p><button onclick='draft(${id})'>Draft outreach</button><pre id='draft'></pre>`}async function draft(id){let d=await api('/api/accounts/'+id+'/outreach',{method:'POST',body:JSON.stringify({channel:'email',persona:'relevant executive',use_ai:true})});document.getElementById('draft').textContent=(d.subject||'')+'\n\n'+d.body}f.onsubmit=async e=>{e.preventDefault();let x=Object.fromEntries(new FormData(f));x.event_date=new Date(x.event_date+'T12:00:00Z').toISOString();if(!x.source_url)delete x.source_url;await api('/api/signals',{method:'POST',body:JSON.stringify(x)});f.reset();load()};load()</script></body></html>""")
