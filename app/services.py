from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Account, OutreachDraft, Signal
from app.schemas import AccountCreate, OutreachRequest, SignalCreate
from app.scoring import profile_for, score_account, score_signal


def normalize_domain(value: str) -> str:
    value = value.strip().lower()
    if "://" in value:
        value = urlparse(value).netloc
    value = value.split("/")[0].split(":")[0]
    if value.startswith("www."):
        value = value[4:]
    if "." not in value:
        raise ValueError("A valid company domain is required")
    return value


def signal_fingerprint(domain: str, payload: SignalCreate) -> str:
    raw = "|".join(
        [
            normalize_domain(domain),
            payload.signal_type.strip().lower(),
            payload.title.strip().lower(),
            payload.event_date.date().isoformat(),
            str(payload.source_url or ""),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_or_create_account(session: Session, payload: SignalCreate) -> Account:
    if payload.account_id is not None:
        account = session.get(Account, payload.account_id)
        if not account:
            raise LookupError("Account not found")
        return account

    if not payload.account_name or not payload.account_domain:
        raise ValueError("Provide account_id or account_name and account_domain")

    domain = normalize_domain(payload.account_domain)
    account = session.scalar(select(Account).where(Account.domain == domain))
    if account:
        return account

    account = Account(name=payload.account_name.strip(), domain=domain)
    session.add(account)
    session.flush()
    return account


def create_account(session: Session, payload: AccountCreate) -> Account:
    domain = normalize_domain(payload.domain)
    existing = session.scalar(select(Account).where(Account.domain == domain))
    if existing:
        raise ValueError("An account with this domain already exists")
    account = Account(**payload.model_dump(exclude={"domain"}), domain=domain)
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def create_signal(session: Session, payload: SignalCreate) -> tuple[Signal, bool]:
    account = get_or_create_account(session, payload)
    fingerprint = signal_fingerprint(account.domain, payload)
    existing = session.scalar(select(Signal).where(Signal.fingerprint == fingerprint))
    if existing:
        return existing, False

    score, expires_at = score_signal(
        signal_type=payload.signal_type,
        event_date=payload.event_date,
        confidence=payload.confidence,
        source_reliability=payload.source_reliability,
        evidence_kind=payload.evidence_kind,
    )
    signal = Signal(
        account_id=account.id,
        signal_type=payload.signal_type.strip().lower().replace("-", "_").replace(" ", "_"),
        title=payload.title.strip(),
        summary=payload.summary,
        source_name=payload.source_name,
        source_url=str(payload.source_url) if payload.source_url else None,
        event_date=payload.event_date,
        evidence_kind=payload.evidence_kind,
        confidence=payload.confidence,
        source_reliability=payload.source_reliability,
        raw_payload=payload.raw_payload,
        score=score,
        expires_at=expires_at,
        fingerprint=fingerprint,
    )
    session.add(signal)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.scalar(select(Signal).where(Signal.fingerprint == fingerprint))
        if existing:
            return existing, False
        raise
    session.refresh(signal)
    recompute_account(session, account.id)
    return signal, True


def recompute_signal(session: Session, signal: Signal) -> Signal:
    score, expires_at = score_signal(
        signal_type=signal.signal_type,
        event_date=signal.event_date,
        confidence=signal.confidence,
        source_reliability=signal.source_reliability,
        evidence_kind=signal.evidence_kind,
    )
    signal.score = score
    signal.expires_at = expires_at
    session.add(signal)
    session.commit()
    session.refresh(signal)
    recompute_account(session, signal.account_id)
    return signal


def recompute_account(session: Session, account_id: int) -> Account:
    account = session.get(Account, account_id)
    if not account:
        raise LookupError("Account not found")
    now = datetime.now(UTC).replace(tzinfo=None)
    active = list(
        session.scalars(
            select(Signal).where(
                Signal.account_id == account_id,
                (Signal.expires_at.is_(None)) | (Signal.expires_at >= now),
            )
        )
    )
    account.intent_score, account.intent_band = score_account(
        [item.score for item in active],
        [item.signal_type for item in active],
        account.icp_fit,
    )
    account.updated_at = now
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def account_implications(signals: list[Signal]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for signal in signals:
        implication = profile_for(signal.signal_type).implication
        if implication not in seen:
            seen.add(implication)
            output.append(implication)
    return output[:5]


def account_personas(signals: list[Signal]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for signal in signals:
        for persona in profile_for(signal.signal_type).personas:
            if persona not in seen:
                seen.add(persona)
                output.append(persona)
    return output[:6]


def template_outreach(
    account: Account,
    signal: Signal,
    request: OutreachRequest,
) -> tuple[str | None, str]:
    observed = signal.title.rstrip(".")
    implication = profile_for(signal.signal_type).implication.lower()
    sender_context = request.sender_context.strip() if request.sender_context else None

    if request.channel == "linkedin":
        context_sentence = f" {sender_context}" if sender_context else ""
        body = (
            f"Hi {{firstName}}, I noticed {observed}. That may create {implication}."
            f"{context_sentence} {request.call_to_action}"
        )
        return None, body

    subject = f"{account.name} and {signal.signal_type.replace('_', ' ')}"
    paragraphs = [
        "Hi {firstName},",
        (
            f"I noticed {observed}. Given your role as {request.persona}, I thought it might be "
            f"relevant because this may create {implication}."
        ),
    ]
    if sender_context:
        paragraphs.append(sender_context)
    paragraphs.extend([request.call_to_action, "Best,\n{senderName}"])
    return subject, "\n\n".join(paragraphs)


def save_outreach(
    session: Session,
    *,
    account: Account,
    signal: Signal,
    channel: str,
    subject: str | None,
    body: str,
    generated_by: str,
) -> OutreachDraft:
    draft = OutreachDraft(
        account_id=account.id,
        signal_id=signal.id,
        channel=channel,
        subject=subject,
        body=body,
        generated_by=generated_by,
    )
    session.add(draft)
    session.commit()
    session.refresh(draft)
    return draft
