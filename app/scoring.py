from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import pow


@dataclass(frozen=True)
class SignalProfile:
    base_weight: float
    half_life_days: int
    implication: str
    personas: tuple[str, ...]


SIGNAL_PROFILES: dict[str, SignalProfile] = {
    "first_party_engagement": SignalProfile(
        95,
        14,
        "Active category research or vendor interest",
        ("Functional leader", "Operations", "Procurement"),
    ),
    "funding": SignalProfile(
        90,
        120,
        "New budget and pressure to scale execution",
        ("CEO", "COO", "CFO", "Functional leader"),
    ),
    "executive_hire": SignalProfile(
        84,
        90,
        "A new leader may review systems, vendors, and operating priorities",
        ("New executive", "Operations", "Finance"),
    ),
    "acquisition": SignalProfile(
        88,
        120,
        "Integration work often creates process and tooling changes",
        ("COO", "CIO", "Finance", "People leader"),
    ),
    "expansion": SignalProfile(
        82,
        90,
        "Growth can create capacity, hiring, and workflow pressure",
        ("COO", "Functional leader", "Finance"),
    ),
    "technology_change": SignalProfile(
        80,
        90,
        "A stack change can signal an active transformation program",
        ("CIO", "IT", "Operations", "Functional leader"),
    ),
    "compliance_event": SignalProfile(
        78,
        60,
        "A regulatory event can create time-bound remediation demand",
        ("Legal", "Compliance", "CISO", "Operations"),
    ),
    "job_posting": SignalProfile(
        68,
        45,
        "Hiring volume can reveal a near-term capability or capacity gap",
        ("Hiring manager", "People leader", "Finance"),
    ),
    "product_launch": SignalProfile(
        70,
        45,
        "A launch can increase go-to-market and operational complexity",
        ("Product", "Marketing", "Sales", "Operations"),
    ),
    "pricing_change": SignalProfile(
        72,
        30,
        "Pricing changes may accompany packaging, monetization, or systems work",
        ("CRO", "Product", "Finance", "RevOps"),
    ),
    "partnership": SignalProfile(
        62,
        60,
        "A new partnership can create enablement and integration needs",
        ("Partnerships", "Operations", "Product"),
    ),
    "leadership_change": SignalProfile(
        76,
        75,
        "Leadership change can reset priorities and vendor relationships",
        ("Executive team", "Operations", "Finance"),
    ),
    "generic": SignalProfile(
        48,
        30,
        "The observed change may create a relevant operating need",
        ("Functional leader", "Operations"),
    ),
}


def profile_for(signal_type: str) -> SignalProfile:
    key = signal_type.strip().lower().replace("-", "_").replace(" ", "_")
    return SIGNAL_PROFILES.get(key, SIGNAL_PROFILES["generic"])


def score_signal(
    *,
    signal_type: str,
    event_date: datetime,
    confidence: float,
    source_reliability: float,
    evidence_kind: str,
    now: datetime | None = None,
) -> tuple[float, datetime]:
    now = now or datetime.now(UTC).replace(tzinfo=None)
    profile = profile_for(signal_type)
    age_days = max(0.0, (now - event_date).total_seconds() / 86400)
    recency = pow(0.5, age_days / profile.half_life_days)
    reliability_factor = 0.40 + (0.60 * max(0, min(source_reliability, 1)))
    confidence_factor = 0.50 + (0.50 * max(0, min(confidence, 1)))
    evidence_factor = 1.0 if evidence_kind == "direct" else 0.82
    score = profile.base_weight * recency * reliability_factor * confidence_factor * evidence_factor
    expires_at = event_date + timedelta(days=profile.half_life_days * 3)
    return round(max(0.0, min(score, 100.0)), 2), expires_at


def score_account(
    signal_scores: list[float],
    signal_types: list[str],
    icp_fit: float,
) -> tuple[float, str]:
    if not signal_scores:
        return 0.0, "cold"

    ranked = sorted(signal_scores, reverse=True)
    contribution_weights = (1.0, 0.62, 0.38, 0.22, 0.12)
    weighted = sum(
        score * contribution_weights[index]
        for index, score in enumerate(ranked[:5])
    )
    normalizer = sum(contribution_weights[: min(len(ranked), 5)])
    base = weighted / normalizer

    unique_types = len(set(signal_types))
    reinforcement = min(
        15.0,
        max(0, unique_types - 1) * 4.0 + max(0, len(ranked) - 1) * 1.5,
    )
    fit_factor = 0.65 + (0.35 * max(0, min(icp_fit, 1)))
    final = round(min(100.0, (base + reinforcement) * fit_factor), 2)

    if final >= 75:
        band = "hot"
    elif final >= 50:
        band = "warm"
    elif final >= 25:
        band = "watch"
    else:
        band = "cold"
    return final, band
