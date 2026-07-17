# Architecture

```text
Manual input / CSV / API
          ↓
validation and normalization
          ↓
company resolution + deduplication
          ↓
signal scoring and time decay
          ↓
account aggregation + reinforcement
          ↓
briefs, priority queue, outreach
```

The FastAPI service serves a browser dashboard and an OpenAPI REST interface. SQLAlchemy models store accounts, signals, outreach drafts, and reusable feed-source metadata. SQLite is the zero-configuration default; a PostgreSQL URL can be supplied without changing application code.

Each signal type has a base commercial weight, half-life, likely implication, and stakeholder map. Scores combine recency, source reliability, evidence confidence, and direct-versus-inferred provenance. Account scores weight the strongest active signals, add cross-signal reinforcement, and adjust for ICP fit.

A SHA-256 fingerprint generated from company domain, type, event title, event date, and source URL prevents duplicate events from inflating scores. Expired events remain available for audit but no longer influence active intent.

The deterministic outreach generator always works without external services. The optional OpenAI path uses structured output and falls back to the deterministic draft when unavailable.
