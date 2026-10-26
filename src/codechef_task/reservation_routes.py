# HTTP endpoints for the temporary reservation lifecycle.

from datetime import UTC, datetime, timedelta
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from codechef_task.config import settings
from codechef_task.database import get_db_session
from codechef_task.idempotency import (
    CachedResponse,
    claim_idempotency_key,
    read_after_duplicate,
    request_fingerprint,
    store_response,
    validate_idempotency_key,
)
from codechef_task.models import (
    Booking,
    BookingStatus,
    Event,
    EventStatus,
    Payment,
    PaymentStatus,
    Reservation,
    ReservationStatus,
    Seat,
)

router = APIRouter()
logger = logging.getLogger(__name__)


class ReservationCreate(BaseModel):
    seat_id: int = Field(gt=0)


class ReservationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_id: int
    seat_id: int
    status: ReservationStatus
    created_at: datetime
    expires_at: datetime


def _as_utc(value: datetime) -> datetime:
    """Interpret MariaDB DATETIME values as UTC and normalize aware values."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _reservation_response(reservation: Reservation) -> ReservationRead:
    return ReservationRead(
        id=reservation.id,
        event_id=reservation.event_id,
        seat_id=reservation.seat_id,
        status=reservation.status,
        created_at=_as_utc(reservation.created_at),
        expires_at=_as_utc(reservation.expires_at),
    )


async def _expire_if_elapsed(
    session: AsyncSession, reservation: Reservation, now: datetime
) -> bool:
    """Persist expiration and free the generated active-seat key when due."""
    if (
        reservation.status == ReservationStatus.ACTIVE.value
        and _as_utc(reservation.expires_at) <= now
    ):
        reservation.status = ReservationStatus.EXPIRED.value
        reservation.updated_at = now
        await _fail_pending_checkout(session, reservation, now)
        await session.flush()
        logger.info(
            "reservation.expired reservation_id=%s event_id=%s seat_id=%s",
            reservation.id,
            reservation.event_id,
            reservation.seat_id,
        )
        return True
    return False


async def _fail_pending_checkout(
    session: AsyncSession, reservation: Reservation, now: datetime
) -> None:
    """Fail delayed payment if its active reservation is cancelled or expires."""
    pending = (
        await session.execute(
            select(Booking, Payment)
            .join(Payment, Payment.booking_id == Booking.id)
            .where(
                Booking.reservation_id == reservation.id,
                Payment.status == PaymentStatus.PENDING.value,
            )
            .with_for_update()
        )
    ).one_or_none()
    if pending is None:
        return
    booking, payment = pending
    booking.status = BookingStatus.CANCELLED.value
    booking.updated_at = now
    payment.status = PaymentStatus.FAILED.value
    payment.updated_at = now


async def _read_reservation_location(
    session: AsyncSession, reservation_id: int
) -> tuple[int, int]:
    """Find immutable seat coordinates before starting the lifecycle transaction."""
    location = (
        await session.execute(
            select(Reservation.event_id, Reservation.seat_id).where(
                Reservation.id == reservation_id
            )
        )
    ).one_or_none()
    if location is None:
        raise HTTPException(status_code=404, detail="Reservation not found")
    # End the lookup transaction so a later InnoDB locking read does not use a
    # repeatable-read snapshot created before it waited for the seat row.
    await session.commit()
    return location


async def _lock_reservation_and_seat(
    session: AsyncSession,
    reservation_id: int,
    event_id: int,
    seat_id: int,
) -> Reservation:
    """Lock the seat then reservation, consistently across lifecycle writes."""
    seat = await session.scalar(
        select(Seat)
        .where(Seat.event_id == event_id, Seat.id == seat_id)
        .with_for_update()
    )
    if seat is None:
        raise HTTPException(status_code=404, detail="Reservation not found")

    reservation = await session.scalar(
        select(Reservation).where(Reservation.id == reservation_id).with_for_update()
    )
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")
    return reservation


@router.post(
    "/api/events/{event_id}/reservations",
    response_model=ReservationRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_reservation(
    payload: ReservationCreate,
    http_response: Response,
    event_id: int = Path(gt=0),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session: AsyncSession = Depends(get_db_session),
) -> ReservationRead:
    """Temporarily hold an available seat, serializing attempts on its seat row."""
    validate_idempotency_key(idempotency_key)
    fingerprint = request_fingerprint(event_id=event_id, seat_id=payload.seat_id)
    response: ReservationRead | None = None
    cached_response: CachedResponse | None = None
    idempotency_record = None
    conflict: str | None = None
    try:
        async with session.begin():
            if idempotency_key is not None:
                idempotency_record = await claim_idempotency_key(
                    session,
                    operation="reservation.create",
                    key=idempotency_key,
                    fingerprint=fingerprint,
                )

            # This existing InnoDB row is the mutex for this event/seat. Every
            # reservation lifecycle operation locks it before touching a hold.
            seat = await session.scalar(
                select(Seat)
                .where(Seat.event_id == event_id, Seat.id == payload.seat_id)
                .with_for_update()
            )
            if seat is None:
                event_exists = await session.scalar(
                    select(Event.id).where(Event.id == event_id)
                )
                if event_exists is None:
                    raise HTTPException(status_code=404, detail="Event not found")
                raise HTTPException(
                    status_code=404, detail="Seat not found for this event"
                )

            # This is the first consistent read in the transaction, after any
            # wait for the seat lock, so it observes committed state.
            event = await session.scalar(select(Event).where(Event.id == event_id))
            if event is None:
                raise HTTPException(status_code=404, detail="Event not found")
            if event.status != EventStatus.OPEN.value:
                raise HTTPException(
                    status_code=409, detail="Event is not open for reservations"
                )

            now = datetime.now(UTC)
            active_reservation = await session.scalar(
                select(Reservation).where(
                    Reservation.event_id == event_id,
                    Reservation.active_seat_id == seat.id,
                )
            )
            if active_reservation is not None:
                await _expire_if_elapsed(session, active_reservation, now)
                if active_reservation.status == ReservationStatus.ACTIVE.value:
                    conflict = "Seat is held by an active reservation"

            if conflict is None:
                confirmed_booking_id = await session.scalar(
                    select(Booking.id)
                    .where(
                        Booking.event_id == event_id,
                        Booking.seat_id == seat.id,
                        Booking.status == BookingStatus.CONFIRMED.value,
                    )
                    .limit(1)
                )
                if confirmed_booking_id is not None:
                    conflict = "Seat already has a confirmed booking"

            if conflict is None:
                reservation = Reservation(
                    event_id=event_id,
                    seat_id=seat.id,
                    status=ReservationStatus.ACTIVE.value,
                    created_at=now,
                    updated_at=now,
                    expires_at=now
                    + timedelta(seconds=settings.reservation_hold_seconds),
                )
                session.add(reservation)
                await session.flush()
                await session.refresh(reservation)
                reservation_response = _reservation_response(reservation)
                store_response(
                    idempotency_record,
                    status_code=status.HTTP_201_CREATED,
                    body=reservation_response.model_dump(mode="json"),
                )
                response = reservation_response
            elif idempotency_record is not None:
                # Business conflicts are not cached; a later retry may be
                # valid after inventory changes.
                await session.delete(idempotency_record)
    except IntegrityError:
        # The context manager has rolled back before this handler is reached.
        if idempotency_key is not None:
            cached_response = await read_after_duplicate(
                session,
                operation="reservation.create",
                key=idempotency_key,
                fingerprint=fingerprint,
            )
            if cached_response is not None:
                http_response.status_code = cached_response.status_code
                return ReservationRead.model_validate(cached_response.body)
        raise HTTPException(
            status_code=409, detail="Seat is no longer available"
        ) from None

    # Commit any lazy expiration before reporting a later availability conflict.
    if conflict is not None:
        raise HTTPException(status_code=409, detail=conflict)
    assert response is not None
    logger.info(
        "reservation.created reservation_id=%s event_id=%s seat_id=%s expires_at=%s",
        response.id,
        response.event_id,
        response.seat_id,
        response.expires_at.isoformat(),
    )
    return response


@router.get("/api/reservations/{reservation_id}", response_model=ReservationRead)
async def get_reservation(
    reservation_id: int = Path(gt=0),
    session: AsyncSession = Depends(get_db_session),
) -> ReservationRead:
    """Return current status and persist expiration when the hold has elapsed."""
    event_id, seat_id = await _read_reservation_location(session, reservation_id)
    async with session.begin():
        reservation = await _lock_reservation_and_seat(
            session, reservation_id, event_id, seat_id
        )
        await _expire_if_elapsed(session, reservation, datetime.now(UTC))
        return _reservation_response(reservation)


@router.delete("/api/reservations/{reservation_id}", response_model=ReservationRead)
async def cancel_reservation(
    reservation_id: int = Path(gt=0),
    session: AsyncSession = Depends(get_db_session),
) -> ReservationRead:
    """Cancel a current hold; terminal reservation states cannot be reactivated."""
    event_id, seat_id = await _read_reservation_location(session, reservation_id)
    response: ReservationRead | None = None
    conflict: str | None = None
    async with session.begin():
        reservation = await _lock_reservation_and_seat(
            session, reservation_id, event_id, seat_id
        )
        now = datetime.now(UTC)
        expired = await _expire_if_elapsed(session, reservation, now)
        if expired:
            conflict = "Reservation has expired"
        elif reservation.status != ReservationStatus.ACTIVE.value:
            conflict = "Only an active reservation can be cancelled"
        else:
            reservation.status = ReservationStatus.CANCELLED.value
            reservation.updated_at = now
            await _fail_pending_checkout(session, reservation, now)
            await session.flush()
            response = _reservation_response(reservation)

    if conflict is not None:
        raise HTTPException(status_code=409, detail=conflict)
    assert response is not None
    logger.info(
        "reservation.cancelled reservation_id=%s event_id=%s seat_id=%s",
        response.id,
        response.event_id,
        response.seat_id,
    )
    return response
