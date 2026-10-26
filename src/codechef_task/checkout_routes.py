"""Atomic reservation checkout with a deterministic payment simulator."""

from datetime import UTC, datetime
from decimal import Decimal
import logging
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

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
    Payment,
    PaymentStatus,
    Reservation,
    ReservationStatus,
    Seat,
)
from codechef_task.reservation_routes import (
    _expire_if_elapsed,
    _fail_pending_checkout,
    _read_reservation_location,
)

router = APIRouter()
logger = logging.getLogger(__name__)


class CheckoutRequest(BaseModel):
    """Select a simulated gateway result; price always comes from the event."""

    outcome: Literal["success", "failure", "delayed"]


class SettlementRequest(BaseModel):
    """Supply the simulated outcome when a pending payment is settled."""

    outcome: Literal["success", "failure"]


class CheckoutRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    booking_id: int
    reservation_id: int
    booking_status: BookingStatus
    payment_id: int
    payment_status: PaymentStatus
    amount: Decimal
    currency: str


def _checkout_response(booking: Booking, payment: Payment) -> CheckoutRead:
    return CheckoutRead(
        booking_id=booking.id,
        reservation_id=booking.reservation_id,
        booking_status=booking.status,
        payment_id=payment.id,
        payment_status=payment.status,
        amount=payment.amount,
        currency=payment.currency,
    )


@router.post(
    "/api/reservations/{reservation_id}/checkout",
    response_model=CheckoutRead,
    status_code=status.HTTP_201_CREATED,
)
async def checkout_reservation(
    payload: CheckoutRequest,
    response: Response,
    reservation_id: int = Path(gt=0),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session: AsyncSession = Depends(get_db_session),
) -> CheckoutRead:
    """Convert an active hold and record one simulated payment atomically."""
    validate_idempotency_key(idempotency_key)
    fingerprint = request_fingerprint(
        reservation_id=reservation_id, outcome=payload.outcome
    )
    event_id, seat_id = await _read_reservation_location(session, reservation_id)
    result: CheckoutRead | None = None
    cached_response: CachedResponse | None = None
    idempotency_record = None
    conflict: str | None = None
    replayed = False

    try:
        async with session.begin():
            if idempotency_key is not None:
                idempotency_record = await claim_idempotency_key(
                    session,
                    operation="reservation.checkout",
                    key=idempotency_key,
                    fingerprint=fingerprint,
                )

            # After claiming a fresh key, use the established seat-first
            # lock protocol. A duplicate key is resolved after rollback below.
            seat = await session.scalar(
                select(Seat)
                .where(Seat.event_id == event_id, Seat.id == seat_id)
                .with_for_update()
            )
            if seat is None:
                raise HTTPException(status_code=404, detail="Reservation not found")
            reservation = await session.scalar(
                select(Reservation)
                .where(Reservation.id == reservation_id)
                .with_for_update()
            )
            if reservation is None:
                raise HTTPException(status_code=404, detail="Reservation not found")

            now = datetime.now(UTC)
            expired_now = await _expire_if_elapsed(session, reservation, now)
            existing_booking = await session.scalar(
                select(Booking).where(Booking.reservation_id == reservation_id)
            )
            if existing_booking is not None:
                existing_payment = await session.scalar(
                    select(Payment).where(Payment.booking_id == existing_booking.id)
                )
                if existing_payment is None:
                    raise HTTPException(
                        status_code=409, detail="Checkout record is incomplete"
                    )
                result = _checkout_response(existing_booking, existing_payment)
                replayed = True
            else:
                if expired_now:
                    conflict = "Reservation has expired"
                elif reservation.status == ReservationStatus.EXPIRED.value:
                    conflict = "Reservation has expired"
                elif reservation.status == ReservationStatus.CANCELLED.value:
                    conflict = "Reservation has been cancelled"
                elif reservation.status == ReservationStatus.CONVERTED.value:
                    conflict = "Reservation has already been consumed"
                elif reservation.status != ReservationStatus.ACTIVE.value:
                    conflict = "Reservation cannot be checked out"
                else:
                    event = await session.scalar(
                        select(Event).where(Event.id == reservation.event_id)
                    )
                    if event is None:
                        raise HTTPException(status_code=404, detail="Event not found")
                    success = payload.outcome == "success"
                    delayed = payload.outcome == "delayed"
                    booking = Booking(
                        event_id=reservation.event_id,
                        seat_id=reservation.seat_id,
                        reservation_id=reservation.id,
                        status=(
                            BookingStatus.CONFIRMED.value
                            if success
                            else BookingStatus.PENDING.value
                            if delayed
                            else BookingStatus.CANCELLED.value
                        ),
                        amount_paid=event.ticket_price if success else Decimal("0.00"),
                        currency=event.currency,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(booking)
                    await session.flush()
                    payment = Payment(
                        booking_id=booking.id,
                        amount=event.ticket_price,
                        currency=event.currency,
                        status=(
                            PaymentStatus.SUCCEEDED.value
                            if success
                            else PaymentStatus.PENDING.value
                            if delayed
                            else PaymentStatus.FAILED.value
                        ),
                        created_at=now,
                        updated_at=now,
                    )
                    if not delayed:
                        reservation.status = (
                            ReservationStatus.CONVERTED.value
                            if success
                            else ReservationStatus.CANCELLED.value
                        )
                        reservation.updated_at = now
                    session.add(payment)
                    await session.flush()
                    result = _checkout_response(booking, payment)

            if conflict is not None and idempotency_record is not None:
                await session.delete(idempotency_record)
            elif result is not None:
                response_status = (
                    status.HTTP_200_OK if replayed else status.HTTP_201_CREATED
                )
                store_response(
                    idempotency_record,
                    status_code=response_status,
                    body=result.model_dump(mode="json"),
                )
    except IntegrityError:
        # Any failed insert rolls back the reservation transition and its
        # booking/payment rows together.
        if idempotency_key is not None:
            cached_response = await read_after_duplicate(
                session,
                operation="reservation.checkout",
                key=idempotency_key,
                fingerprint=fingerprint,
            )
            if cached_response is not None:
                response.status_code = cached_response.status_code
                return CheckoutRead.model_validate(cached_response.body)
        raise HTTPException(status_code=409, detail="Checkout could not be completed") from None

    if conflict is not None:
        raise HTTPException(status_code=409, detail=conflict)
    assert result is not None
    if replayed:
        response.status_code = status.HTTP_200_OK
    logger.info(
        "checkout.completed reservation_id=%s booking_id=%s payment_id=%s "
        "booking_status=%s payment_status=%s requested_outcome=%s",
        result.reservation_id,
        result.booking_id,
        result.payment_id,
        result.booking_status.value,
        result.payment_status.value,
        payload.outcome,
    )
    return result


@router.post(
    "/api/payments/{payment_id}/settle", response_model=CheckoutRead
)
async def settle_delayed_payment(
    payload: SettlementRequest,
    payment_id: int = Path(gt=0),
    session: AsyncSession = Depends(get_db_session),
) -> CheckoutRead:
    """Settle a persisted pending payment without a worker or sleeping request."""
    location = (
        await session.execute(
            select(
                Booking.id,
                Booking.event_id,
                Booking.seat_id,
                Booking.reservation_id,
            )
            .join(Payment, Payment.booking_id == Booking.id)
            .where(Payment.id == payment_id)
        )
    ).one_or_none()
    if location is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    booking_id, event_id, seat_id, reservation_id = location
    await session.commit()
    if reservation_id is None:
        raise HTTPException(status_code=409, detail="Payment has no reservation")

    result: CheckoutRead | None = None
    conflict: str | None = None
    try:
        async with session.begin():
            seat = await session.scalar(
                select(Seat)
                .where(Seat.event_id == event_id, Seat.id == seat_id)
                .with_for_update()
            )
            if seat is None:
                raise HTTPException(status_code=404, detail="Payment not found")
            reservation = await session.scalar(
                select(Reservation)
                .where(Reservation.id == reservation_id)
                .with_for_update()
            )
            if reservation is None:
                raise HTTPException(status_code=404, detail="Reservation not found")

            expired_now = await _expire_if_elapsed(
                session, reservation, datetime.now(UTC)
            )
            booking = await session.scalar(
                select(Booking).where(Booking.id == booking_id).with_for_update()
            )
            payment = await session.scalar(
                select(Payment).where(Payment.id == payment_id).with_for_update()
            )
            if booking is None or payment is None:
                raise HTTPException(status_code=404, detail="Payment not found")

            if payment.status != PaymentStatus.PENDING.value:
                result = _checkout_response(booking, payment)
            elif expired_now or reservation.status == ReservationStatus.EXPIRED.value:
                conflict = "Reservation expired before payment settlement"
            elif reservation.status != ReservationStatus.ACTIVE.value:
                await _fail_pending_checkout(session, reservation, datetime.now(UTC))
                conflict = "Reservation is no longer active"
            else:
                now = datetime.now(UTC)
                if payload.outcome == "success":
                    booking.status = BookingStatus.CONFIRMED.value
                    booking.amount_paid = payment.amount
                    reservation.status = ReservationStatus.CONVERTED.value
                    payment.status = PaymentStatus.SUCCEEDED.value
                else:
                    booking.status = BookingStatus.CANCELLED.value
                    reservation.status = ReservationStatus.CANCELLED.value
                    payment.status = PaymentStatus.FAILED.value
                booking.updated_at = now
                reservation.updated_at = now
                payment.updated_at = now
                await session.flush()
                result = _checkout_response(booking, payment)
    except IntegrityError:
        raise HTTPException(
            status_code=409, detail="Payment settlement could not be completed"
        ) from None

    if conflict is not None:
        raise HTTPException(status_code=409, detail=conflict)
    assert result is not None
    logger.info(
        "payment.settled payment_id=%s booking_id=%s reservation_id=%s "
        "booking_status=%s payment_status=%s requested_outcome=%s",
        result.payment_id,
        result.booking_id,
        result.reservation_id,
        result.booking_status.value,
        result.payment_status.value,
        payload.outcome,
    )
    return result
