"""Event seat inventory, booking history, and confirmed-booking cancellation."""

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from codechef_task.database import get_db_session
from codechef_task.models import (
    Booking,
    BookingStatus,
    Event,
    Payment,
    PaymentStatus,
    Reservation,
    ReservationStatus,
    Seat,
)
from codechef_task.reservation_routes import (
    _as_utc,
    _expire_if_elapsed,
    _lock_reservation_and_seat,
    _read_reservation_location,
)
from codechef_task.user_identity import ensure_owner, require_user_ref

router = APIRouter()


class SeatInventoryItem(BaseModel):
    seat_id: int
    seat_number: int
    status: str


class SeatInventoryRead(BaseModel):
    event_id: int
    seats: list[SeatInventoryItem]


class PaymentHistoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: PaymentStatus
    amount: Decimal
    currency: str
    created_at: datetime


class BookingHistoryItem(BaseModel):
    id: int
    event_id: int
    seat_id: int
    reservation_id: int | None
    status: BookingStatus
    amount_paid: Decimal
    currency: str
    created_at: datetime
    payment: PaymentHistoryRead | None


class EventBookingHistoryRead(BaseModel):
    event_id: int
    items: list[BookingHistoryItem]


class UserBookingHistoryRead(BaseModel):
    items: list[BookingHistoryItem]


def _booking_item(booking: Booking, payment: Payment | None) -> BookingHistoryItem:
    return BookingHistoryItem(
        id=booking.id,
        event_id=booking.event_id,
        seat_id=booking.seat_id,
        reservation_id=booking.reservation_id,
        status=booking.status,
        amount_paid=booking.amount_paid,
        currency=booking.currency,
        created_at=_as_utc(booking.created_at),
        payment=(
            PaymentHistoryRead(
                id=payment.id,
                status=payment.status,
                amount=payment.amount,
                currency=payment.currency,
                created_at=_as_utc(payment.created_at),
            )
            if payment is not None
            else None
        ),
    )


@router.get("/api/events/{event_id}/seats", response_model=SeatInventoryRead)
async def list_event_seats(
    event_id: int = Path(gt=0),
    session: AsyncSession = Depends(get_db_session),
) -> SeatInventoryRead:
    """List seats and materialize any elapsed active holds as expired."""
    event_exists = await session.scalar(select(Event.id).where(Event.id == event_id))
    if event_exists is None:
        raise HTTPException(status_code=404, detail="Event not found")
    await session.commit()

    now = datetime.now(UTC)
    expired_ids = list(
        await session.scalars(
            select(Reservation.id).where(
                Reservation.event_id == event_id,
                Reservation.status == ReservationStatus.ACTIVE.value,
                Reservation.expires_at <= now,
            )
        )
    )
    await session.commit()

    # Reuse the reservation endpoints' seat-first lock order for each due hold.
    for reservation_id in expired_ids:
        try:
            location_event_id, seat_id, _ = await _read_reservation_location(
                session, reservation_id
            )
        except HTTPException as exc:
            if exc.status_code == 404:
                continue
            raise
        async with session.begin():
            reservation = await _lock_reservation_and_seat(
                session, reservation_id, location_event_id, seat_id
            )
            await _expire_if_elapsed(session, reservation, datetime.now(UTC))

    async with session.begin():
        seats = list(
            await session.scalars(
                select(Seat)
                .where(Seat.event_id == event_id)
                .order_by(Seat.seat_number)
            )
        )
        active_seat_ids = set(
            await session.scalars(
                select(Reservation.seat_id).where(
                    Reservation.event_id == event_id,
                    Reservation.status == ReservationStatus.ACTIVE.value,
                    Reservation.expires_at > datetime.now(UTC),
                )
            )
        )
        confirmed_seat_ids = set(
            await session.scalars(
                select(Booking.seat_id).where(
                    Booking.event_id == event_id,
                    Booking.status == BookingStatus.CONFIRMED.value,
                )
            )
        )

    return SeatInventoryRead(
        event_id=event_id,
        seats=[
            SeatInventoryItem(
                seat_id=seat.id,
                seat_number=seat.seat_number,
                status=(
                    "booked"
                    if seat.id in confirmed_seat_ids
                    else "reserved"
                    if seat.id in active_seat_ids
                    else "available"
                ),
            )
            for seat in seats
        ],
    )


# Keep the original path denied until it can be protected by real staff auth.
@router.get(
    "/api/events/{event_id}/bookings",
    status_code=403,
    responses={
        403: {
            "description": "Event-wide history requires authenticated staff access"
        }
    },
)
async def list_event_bookings(
    event_id: int = Path(gt=0),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    """Block unscoped booking/payment history without authenticated access."""
    raise HTTPException(
        status_code=403,
        detail="Event-wide booking history is unavailable without authenticated access",
    )


@router.get(
    "/api/events/{event_id}/bookings/all",
    response_model=EventBookingHistoryRead,
)
async def list_all_event_bookings(
    event_id: int = Path(gt=0),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> EventBookingHistoryRead:
    """Return every booking and payment for an event without authentication."""
    if await session.scalar(select(Event.id).where(Event.id == event_id)) is None:
        raise HTTPException(status_code=404, detail="Event not found")
    await session.commit()
    rows = (
        await session.execute(
            select(Booking, Payment)
            .outerjoin(Payment, Payment.booking_id == Booking.id)
            .where(Booking.event_id == event_id)
            .order_by(Booking.created_at.desc(), Booking.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return EventBookingHistoryRead(
        event_id=event_id,
        items=[_booking_item(booking, payment) for booking, payment in rows],
    )


@router.get("/api/users/me/bookings", response_model=UserBookingHistoryRead)
async def list_user_bookings(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db_session),
    user_ref: str = Depends(require_user_ref),
) -> UserBookingHistoryRead:
    """Return paginated booking/payment history for the identified caller."""
    rows = (
        await session.execute(
            select(Booking, Payment)
            .outerjoin(Payment, Payment.booking_id == Booking.id)
            .where(Booking.user_ref == user_ref)
            .order_by(Booking.created_at.desc(), Booking.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return UserBookingHistoryRead(
        items=[_booking_item(booking, payment) for booking, payment in rows]
    )


async def _read_booking_location(
    session: AsyncSession, booking_id: int
) -> tuple[int, int]:
    location = (
        await session.execute(
            select(Booking.event_id, Booking.seat_id).where(Booking.id == booking_id)
        )
    ).one_or_none()
    if location is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    await session.commit()
    return location


@router.post(
    "/api/bookings/{booking_id}/cancel", response_model=BookingHistoryItem
)
async def cancel_confirmed_booking(
    booking_id: int = Path(gt=0),
    session: AsyncSession = Depends(get_db_session),
    user_ref: str = Depends(require_user_ref),
) -> BookingHistoryItem:
    """Cancel a confirmed booking while preserving its historical records."""
    event_id, seat_id = await _read_booking_location(session, booking_id)
    result: BookingHistoryItem | None = None
    conflict: str | None = None
    async with session.begin():
        seat = await session.scalar(
            select(Seat)
            .where(Seat.event_id == event_id, Seat.id == seat_id)
            .with_for_update()
        )
        if seat is None:
            raise HTTPException(status_code=404, detail="Booking not found")
        booking = await session.scalar(
            select(Booking).where(Booking.id == booking_id).with_for_update()
        )
        if booking is None:
            raise HTTPException(status_code=404, detail="Booking not found")
        ensure_owner(booking.user_ref, user_ref)
        payment = await session.scalar(
            select(Payment).where(Payment.booking_id == booking.id)
        )
        if booking.status == BookingStatus.CANCELLED.value:
            result = _booking_item(booking, payment)
        elif booking.status != BookingStatus.CONFIRMED.value:
            conflict = "Only a confirmed booking can be cancelled"
        else:
            now = datetime.now(UTC)
            booking.status = BookingStatus.CANCELLED.value
            booking.updated_at = now
            await session.flush()
            result = _booking_item(booking, payment)

    if conflict is not None:
        raise HTTPException(status_code=409, detail=conflict)
    assert result is not None
    return result
