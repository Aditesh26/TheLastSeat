import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from codechef_task.app import app
from codechef_task.database import get_db_session
from codechef_task.models import (
    Booking,
    Event,
    EventStatus,
    Payment,
    Reservation,
    ReservationStatus,
    Seat,
)
from codechef_task.schema import initialize_schema


def test_checkout_success_failure_replays_expiry_and_rollback() -> None:
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
                    slug="checkout-workflow-event",
                    name="Checkout Workflow",
                    ticket_price=Decimal("37.50"),
                    currency="USD",
                    status=EventStatus.OPEN.value,
                )
                session.add(event)
                await session.flush()
                seats = [Seat(event_id=event.id, seat_number=n) for n in range(1, 6)]
                session.add_all(seats)
                await session.flush()
                now = datetime.now(UTC)
                reservations = [
                    Reservation(
                        event_id=event.id,
                        seat_id=seats[0].id,
                        expires_at=now + timedelta(minutes=5),
                    ),
                    Reservation(
                        event_id=event.id,
                        seat_id=seats[1].id,
                        expires_at=now + timedelta(minutes=5),
                    ),
                    Reservation(
                        event_id=event.id,
                        seat_id=seats[2].id,
                        expires_at=now - timedelta(seconds=1),
                    ),
                    Reservation(
                        event_id=event.id,
                        seat_id=seats[3].id,
                        status=ReservationStatus.CANCELLED.value,
                        expires_at=now + timedelta(minutes=5),
                    ),
                    Reservation(
                        event_id=event.id,
                        seat_id=seats[4].id,
                        expires_at=now + timedelta(minutes=5),
                    ),
                ]
                session.add_all(reservations)
                await session.flush()
                event_id = event.id
                reservation_ids = [reservation.id for reservation in reservations]
                await session.commit()

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test",
                headers={"X-User-Ref": "test-user"},
            ) as client:
                success = await client.post(
                    f"/api/reservations/{reservation_ids[0]}/checkout",
                    json={"outcome": "success"},
                )
                assert success.status_code == 201, success.text
                success_data = success.json()
                assert success_data["booking_status"] == "confirmed"
                assert success_data["payment_status"] == "succeeded"
                assert success_data["amount"] == "37.50"

                replay = await client.post(
                    f"/api/reservations/{reservation_ids[0]}/checkout",
                    json={"outcome": "failure"},
                )
                assert replay.status_code == 200
                assert replay.json() == success_data

                failure = await client.post(
                    f"/api/reservations/{reservation_ids[1]}/checkout",
                    json={"outcome": "failure"},
                )
                assert failure.status_code == 201
                assert failure.json()["booking_status"] == "cancelled"
                assert failure.json()["payment_status"] == "failed"

                expired = await client.post(
                    f"/api/reservations/{reservation_ids[2]}/checkout",
                    json={"outcome": "success"},
                )
                assert expired.status_code == 409
                assert expired.json()["detail"] == "Reservation has expired"

                cancelled = await client.post(
                    f"/api/reservations/{reservation_ids[3]}/checkout",
                    json={"outcome": "success"},
                )
                assert cancelled.status_code == 409
                assert cancelled.json()["detail"] == "Reservation has been cancelled"

                # A failure releases its seat and preserves the cancelled record.
                reclaimed = await client.post(
                    f"/api/events/{event_id}/reservations",
                    json={"seat_id": 2},
                )
                assert reclaimed.status_code == 201

                # A database-side failure after booking insertion must roll the
                # booking and reservation transition back with the payment.
                async with test_engine.begin() as connection:
                    await connection.execute(
                        text(
                            "CREATE TRIGGER reject_test_payment BEFORE INSERT ON payments "
                            "BEGIN SELECT RAISE(ABORT, 'test rollback'); END"
                        )
                    )
                rolled_back = await client.post(
                    f"/api/reservations/{reservation_ids[4]}/checkout",
                    json={"outcome": "success"},
                )
                assert rolled_back.status_code == 409

            async with session_factory() as session:
                assert await session.scalar(
                    select(Reservation.status).where(
                        Reservation.id == reservation_ids[4]
                    )
                ) == ReservationStatus.ACTIVE.value
                assert await session.scalar(
                    select(Booking.id).where(
                        Booking.reservation_id == reservation_ids[4]
                    )
                ) is None
                assert await session.scalar(
                    select(Reservation.status).where(
                        Reservation.id == reservation_ids[2]
                    )
                ) == ReservationStatus.EXPIRED.value
                assert await session.scalar(
                    select(Payment.amount).where(
                        Payment.booking_id == success_data["booking_id"]
                    )
                ) == Decimal("37.50")
        finally:
            app.dependency_overrides.clear()
            await test_engine.dispose()

    asyncio.run(scenario())
