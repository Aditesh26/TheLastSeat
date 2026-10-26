import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from codechef_task.app import app
from codechef_task.database import get_db_session
from codechef_task.models import Event, EventStatus, Reservation, ReservationStatus, Seat
from codechef_task.schema import initialize_schema


def test_seat_status_booking_history_and_confirmed_booking_cancellation() -> None:
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
                    slug="inventory-history-event",
                    name="Inventory and History",
                    ticket_price=Decimal("12.00"),
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
                await session.commit()

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test",
                headers={"X-User-Ref": "24BCE0001"},
            ) as client:
                initial = await client.get(f"/api/events/{event_id}/seats")
                assert initial.status_code == 200
                assert [row["status"] for row in initial.json()["seats"]] == [
                    "available"
                ] * 4

                held = await client.post(
                    f"/api/events/{event_id}/reservations",
                    json={"seat_id": seat_ids[0]},
                )
                reservation_id = held.json()["id"]
                after_hold = await client.get(f"/api/events/{event_id}/seats")
                assert after_hold.json()["seats"][0]["status"] == "reserved"

                async with session_factory() as session:
                    reservation = await session.get(Reservation, reservation_id)
                    reservation.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                    await session.commit()
                after_expiry = await client.get(f"/api/events/{event_id}/seats")
                assert after_expiry.json()["seats"][0]["status"] == "available"
                async with session_factory() as session:
                    assert await session.scalar(
                        select(Reservation.status).where(
                            Reservation.id == reservation_id
                        )
                    ) == ReservationStatus.EXPIRED.value

                held_for_cancel = await client.post(
                    f"/api/events/{event_id}/reservations",
                    json={"seat_id": seat_ids[1]},
                )
                await client.delete(
                    f"/api/reservations/{held_for_cancel.json()['id']}"
                )
                after_hold_cancel = await client.get(
                    f"/api/events/{event_id}/seats"
                )
                assert after_hold_cancel.json()["seats"][1]["status"] == "available"

                confirmed_hold = await client.post(
                    f"/api/events/{event_id}/reservations",
                    json={"seat_id": seat_ids[2]},
                )
                checkout = await client.post(
                    f"/api/reservations/{confirmed_hold.json()['id']}/checkout",
                    json={"outcome": "success"},
                )
                assert checkout.status_code == 201
                after_booking = await client.get(f"/api/events/{event_id}/seats")
                assert after_booking.json()["seats"][2]["status"] == "booked"

                history = await client.get(f"/api/events/{event_id}/bookings")
                assert history.status_code == 403

                all_history = await client.get(
                    f"/api/events/{event_id}/bookings/all"
                )
                assert all_history.status_code == 200
                assert all_history.json()["event_id"] == event_id
                assert len(all_history.json()["items"]) == 1
                assert all_history.json()["items"][0]["payment"]["status"] == "succeeded"

                history = await client.get("/api/users/me/bookings")
                assert history.status_code == 200
                assert len(history.json()["items"]) == 1
                booking = history.json()["items"][0]
                assert "user_ref" not in booking
                assert booking["status"] == "confirmed"
                assert booking["payment"]["status"] == "succeeded"
                assert booking["payment"]["amount"] == "12.00"

                cancelled_booking = await client.post(
                    f"/api/bookings/{booking['id']}/cancel"
                )
                assert cancelled_booking.status_code == 200
                assert cancelled_booking.json()["status"] == "cancelled"
                # The successful payment remains in history; cancellation does
                # not imply a refund or delete the booking.
                assert cancelled_booking.json()["payment"]["status"] == "succeeded"
                repeated_cancel = await client.post(
                    f"/api/bookings/{booking['id']}/cancel"
                )
                assert repeated_cancel.status_code == 200
                after_booking_cancel = await client.get(
                    f"/api/events/{event_id}/seats"
                )
                assert after_booking_cancel.json()["seats"][2]["status"] == "available"
                history_after_cancel = await client.get("/api/users/me/bookings")
                assert history_after_cancel.json()["items"][0]["status"] == "cancelled"
                all_history_after_cancel = await client.get(
                    f"/api/events/{event_id}/bookings/all"
                )
                assert all_history_after_cancel.json()["items"][0]["status"] == "cancelled"

                reallocated = await client.post(
                    f"/api/events/{event_id}/reservations",
                    json={"seat_id": seat_ids[2]},
                )
                assert reallocated.status_code == 201
        finally:
            app.dependency_overrides.clear()
            await test_engine.dispose()

    asyncio.run(scenario())
