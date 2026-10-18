import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, text

from codechef_task.app import app
from codechef_task.database import SessionLocal, engine
from codechef_task.models import (
    Booking,
    Event,
    EventStatus,
    IdempotencyRecord,
    Payment,
    Reservation,
    Seat,
)
from codechef_task.migrate import apply_checkout_constraints
from codechef_task.schema import initialize_schema


def test_concurrent_reservations_are_safe_on_mariadb() -> None:
    async def scenario() -> None:
        await engine.dispose()
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception as exc:
            pytest.skip(f"MariaDB unavailable ({type(exc).__name__})")

        await initialize_schema(engine)
        await apply_checkout_constraints(engine)
        async with engine.connect() as connection:
            table_engines = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT TABLE_NAME, ENGINE "
                            "FROM information_schema.TABLES "
                            "WHERE TABLE_SCHEMA = DATABASE() "
                            "AND TABLE_NAME IN ('seats', 'reservations', "
                            "'idempotency_records')"
                        )
                    )
                ).all()
            )
        assert table_engines.get("seats", "").lower() == "innodb"
        assert table_engines.get("reservations", "").lower() == "innodb"
        assert table_engines.get("idempotency_records", "").lower() == "innodb"
        async with engine.connect() as connection:
            key_index_columns = list(
                (
                    await connection.execute(
                        text(
                            "SELECT COLUMN_NAME, NON_UNIQUE "
                            "FROM information_schema.STATISTICS "
                            "WHERE TABLE_SCHEMA = DATABASE() "
                            "AND TABLE_NAME = 'idempotency_records' "
                            "AND INDEX_NAME = 'uq_idempotency_operation_key' "
                            "ORDER BY SEQ_IN_INDEX"
                        )
                    )
                ).all()
            )
        assert key_index_columns == [("operation", 0), ("key", 0)]

        event_id: int | None = None
        test_keys: list[str] = []
        try:
            async with SessionLocal() as session:
                event = Event(
                    slug=f"m3-concurrency-{uuid4().hex}",
                    name="Reservation Concurrency Test",
                    ticket_price=Decimal("10.00"),
                    currency="USD",
                    status=EventStatus.OPEN.value,
                )
                session.add(event)
                await session.flush()
                event_id = event.id
                seats = [Seat(event_id=event.id, seat_number=n) for n in range(1, 10)]
                session.add_all(seats)
                await session.flush()
                now = datetime.now(UTC)
                old_time = now - timedelta(seconds=1)
                expired = Reservation(
                    event_id=event.id,
                    seat_id=seats[3].id,
                    status="active",
                    created_at=old_time - timedelta(minutes=5),
                    updated_at=old_time - timedelta(minutes=5),
                    expires_at=old_time,
                )
                active_to_cancel = Reservation(
                    event_id=event.id,
                    seat_id=seats[5].id,
                    status="active",
                    created_at=now,
                    updated_at=now,
                    expires_at=now + timedelta(minutes=5),
                )
                expired_to_race = Reservation(
                    event_id=event.id,
                    seat_id=seats[6].id,
                    status="active",
                    created_at=old_time - timedelta(minutes=5),
                    updated_at=old_time - timedelta(minutes=5),
                    expires_at=old_time,
                )
                session.add_all(
                    [
                        expired,
                        active_to_cancel,
                        expired_to_race,
                        Booking(
                            event_id=event.id,
                            seat_id=seats[4].id,
                            status="confirmed",
                            amount_paid=Decimal("10.00"),
                            currency="USD",
                        ),
                    ]
                )
                await session.flush()
                expired_id = expired.id
                active_to_cancel_id = active_to_cancel.id
                expired_to_race_id = expired_to_race.id
                await session.commit()
                seat_ids = [seat.id for seat in seats]

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                same_seat = await asyncio.gather(
                    client.post(
                        f"/api/events/{event_id}/reservations",
                        json={"seat_id": seat_ids[0]},
                    ),
                    client.post(
                        f"/api/events/{event_id}/reservations",
                        json={"seat_id": seat_ids[0]},
                    ),
                )
                assert sorted(response.status_code for response in same_seat) == [201, 409]

                # MariaDB's unique-key wait resolves concurrent identical
                # idempotency claims to the same durable reservation response.
                reservation_key = f"mariadb-reserve-{uuid4().hex}"
                test_keys.append(reservation_key)
                same_key_reservations = await asyncio.gather(
                    client.post(
                        f"/api/events/{event_id}/reservations",
                        json={"seat_id": seat_ids[8]},
                        headers={"Idempotency-Key": reservation_key},
                    ),
                    client.post(
                        f"/api/events/{event_id}/reservations",
                        json={"seat_id": seat_ids[8]},
                        headers={"Idempotency-Key": reservation_key},
                    ),
                )
                assert [response.status_code for response in same_key_reservations] == [
                    201,
                    201,
                ]
                assert same_key_reservations[0].json() == same_key_reservations[1].json()
                keyed_reservation_id = same_key_reservations[0].json()["id"]
                checkout_key = f"mariadb-checkout-{uuid4().hex}"
                test_keys.append(checkout_key)
                same_key_checkouts = await asyncio.gather(
                    client.post(
                        f"/api/reservations/{keyed_reservation_id}/checkout",
                        json={"outcome": "success"},
                        headers={"Idempotency-Key": checkout_key},
                    ),
                    client.post(
                        f"/api/reservations/{keyed_reservation_id}/checkout",
                        json={"outcome": "success"},
                        headers={"Idempotency-Key": checkout_key},
                    ),
                )
                assert [response.status_code for response in same_key_checkouts] == [
                    201,
                    201,
                ]
                assert same_key_checkouts[0].json() == same_key_checkouts[1].json()

                different_seats = await asyncio.gather(
                    client.post(
                        f"/api/events/{event_id}/reservations",
                        json={"seat_id": seat_ids[1]},
                    ),
                    client.post(
                        f"/api/events/{event_id}/reservations",
                        json={"seat_id": seat_ids[2]},
                    ),
                )
                assert [response.status_code for response in different_seats] == [201, 201]

                cancelled = await client.delete(
                    f"/api/reservations/{different_seats[0].json()['id']}"
                )
                assert cancelled.status_code == 200
                replacement = await client.post(
                    f"/api/events/{event_id}/reservations",
                    json={"seat_id": seat_ids[1]},
                )
                assert replacement.status_code == 201

                # Concurrent retries for one reservation serialize on the
                # seat row and resolve to the same persisted booking/payment.
                reservation_id = replacement.json()["id"]
                checkouts = await asyncio.gather(
                    client.post(
                        f"/api/reservations/{reservation_id}/checkout",
                        json={"outcome": "success"},
                    ),
                    client.post(
                        f"/api/reservations/{reservation_id}/checkout",
                        json={"outcome": "success"},
                    ),
                )
                assert sorted(response.status_code for response in checkouts) == [
                    200,
                    201,
                ]
                assert checkouts[0].json()["booking_id"] == checkouts[1].json()[
                    "booking_id"
                ]
                assert checkouts[0].json()["payment_id"] == checkouts[1].json()[
                    "payment_id"
                ]

                delayed_hold = await client.post(
                    f"/api/events/{event_id}/reservations",
                    json={"seat_id": seat_ids[7]},
                )
                delayed_checkout = await client.post(
                    f"/api/reservations/{delayed_hold.json()['id']}/checkout",
                    json={"outcome": "delayed"},
                )
                payment_id = delayed_checkout.json()["payment_id"]
                settlements = await asyncio.gather(
                    client.post(
                        f"/api/payments/{payment_id}/settle",
                        json={"outcome": "success"},
                    ),
                    client.post(
                        f"/api/payments/{payment_id}/settle",
                        json={"outcome": "success"},
                    ),
                )
                assert [response.status_code for response in settlements] == [200, 200]
                assert settlements[0].json() == settlements[1].json()

                reclaimed = await client.post(
                    f"/api/events/{event_id}/reservations",
                    json={"seat_id": seat_ids[3]},
                )
                assert reclaimed.status_code == 201
                expired_record = await client.get(f"/api/reservations/{expired_id}")
                assert expired_record.status_code == 200
                assert expired_record.json()["status"] == "expired"

                booked = await client.post(
                    f"/api/events/{event_id}/reservations",
                    json={"seat_id": seat_ids[4]},
                )
                assert booked.status_code == 409

                cancel_and_create = await asyncio.gather(
                    client.delete(f"/api/reservations/{active_to_cancel_id}"),
                    client.post(
                        f"/api/events/{event_id}/reservations",
                        json={"seat_id": seat_ids[5]},
                    ),
                )
                assert cancel_and_create[0].status_code == 200
                assert cancel_and_create[1].status_code in (201, 409)

                expire_and_create = await asyncio.gather(
                    client.get(f"/api/reservations/{expired_to_race_id}"),
                    client.post(
                        f"/api/events/{event_id}/reservations",
                        json={"seat_id": seat_ids[6]},
                    ),
                )
                assert expire_and_create[0].status_code == 200
                assert expire_and_create[0].json()["status"] == "expired"
                assert expire_and_create[1].status_code == 201

            async with SessionLocal() as session:
                active_count = await session.scalar(
                    select(func.count(Reservation.id)).where(
                        Reservation.event_id == event_id,
                        Reservation.seat_id == seat_ids[5],
                        Reservation.status == "active",
                    )
                )
                assert active_count <= 1
                booking_count = await session.scalar(
                    select(func.count(Booking.id)).where(
                        Booking.reservation_id == reservation_id
                    )
                )
                payment_count = await session.scalar(
                    select(func.count(Payment.id)).where(
                        Payment.booking_id == checkouts[0].json()["booking_id"]
                    )
                )
                assert booking_count == payment_count == 1
                delayed_booking_count = await session.scalar(
                    select(func.count(Booking.id)).where(
                        Booking.reservation_id == delayed_hold.json()["id"]
                    )
                )
                delayed_payment_count = await session.scalar(
                    select(func.count(Payment.id)).where(Payment.id == payment_id)
                )
                assert delayed_booking_count == delayed_payment_count == 1
                keyed_reservation_count = await session.scalar(
                    select(func.count(Reservation.id)).where(
                        Reservation.id == keyed_reservation_id
                    )
                )
                keyed_booking_count = await session.scalar(
                    select(func.count(Booking.id)).where(
                        Booking.reservation_id == keyed_reservation_id
                    )
                )
                keyed_payment_count = await session.scalar(
                    select(func.count(Payment.id)).where(
                        Payment.booking_id
                        == same_key_checkouts[0].json()["booking_id"]
                    )
                )
                assert keyed_reservation_count == 1
                assert keyed_booking_count == keyed_payment_count == 1
        finally:
            if event_id is not None or test_keys:
                async with SessionLocal.begin() as session:
                    if event_id is not None:
                        await session.execute(delete(Event).where(Event.id == event_id))
                    if test_keys:
                        await session.execute(
                            delete(IdempotencyRecord).where(
                                IdempotencyRecord.key.in_(test_keys)
                            )
                        )
            await engine.dispose()

    asyncio.run(scenario())
