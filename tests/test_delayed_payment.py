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


async def _make_test_data(session_factory, count: int = 4):
    async with session_factory() as session:
        event = Event(
            slug="delayed-payment-event",
            name="Delayed Payment Test",
            ticket_price=Decimal("15.00"),
            currency="USD",
            status=EventStatus.OPEN.value,
        )
        session.add(event)
        await session.flush()
        seats = [Seat(event_id=event.id, seat_number=n) for n in range(1, count + 1)]
        session.add_all(seats)
        await session.flush()
        reservations = [
            Reservation(
                event_id=event.id,
                seat_id=seat.id,
                user_ref="24BCE0001",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            for seat in seats
        ]
        session.add_all(reservations)
        await session.flush()
        identifiers = [event.id, [row.id for row in reservations]]
        await session.commit()
        return identifiers


def test_delayed_payment_success_failure_expiry_cancel_and_replay() -> None:
    async def scenario() -> None:
        test_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        await initialize_schema(test_engine)
        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

        async def provide_test_session():
            async with session_factory() as session:
                yield session

        app.dependency_overrides[get_db_session] = provide_test_session
        try:
            event_id, reservation_ids = await _make_test_data(session_factory)
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test",
                headers={"X-User-Ref": "24BCE0001"},
            ) as client:
                delayed = await client.post(
                    f"/api/reservations/{reservation_ids[0]}/checkout",
                    json={"outcome": "delayed"},
                )
                assert delayed.status_code == 201
                delayed_data = delayed.json()
                assert delayed_data["booking_status"] == "pending"
                assert delayed_data["payment_status"] == "pending"

                pending_replay = await client.post(
                    f"/api/reservations/{reservation_ids[0]}/checkout",
                    json={"outcome": "failure"},
                )
                assert pending_replay.status_code == 200
                assert pending_replay.json() == delayed_data
                assert (
                    await client.get(f"/api/events/{event_id}/seats")
                ).json()["seats"][0]["status"] == "reserved"

                settled = await client.post(
                    f"/api/payments/{delayed_data['payment_id']}/settle",
                    json={"outcome": "success"},
                )
                assert settled.status_code == 200
                assert settled.json()["booking_status"] == "confirmed"
                assert settled.json()["payment_status"] == "succeeded"
                replayed_settlement = await client.post(
                    f"/api/payments/{delayed_data['payment_id']}/settle",
                    json={"outcome": "failure"},
                )
                assert replayed_settlement.json() == settled.json()
                assert (
                    await client.get(f"/api/events/{event_id}/seats")
                ).json()["seats"][0]["status"] == "booked"

                delayed_failure = await client.post(
                    f"/api/reservations/{reservation_ids[1]}/checkout",
                    json={"outcome": "delayed"},
                )
                failure_settled = await client.post(
                    f"/api/payments/{delayed_failure.json()['payment_id']}/settle",
                    json={"outcome": "failure"},
                )
                assert failure_settled.json()["booking_status"] == "cancelled"
                assert failure_settled.json()["payment_status"] == "failed"
                assert (
                    await client.get(f"/api/events/{event_id}/seats")
                ).json()["seats"][1]["status"] == "available"

                delayed_cancel = await client.post(
                    f"/api/reservations/{reservation_ids[2]}/checkout",
                    json={"outcome": "delayed"},
                )
                cancelled_hold = await client.delete(
                    f"/api/reservations/{reservation_ids[2]}"
                )
                assert cancelled_hold.json()["status"] == "cancelled"
                after_hold_cancel = await client.post(
                    f"/api/payments/{delayed_cancel.json()['payment_id']}/settle",
                    json={"outcome": "success"},
                )
                assert after_hold_cancel.json()["booking_status"] == "cancelled"
                assert after_hold_cancel.json()["payment_status"] == "failed"

                delayed_expiry = await client.post(
                    f"/api/reservations/{reservation_ids[3]}/checkout",
                    json={"outcome": "delayed"},
                )
                async with session_factory() as session:
                    reservation = await session.get(
                        Reservation, reservation_ids[3]
                    )
                    reservation.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                    await session.commit()
                expired_hold = await client.get(
                    f"/api/reservations/{reservation_ids[3]}"
                )
                assert expired_hold.json()["status"] == "expired"
                after_expiry = await client.post(
                    f"/api/payments/{delayed_expiry.json()['payment_id']}/settle",
                    json={"outcome": "success"},
                )
                assert after_expiry.json()["booking_status"] == "cancelled"
                assert after_expiry.json()["payment_status"] == "failed"

            async with session_factory() as session:
                statuses = dict(
                    (
                        await session.execute(
                            select(Reservation.id, Reservation.status).where(
                                Reservation.id.in_(reservation_ids)
                            )
                        )
                    ).all()
                )
                assert statuses == {
                    reservation_ids[0]: "converted",
                    reservation_ids[1]: "cancelled",
                    reservation_ids[2]: "cancelled",
                    reservation_ids[3]: "expired",
                }
                assert await session.scalar(
                    select(Payment.status).where(
                        Payment.id == delayed_cancel.json()["payment_id"]
                    )
                ) == PaymentStatus.FAILED.value
                assert await session.scalar(
                    select(Booking.status).where(
                        Booking.reservation_id == reservation_ids[3]
                    )
                ) == BookingStatus.CANCELLED.value
        finally:
            app.dependency_overrides.clear()
            await test_engine.dispose()

    asyncio.run(scenario())


def test_delayed_settlement_rolls_back_if_payment_update_fails() -> None:
    async def scenario() -> None:
        test_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        await initialize_schema(test_engine)
        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

        async def provide_test_session():
            async with session_factory() as session:
                yield session

        app.dependency_overrides[get_db_session] = provide_test_session
        try:
            _, reservation_ids = await _make_test_data(session_factory, count=1)
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test",
                headers={"X-User-Ref": "24BCE0001"},
            ) as client:
                delayed = await client.post(
                    f"/api/reservations/{reservation_ids[0]}/checkout",
                    json={"outcome": "delayed"},
                )
                payment_id = delayed.json()["payment_id"]
                async with test_engine.begin() as connection:
                    await connection.execute(
                        text(
                            "CREATE TRIGGER reject_payment_settlement "
                            "BEFORE UPDATE OF status ON payments "
                            "WHEN NEW.status = 'succeeded' "
                            "BEGIN SELECT RAISE(ABORT, 'test rollback'); END"
                        )
                    )
                failed_settlement = await client.post(
                    f"/api/payments/{payment_id}/settle",
                    json={"outcome": "success"},
                )
                assert failed_settlement.status_code == 409

                async with session_factory() as session:
                    assert await session.scalar(
                        select(Reservation.status).where(
                            Reservation.id == reservation_ids[0]
                        )
                    ) == ReservationStatus.ACTIVE.value
                    assert await session.scalar(
                        select(Booking.status).where(
                            Booking.reservation_id == reservation_ids[0]
                        )
                    ) == BookingStatus.PENDING.value
                    assert await session.scalar(
                        select(Payment.status).where(Payment.id == payment_id)
                    ) == PaymentStatus.PENDING.value

                async with test_engine.begin() as connection:
                    await connection.execute(
                        text("DROP TRIGGER reject_payment_settlement")
                    )
                retry = await client.post(
                    f"/api/payments/{payment_id}/settle",
                    json={"outcome": "success"},
                )
                assert retry.status_code == 200
                assert retry.json()["booking_status"] == "confirmed"
        finally:
            app.dependency_overrides.clear()
            await test_engine.dispose()

    asyncio.run(scenario())
