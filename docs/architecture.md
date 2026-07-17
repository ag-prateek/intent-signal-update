# Architecture Outline

## 1. Ingestion

Collect structured and unstructured events from approved public, licensed, and first-party sources.

Potential signal families:

- Hiring and workforce changes
- Funding, acquisition, and expansion
- Executive and leadership changes
- Technology adoption or migration
- Website, product, and pricing changes
- Regulatory and compliance events
- Content consumption and first-party engagement
- Vendor research and category activity

## 2. Signal normalization

Convert events into a common schema:

- account
- signal type
- event timestamp
- discovery timestamp
- source
- supporting evidence
- confidence
- expiration window
- affected function
- likely business implication

## 3. Validation

- Resolve the company identity
- Verify the event against the source
- Remove duplicates
- Distinguish direct evidence from inference
- Reject stale or weak signals

## 4. Scoring

Suggested dimensions:

- Recency
- Source reliability
- Signal specificity
- ICP fit
- Persona relevance
- Commercial urgency
- Cross-signal reinforcement

## 5. Activation

Produce an account brief containing:

- The observed signal
- Why it may indicate active demand
- Relevant stakeholders
- Evidence links or references
- Recommended timing
- Suggested outreach angle
- Draft email or message

## 6. Guardrails

- Do not fabricate intent
- Clearly label inferences
- Avoid sensitive personal data
- Respect source terms, privacy rules, and outreach regulations
- Keep outreach relevant, factual, and easy to opt out of
