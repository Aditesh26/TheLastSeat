import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from codechef_task.app import app
from codechef_task.config import Settings, settings
from codechef_task.database import get_db_session
from codechef_task.models import (
    Booking,
    Event,
    EventStatus,
    Reservation,
    ReservationStatus,
    Seat,
)


def test_reservation_endpoints_and_expiration_with_sqlite() -> None:
    async def scenario() -> None:
        assert Settings(_env_file=None).reservation_hold_seconds == 300
        test_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        from codechef_task.schema import initialize_schema

        await initialize_schema(test_engine)
        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

        async def provide_test_session():
            async with session_factory() as session:
                yield session

        app.dependency_overrides[get_db_session] = provide_test_session
        try:
            async with session_factory() as session:
                open_event = Event(
                    slug="route-open-event",
                    name="Open Event",
                    ticket_price=Decimal("20.00"),
                    currency="USD",
                    status=EventStatus.OPEN.value,
                )
                other_event = Event(
                    slug="route-other-event",
                    name="Other Event",
                    ticket_price=Decimal("20.00"),
                    currency="USD",
                    status=EventStatus.OPEN.value,
                )
                closed_event = Event(
                    slug="route-closed-event",
                    name="Closed Event",
                    ticket_price=Decimal("20.00"),
                    currency="USD",
                    status=EventStatus.CLOSED.value,
                )
                cancelled_event = Event(
                    slug="route-cancelled-event",
                    name="Cancelled Event",
                    ticket_price=Decimal("20.00"),
                    currency="USD",
                    status=EventStatus.CANCELLED.value,
                )
                session.add_all([open_event, other_event, closed_event, cancelled_event])
                await session.flush()

                open_seats = [Seat(event_id=open_event.id, seat_number=n) for n in range(1, 8)]
                other_seat = Seat(event_id=other_event.id, seat_number=1)
                closed_seat = Seat(event_id=closed_event.id, seat_number=1)
                cancelled_seat = Seat(event_id=cancelled_event.id, seat_number=1)
                session.add_all([*open_seats, other_seat, closed_seat, cancelled_seat])
                await session.flush()
                open_event_id = open_event.id
                open_seat_ids = [seat.id for seat in open_seats]
                other_seat_id = other_seat.id
                closed_event_id, closed_seat_id = closed_event.id, closed_seat.id
                cancelled_event_id, cancelled_seat_id = cancelled_event.id, cancelled_seat.id

                session.add(
                    Booking(
                        event_id=open_event.id,
                        seat_id=open_seat_ids[3],
                        status="confirmed",
                        amount_paid=Decimal("20.00"),
                        currency="USD",
                    )
                )
                old_time = datetime.now(UTC) - timedelta(seconds=1)
                expired_for_reclaim = Reservation(
                    event_id=open_event.id,
                    seat_id=open_seat_ids[4],
                    status=ReservationStatus.ACTIVE.value,
                    created_at=old_time - timedelta(minutes=5),
                    updated_at=old_time - timedelta(minutes=5),
                    expires_at=old_time,
                )
                expired_for_read = Reservation(
                    event_id=open_event.id,
                    seat_id=open_seat_ids[5],
                    status=ReservationStatus.ACTIVE.value,
                    created_at=old_time - timedelta(minutes=5),
                    updated_at=old_time - timedelta(minutes=5),
                    expires_at=old_time,
                )
                session.add_all([expired_for_reclaim, expired_for_read])
                await session.flush()
                expired_ids = [expired_for_reclaim.id, expired_for_read.id]
                await session.commit()

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test",
                headers={"X-User-Ref": "test-user"},
            ) as client:
                assert (await client.get("/health")).status_code == 200
                assert (await client.get("/health/db")).status_code == 200

                created = await client.post(
                    f"/api/events/{open_event_id}/reservations",
                    json={"seat_id": open_seat_ids[0]},
                )
                assert created.status_code == 201, created.text
                created_data = created.json()
                assert created_data["event_id"] == open_event_id
                assert created_data["seat_id"] == open_seat_ids[0]
                assert created_data["status"] == "active"
                created_at = datetime.fromisoformat(created_data["created_at"])
                expires_at = datetime.fromisoformat(created_data["expires_at"])
                assert (expires_at - created_at).total_seconds() == settings.reservation_hold_seconds
                held_again = await client.post(
                    f"/api/events/{open_event_id}/reservations",
                    json={"seat_id": open_seat_ids[0]},
                )
                assert held_again.status_code == 409

                missing_seat = await client.post(
                    f"/api/events/{open_event_id}/reservations", json={"seat_id": 999999}
                )
                assert missing_seat.status_code == 404
                wrong_event = await client.post(
                    f"/api/events/{open_event_id}/reservations", json={"seat_id": other_seat_id}
                )
                assert wrong_event.status_code == 404

                for event_id, seat_id in (
                    (closed_event_id, closed_seat_id),
                    (cancelled_event_id, cancelled_seat_id),
                ):
                    response = await client.post(
                        f"/api/events/{event_id}/reservations", json={"seat_id": seat_id}
                    )
                    assert response.status_code == 409

                already_booked = await client.post(
                    f"/api/events/{open_event_id}/reservations",
                    json={"seat_id": open_seat_ids[3]},
                )
                assert already_booked.status_code == 409

                reclaimed = await client.post(
                    f"/api/events/{open_event_id}/reservations",
                    json={"seat_id": open_seat_ids[4]},
                )
                assert reclaimed.status_code == 201
                old_hold = await client.get(f"/api/reservations/{expired_ids[0]}")
                assert old_hold.status_code == 200
                assert old_hold.json()["status"] == "expired"

                retrieved_expired = await client.get(
                    f"/api/reservations/{expired_ids[1]}"
                )
                assert retrieved_expired.status_code == 200
                assert retrieved_expired.json()["status"] == "expired"

                held_for_cancel = await client.post(
                    f"/api/events/{open_event_id}/reservations",
                    json={"seat_id": open_seat_ids[1]},
                )
                cancel_id = held_for_cancel.json()["id"]
                cancelled = await client.delete(f"/api/reservations/{cancel_id}")
                assert cancelled.status_code == 200
                assert cancelled.json()["status"] == "cancelled"
                new_hold = await client.post(
                    f"/api/events/{open_event_id}/reservations",
                    json={"seat_id": open_seat_ids[1]},
                )
                assert new_hold.status_code == 201
                still_cancelled = await client.get(f"/api/reservations/{cancel_id}")
                assert still_cancelled.json()["status"] == "cancelled"
                cancel_again = await client.delete(f"/api/reservations/{cancel_id}")
                assert cancel_again.status_code == 409

                expired_cancel = await client.delete(
                    f"/api/reservations/{expired_ids[0]}"
                )
                assert expired_cancel.status_code == 409
                cannot_get_missing = await client.get("/api/reservations/999999")
                assert cannot_get_missing.status_code == 404

            async with session_factory() as session:
                states = dict(
                    (
                        await session.execute(
                        select(Reservation.id, Reservation.status).where(
                            Reservation.id.in_(expired_ids)
                        )
                        )
                    ).all()
                )
                assert set(states.values()) == {ReservationStatus.EXPIRED.value}
        finally:
            app.dependency_overrides.clear()
            await test_engine.dispose()

    asyncio.run(scenario())
