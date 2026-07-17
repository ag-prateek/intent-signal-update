from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    domain: str = Field(min_length=3, max_length=255)
    industry: str | None = None
    country: str | None = None
    employee_count: int | None = Field(default=None, ge=1)
    icp_fit: float = Field(default=0.70, ge=0, le=1)
    watchlisted: bool = True


class AccountRead(AccountCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    intent_score: float
    intent_band: str
    created_at: datetime
    updated_at: datetime


class SignalCreate(BaseModel):
    account_id: int | None = None
    account_name: str | None = None
    account_domain: str | None = None
    signal_type: str = Field(min_length=2, max_length=80)
    title: str = Field(min_length=3, max_length=500)
    summary: str | None = None
    source_name: str = Field(default="manual", max_length=160)
    source_url: HttpUrl | None = None
    event_date: datetime
    evidence_kind: Literal["direct", "inferred"] = "direct"
    confidence: float = Field(default=0.75, ge=0, le=1)
    source_reliability: float = Field(default=0.75, ge=0, le=1)
    raw_payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_date")
    @classmethod
    def strip_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is not None:
            return value.astimezone(UTC).replace(tzinfo=None)
        return value


class SignalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    signal_type: str
    title: str
    summary: str | None
    source_name: str
    source_url: str | None
    event_date: datetime
    discovered_at: datetime
    evidence_kind: str
    confidence: float
    source_reliability: float
    score: float
    expires_at: datetime | None
    raw_payload: dict[str, Any]


class OutreachRequest(BaseModel):
    channel: Literal["email", "linkedin"] = "email"
    persona: str = Field(default="relevant executive", max_length=120)
    sender_context: str | None = Field(default=None, max_length=500)
    call_to_action: str = Field(default="Would a brief conversation be useful?", max_length=300)
    use_ai: bool = True


class OutreachRead(BaseModel):
    id: int
    account_id: int
    signal_id: int | None
    channel: str
    subject: str | None
    body: str
    generated_by: str
    created_at: datetime


class FeedCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    url: HttpUrl
    default_account_name: str = Field(min_length=1, max_length=200)
    default_account_domain: str = Field(min_length=3, max_length=255)
    enabled: bool = True


class FeedRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    url: str
    default_account_name: str
    default_account_domain: str
    enabled: bool
    last_polled_at: datetime | None
    created_at: datetime


class RSSIngestRequest(BaseModel):
    feed_url: HttpUrl
    account_name: str = Field(min_length=1, max_length=200)
    account_domain: str = Field(min_length=3, max_length=255)
    source_name: str = "RSS"


class AccountBrief(BaseModel):
    account: AccountRead
    top_signals: list[SignalRead]
    observed_changes: list[str]
    likely_implications: list[str]
    recommended_personas: list[str]
    recommended_timing: str
    evidence_quality: str


class DashboardRead(BaseModel):
    total_accounts: int
    hot_accounts: int
    warm_accounts: int
    active_signals: int
    accounts: list[AccountRead]
    recent_signals: list[SignalRead]
