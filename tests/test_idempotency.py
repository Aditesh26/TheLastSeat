import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from codechef_task.app import app
from codechef_task.database import get_db_session
from codechef_task.models import (
    Booking,
    Event,
    EventStatus,
    IdempotencyRecord,
    Reservation,
    Seat,
)
from codechef_task.schema import initialize_schema


def test_idempotency_keys_replay_validate_and_rollback_atomically() -> None:
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
                    slug="idempotency-test-event",
                    name="Idempotency Test",
                    ticket_price=Decimal("18.50"),
                    currency="USD",
                    status=EventStatus.OPEN.value,
                )
                session.add(event)
                await session.flush()
                seats = [Seat(event_id=event.id, seat_number=n) for n in range(1, 5)]
                session.add_all(seats)
                await session.flush()
                event_id = event.id
                seat_ids = [seat.id for seat in seats]
                now = datetime.now(UTC)
                checkout_reservation = Reservation(
                    event_id=event.id,
                    seat_id=seats[1].id,
                    expires_at=now + timedelta(minutes=5),
                )
                legacy_reservation = Reservation(
                    event_id=event.id,
                    seat_id=seats[2].id,
                    expires_at=now + timedelta(minutes=5),
                )
                rollback_reservation = Reservation(
                    event_id=event.id,
                    seat_id=seats[3].id,
                    expires_at=now + timedelta(minutes=5),
                )
                session.add_all(
                    [checkout_reservation, legacy_reservation, rollback_reservation]
                )
                await session.flush()
                checkout_reservation_id = checkout_reservation.id
                legacy_reservation_id = legacy_reservation.id
                rollback_reservation_id = rollback_reservation.id
                await session.commit()

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test",
                headers={"X-User-Ref": "test-user"},
            ) as client:
                reservation_url = f"/api/events/{event_id}/reservations"
                headers = {"Idempotency-Key": "reserve-seat-1"}
                created = await client.post(
                    reservation_url,
                    json={"seat_id": seat_ids[0]},
                    headers=headers,
                )
                assert created.status_code == 201, created.text
                repeated = await client.post(
                    reservation_url,
                    json={"seat_id": seat_ids[0]},
                    headers=headers,
                )
                assert repeated.status_code == 201
                assert repeated.json() == created.json()

                mismatch = await client.post(
                    reservation_url,
                    json={"seat_id": seat_ids[1]},
                    headers=headers,
                )
                assert mismatch.status_code == 409
                assert mismatch.json()["detail"] == (
                    "Idempotency-Key was already used with a different request"
                )

                for invalid_key in ("", "contains spaces", "x" * 129):
                    invalid = await client.post(
                        reservation_url,
                        json={"seat_id": seat_ids[0]},
                        headers={"Idempotency-Key": invalid_key},
                    )
                    assert invalid.status_code == 422

                # Requests without a key keep the old seat-conflict behavior.
                unkeyed = await client.post(
                    reservation_url, json={"seat_id": seat_ids[0]}
                )
                assert unkeyed.status_code == 409

                checkout_url = (
                    f"/api/reservations/{checkout_reservation_id}/checkout"
                )
                checkout_headers = {"Idempotency-Key": "checkout-success-1"}
                checked_out = await client.post(
                    checkout_url,
                    json={"outcome": "success"},
                    headers=checkout_headers,
                )
                assert checked_out.status_code == 201
                checkout_replay = await client.post(
                    checkout_url,
                    json={"outcome": "success"},
                    headers=checkout_headers,
                )
                assert checkout_replay.status_code == 201
                assert checkout_replay.json() == checked_out.json()
                checkout_mismatch = await client.post(
                    checkout_url,
                    json={"outcome": "failure"},
                    headers=checkout_headers,
                )
                assert checkout_mismatch.status_code == 409

                # A different key still follows the existing one-checkout-per-
                # reservation rule and returns the original result.
                legacy_url = f"/api/reservations/{legacy_reservation_id}/checkout"
                first_key_result = await client.post(
                    legacy_url,
                    json={"outcome": "success"},
                    headers={"Idempotency-Key": "checkout-first-key"},
                )
                second_key_result = await client.post(
                    legacy_url,
                    json={"outcome": "failure"},
                    headers={"Idempotency-Key": "checkout-other-key"},
                )
                assert first_key_result.status_code == 201
                assert second_key_result.status_code == 200
                assert second_key_result.json() == first_key_result.json()
                second_key_replay = await client.post(
                    legacy_url,
                    json={"outcome": "failure"},
                    headers={"Idempotency-Key": "checkout-other-key"},
                )
                assert second_key_replay.status_code == 200
                assert second_key_replay.json() == first_key_result.json()

                # The idempotency claim, booking, payment, and reservation
                # transition roll back together if a database write fails.
                async with test_engine.begin() as connection:
                    await connection.execute(
                        text(
                            "CREATE TRIGGER reject_idempotency_payment "
                            "BEFORE INSERT ON payments "
                            "BEGIN SELECT RAISE(ABORT, 'test rollback'); END"
                        )
                    )
                rollback_url = (
                    f"/api/reservations/{rollback_reservation_id}/checkout"
                )
                rollback_headers = {"Idempotency-Key": "retry-after-db-failure"}
                failed = await client.post(
                    rollback_url,
                    json={"outcome": "success"},
                    headers=rollback_headers,
                )
                assert failed.status_code == 409

                async with session_factory() as session:
                    assert await session.scalar(
                        select(func.count(IdempotencyRecord.id)).where(
                            IdempotencyRecord.key == "retry-after-db-failure"
                        )
                    ) == 0
                    assert await session.scalar(
                        select(Booking.id).where(
                            Booking.reservation_id == rollback_reservation_id
                        )
                    ) is None

                async with test_engine.begin() as connection:
                    await connection.execute(
                        text("DROP TRIGGER reject_idempotency_payment")
                    )
                retry_after_failure = await client.post(
                    rollback_url,
                    json={"outcome": "success"},
                    headers=rollback_headers,
                )
                assert retry_after_failure.status_code == 201

            # Re-running the create-if-missing schema initializer preserves
            # durable idempotency responses along with other table data.
            await initialize_schema(test_engine)
            async with session_factory() as session:
                assert await session.scalar(
                    select(func.count(IdempotencyRecord.id))
                ) == 5
                assert await session.scalar(
                    select(func.count(Reservation.id)).where(
                        Reservation.event_id == event_id,
                        Reservation.seat_id == seat_ids[0],
                    )
                ) == 1
                assert await session.scalar(
                    select(func.count(Booking.id))
                ) == 3
        finally:
            app.dependency_overrides.clear()
            await test_engine.dispose()

    asyncio.run(scenario())
