# Skills

Each skill should be independently testable and return structured evidence.

## Proposed skills

### `detect-buying-signals`
Find recent events that may indicate an account is entering a buying window.

### `validate-signal`
Confirm the event, source quality, company identity, and timestamp.

### `score-account-intent`
Rank the account using recency, relevance, confidence, and ICP fit.

### `map-buying-committee`
Identify the functions and personas most likely affected by the signal.

### `write-evidence-based-outreach`
Draft concise outreach that references the verified event and a plausible business implication.

### `refresh-signal-watchlist`
Re-check active accounts and reduce scores as signals become stale.

## Minimum skill output

```json
{
  "account": "Example Inc.",
  "signal_type": "executive_hire",
  "observed_event": "A new CRO joined the company",
  "source": "verified source reference",
  "event_date": "YYYY-MM-DD",
  "confidence": 0.87,
  "intent_score": 76,
  "reasoning_summary": "The event may precede a review of revenue systems and vendors.",
  "recommended_personas": ["CRO", "VP Sales", "RevOps"],
  "outreach_angle": "Reference the leadership transition and likely operating priorities."
}
```
