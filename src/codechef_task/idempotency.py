"""Helpers for durable, transaction-scoped Idempotency-Key handling."""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from codechef_task.models import IdempotencyRecord

_KEY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


@dataclass(frozen=True)
class CachedResponse:
    status_code: int
    body: dict[str, Any]


def validate_idempotency_key(key: str | None) -> None:
    """Accept 1-128 ASCII token characters; reject empty/malformed values."""
    if key is not None and _KEY_PATTERN.fullmatch(key) is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "Idempotency-Key must be 1-128 characters and contain only "
                "letters, digits, '.', '_', ':', or '-'."
            ),
        )


def request_fingerprint(**parameters: Any) -> str:
    """Hash a stable JSON encoding of the logical operation parameters."""
    canonical = json.dumps(
        parameters, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def claim_idempotency_key(
    session: AsyncSession,
    *,
    operation: str,
    key: str,
    fingerprint: str,
) -> IdempotencyRecord:
    """Claim a new key via insert, avoiding locks on a missing-key gap."""
    record = IdempotencyRecord(
        operation=operation,
        key=key,
        request_fingerprint=fingerprint,
    )
    session.add(record)
    # A competing transaction using the same unique key waits here. If it
    # commits first, this insert raises IntegrityError and the caller reloads
    # the now-durable response after rollback. Inserting directly avoids the
    # gap locks created by SELECT ... FOR UPDATE when no record exists.
    await session.flush()
    return record


def store_response(
    record: IdempotencyRecord | None,
    *,
    status_code: int,
    body: dict[str, Any],
) -> None:
    """Attach the successful route response before the same txn commits."""
    if record is None:
        return
    record.response_status = status_code
    record.response_body = body


async def read_after_duplicate(
    session: AsyncSession,
    *,
    operation: str,
    key: str,
    fingerprint: str,
) -> CachedResponse | None:
    """Resolve a concurrent unique-key insert after transaction rollback."""
    await session.rollback()
    async with session.begin():
        record = await session.scalar(
            select(IdempotencyRecord)
            .where(
                IdempotencyRecord.operation == operation,
                IdempotencyRecord.key == key,
            )
            .with_for_update()
        )
        if record is None:
            return None
        if record.request_fingerprint != fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Idempotency-Key was already used with a different request",
            )
        if record.response_status is None or record.response_body is None:
            raise HTTPException(
                status_code=409,
                detail="Idempotency-Key has no completed response",
            )
        return CachedResponse(record.response_status, record.response_body)
