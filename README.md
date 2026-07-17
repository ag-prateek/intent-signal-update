# Intent Signal Update

A runnable B2B intent-signal MVP that detects observable business events, scores account intent, creates explainable account briefs, and drafts outreach grounded in verified evidence.

## Included

- Account and watchlist data model
- Manual signal capture through dashboard and API
- CSV bulk ingestion
- Stable duplicate detection
- Signal-specific decay and expiration
- Confidence, source reliability, and direct/inferred provenance
- Cross-signal reinforcement and ICP-fit adjustment
- Hot, warm, watch, and cold account bands
- Account briefs with evidence, implications, personas, and timing
- Email and LinkedIn outreach generation
- Optional OpenAI Responses API drafting with deterministic fallback
- API-key protection
- SQLite by default, PostgreSQL-compatible through SQLAlchemy
- Docker and GitHub Actions CI

## Run locally

```bash
cp .env.example .env
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install -e ".[dev]"
uvicorn app.main:app --reload
```

Open `http://localhost:8000`. API docs are at `http://localhost:8000/docs`.

## Docker

```bash
cp .env.example .env
docker compose up --build
```

## Configuration

```dotenv
DATABASE_URL=sqlite:///./intent_signals.db
API_KEY=
OPENAI_API_KEY=
OPENAI_MODEL=gpt-5-mini
```

The app works without OpenAI. When `OPENAI_API_KEY` is absent, outreach uses the evidence-backed deterministic template. If `API_KEY` is set, API routes require `x-api-key` except `/api/health`.

## Core scoring

Each signal type has a commercial weight and half-life. Signal scores combine recency decay, source reliability, confidence, and direct-versus-inferred evidence. Account scores combine the strongest active signals, add reinforcement when independent signal types align, and adjust for ICP fit.

Buying intent is always presented as an inference, never as a confirmed fact.

## API examples

```bash
curl -X POST http://localhost:8000/api/signals \
  -H "Content-Type: application/json" \
  -d '{
    "account_name":"Acme",
    "account_domain":"acme.com",
    "signal_type":"executive_hire",
    "title":"Acme appointed a new CRO",
    "source_name":"Company newsroom",
    "source_url":"https://acme.com/news/cro",
    "event_date":"2026-07-10T12:00:00Z",
    "confidence":0.95,
    "source_reliability":0.95
  }'
```

A CSV import endpoint is available at `POST /api/ingest/csv`. Required columns are `account_name`, `account_domain`, `signal_type`, `title`, and `event_date`.

## Validation

```bash
ruff check .
pytest
```

## Guardrails

- Do not fabricate intent or business problems.
- Keep direct evidence separate from inference.
- Allow signals to decay and expire.
- Do not let duplicate events inflate scores.
- Use only sources and outreach practices permitted by applicable terms, privacy rules, and regulations.
