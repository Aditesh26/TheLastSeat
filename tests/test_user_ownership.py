import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from codechef_task.app import app
from codechef_task.database import get_db_session
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
from codechef_task.schema import initialize_schema


def test_user_header_ownership_booking_history_idempotency_and_legacy_rows() -> None:
    async def scenario() -> None:
        test_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        await initialize_schema(test_engine)
        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

        async def provide_test_session():
            async with session_factory() as session:
                yield session

        app.dependency_overrides[get_db_session] = provide_test_session
        try:
            async with session_factory() as session:
                event = Event(
                    slug="user-ownership-test",
                    name="Ownership Test",
                    ticket_price=Decimal("15.00"),
                    currency="USD",
                    status=EventStatus.OPEN.value,
                )
                session.add(event)
                await session.flush()
                seats = [Seat(event_id=event.id, seat_number=n) for n in range(1, 9)]
                session.add_all(seats)
                await session.flush()
                now = datetime.now(UTC)
                holds = [
                    Reservation(
                        event_id=event.id,
                        seat_id=seats[i].id,
                        user_ref=owner,
                        status=state,
                        expires_at=now + timedelta(minutes=5),
                    )
                    for i, owner, state in (
                        (0, "alice", ReservationStatus.ACTIVE.value),
                        (1, "bob", ReservationStatus.ACTIVE.value),
                        (2, None, ReservationStatus.ACTIVE.value),
                        (3, "alice", ReservationStatus.ACTIVE.value),
                        (4, None, ReservationStatus.ACTIVE.value),
                        (5, "alice", ReservationStatus.CONVERTED.value),
                        (6, "bob", ReservationStatus.CONVERTED.value),
                        (7, "Alice", ReservationStatus.CONVERTED.value),
                    )
                ]
                session.add_all(holds)
                await session.flush()
                alice_reservation_id = holds[0].id
                bob_reservation_id = holds[1].id
                legacy_reservation_id = holds[2].id
                alice_pending_reservation_id = holds[3].id
                legacy_pending_reservation_id = holds[4].id
                alice_confirmed_reservation_id = holds[5].id
                case_mismatch_reservation_id = holds[7].id

                alice_confirmed = Booking(
                    event_id=event.id,
                    seat_id=seats[5].id,
                    reservation_id=holds[5].id,
                    user_ref="alice",
                    status=BookingStatus.CONFIRMED.value,
                    amount_paid=Decimal("15.00"),
                    currency="USD",
                )
                bob_confirmed = Booking(
                    event_id=event.id,
                    seat_id=seats[6].id,
                    reservation_id=holds[6].id,
                    user_ref="bob",
                    status=BookingStatus.CONFIRMED.value,
                    amount_paid=Decimal("15.00"),
                    currency="USD",
                )
                case_mismatch_booking = Booking(
                    event_id=event.id,
                    seat_id=seats[7].id,
                    reservation_id=holds[7].id,
                    user_ref="Alice",
                    status=BookingStatus.CANCELLED.value,
                    amount_paid=Decimal("0.00"),
                    currency="USD",
                )
                alice_pending = Booking(
                    event_id=event.id,
                    seat_id=seats[3].id,
                    reservation_id=holds[3].id,
                    user_ref="alice",
                    status=BookingStatus.PENDING.value,
                    amount_paid=Decimal("0.00"),
                    currency="USD",
                )
                legacy_pending = Booking(
                    event_id=event.id,
                    seat_id=seats[4].id,
                    reservation_id=holds[4].id,
                    user_ref=None,
                    status=BookingStatus.PENDING.value,
                    amount_paid=Decimal("0.00"),
                    currency="USD",
                )
                session.add_all(
                    [
                        alice_confirmed,
                        bob_confirmed,
                        case_mismatch_booking,
                        alice_pending,
                        legacy_pending,
                    ]
                )
                await session.flush()
                session.add_all(
                    [
                        Payment(
                            booking_id=booking.id,
                            amount=Decimal("15.00"),
                            currency="USD",
                            status=(
                                PaymentStatus.PENDING.value
                                if booking.status == BookingStatus.PENDING.value
                                else PaymentStatus.SUCCEEDED.value
                            ),
                        )
                        for booking in (
                            alice_confirmed,
                            bob_confirmed,
                            case_mismatch_booking,
                            alice_pending,
                            legacy_pending,
                        )
                    ]
                )
                await session.commit()
                alice_confirmed_booking_id = alice_confirmed.id
                event_id = event.id
                seat_ids = [seat.id for seat in seats]
                alice_pending_payment_id = await session.scalar(
                    select(Payment.id).where(Payment.booking_id == alice_pending.id)
                )
                legacy_pending_payment_id = await session.scalar(
                    select(Payment.id).where(Payment.booking_id == legacy_pending.id)
                )

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test",
                headers={"X-User-Ref": "alice"},
            ) as client:
                missing_request = client.build_request(
                    "GET", f"/api/reservations/{alice_reservation_id}"
                )
                missing_request.headers.pop("X-User-Ref", None)
                missing = await client.send(missing_request)
                invalid = await client.get(
                    f"/api/reservations/{alice_reservation_id}",
                    headers={"X-User-Ref": "bad user"},
                )
                assert missing.status_code == 422
                assert invalid.status_code == 422
                missing_booking_cancel = client.build_request(
                    "POST",
                    f"/api/bookings/{alice_confirmed_booking_id}/cancel",
                )
                missing_booking_cancel.headers.pop("X-User-Ref", None)
                invalid_booking_cancel = await client.post(
                    f"/api/bookings/{alice_confirmed_booking_id}/cancel",
                    headers={"X-User-Ref": "bad user"},
                )
                assert (await client.send(missing_booking_cancel)).status_code == 422
                assert invalid_booking_cancel.status_code == 422

                for method, path, body in (
                    ("GET", f"/api/reservations/{alice_reservation_id}", None),
                    ("DELETE", f"/api/reservations/{alice_reservation_id}", None),
                    (
                        "POST",
                        f"/api/reservations/{alice_reservation_id}/checkout",
                        {"outcome": "success"},
                    ),
                    (
                        "POST",
                        f"/api/payments/{alice_pending_payment_id}/settle",
                        {"outcome": "success"},
                    ),
                    (
                        "POST",
                        f"/api/bookings/{alice_confirmed_booking_id}/cancel",
                        None,
                    ),
                ):
                    denied = await client.request(
                        method,
                        path,
                        json=body,
                        headers={"X-User-Ref": "bob"},
                    )
                    assert denied.status_code == 403

                own_hold = await client.get(
                    f"/api/reservations/{alice_reservation_id}"
                )
                assert own_hold.status_code == 200

                own_history = await client.get("/api/users/me/bookings")
                assert own_history.status_code == 200
                own_items = own_history.json()["items"]
                assert {item["reservation_id"] for item in own_items} == {
                    alice_pending_reservation_id,
                    alice_confirmed_reservation_id,
                }
                assert all(item["payment"] is not None for item in own_items)
                assert all(item["reservation_id"] != holds[6].id for item in own_items)

                reservation_url = f"/api/events/{event_id}/reservations"
                reserve_key = "shared-reservation-key"
                first_reservation = await client.post(
                    reservation_url,
                    json={"seat_id": seat_ids[7]},
                    headers={"Idempotency-Key": reserve_key},
                )
                second_reservation = await client.post(
                    reservation_url,
                    json={"seat_id": seat_ids[7]},
                    headers={"X-User-Ref": "bob", "Idempotency-Key": reserve_key},
                )
                assert first_reservation.status_code == 201
                assert second_reservation.status_code == 409
                created_checkout = await client.post(
                    f"/api/reservations/{first_reservation.json()['id']}/checkout",
                    json={"outcome": "success"},
                )
                assert created_checkout.status_code == 201
                async with session_factory() as session:
                    created_booking_owner = await session.scalar(
                        select(Booking.user_ref).where(
                            Booking.id == created_checkout.json()["booking_id"]
                        )
                    )
                assert created_booking_owner == "alice"

                first_checkout = await client.post(
                    f"/api/reservations/{alice_reservation_id}/checkout",
                    json={"outcome": "success"},
                    headers={"Idempotency-Key": "shared-checkout-key"},
                )
                cross_user_checkout = await client.post(
                    f"/api/reservations/{bob_reservation_id}/checkout",
                    json={"outcome": "success"},
                    headers={
                        "X-User-Ref": "bob",
                        "Idempotency-Key": "shared-checkout-key",
                    },
                )
                assert first_checkout.status_code == 201
                assert cross_user_checkout.status_code == 409
                wrong_user_booking_cancel = await client.post(
                    f"/api/bookings/{created_checkout.json()['booking_id']}/cancel",
                    headers={"X-User-Ref": "bob"},
                )
                assert wrong_user_booking_cancel.status_code == 403

                settled = await client.post(
                    f"/api/payments/{legacy_pending_payment_id}/settle",
                    json={"outcome": "success"},
                )
                legacy_hold = await client.get(
                    f"/api/reservations/{legacy_reservation_id}"
                )
                assert settled.status_code == 200
                assert legacy_hold.status_code == 200
                legacy_booking_cancel = await client.post(
                    f"/api/bookings/{settled.json()['booking_id']}/cancel"
                )
                assert legacy_booking_cancel.status_code == 200

                legacy_checkout = await client.post(
                    f"/api/reservations/{legacy_reservation_id}/checkout",
                    json={"outcome": "success"},
                )
                assert legacy_checkout.status_code == 201
        finally:
            app.dependency_overrides.clear()
            await test_engine.dispose()

    asyncio.run(scenario())
