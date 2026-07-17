from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    industry: Mapped[str | None] = mapped_column(String(120), nullable=True)
    country: Mapped[str | None] = mapped_column(String(80), nullable=True)
    employee_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    icp_fit: Mapped[float] = mapped_column(Float, nullable=False, default=0.70)
    watchlisted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    intent_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, index=True)
    intent_band: Mapped[str] = mapped_column(String(20), nullable=False, default="cold", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    signals: Mapped[list[Signal]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    outreach_drafts: Mapped[list[OutreachDraft]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (UniqueConstraint("fingerprint", name="uq_signal_fingerprint"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    signal_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_name: Mapped[str] = mapped_column(String(160), nullable=False, default="manual")
    source_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    event_date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    evidence_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="direct")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.75)
    source_reliability: Mapped[float] = mapped_column(Float, nullable=False, default=0.75)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    account: Mapped[Account] = relationship(back_populates="signals")
    outreach_drafts: Mapped[list[OutreachDraft]] = relationship(back_populates="signal")


class OutreachDraft(Base):
    __tablename__ = "outreach_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    signal_id: Mapped[int | None] = mapped_column(
        ForeignKey("signals.id", ondelete="SET NULL"), nullable=True
    )
    channel: Mapped[str] = mapped_column(String(30), nullable=False, default="email")
    subject: Mapped[str | None] = mapped_column(String(300), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    generated_by: Mapped[str] = mapped_column(String(50), nullable=False, default="template")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    account: Mapped[Account] = relationship(back_populates="outreach_drafts")
    signal: Mapped[Signal | None] = relationship(back_populates="outreach_drafts")


class FeedSource(Base):
    __tablename__ = "feed_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    url: Mapped[str] = mapped_column(String(2000), nullable=False, unique=True)
    default_account_name: Mapped[str] = mapped_column(String(200), nullable=False)
    default_account_domain: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
