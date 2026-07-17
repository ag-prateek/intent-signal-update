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
    "first_party_engagement": SignalProfile(95, 14, "Active category research or vendor interest", ("Functional leader", "Operations", "Proc