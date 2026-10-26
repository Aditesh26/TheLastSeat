"""Basic concurrent HTTP load test for a disposable, uniquely named event.

The script creates only its own event and inventory, verifies the resulting
reservation/booking rows, then deletes that event (and its cascading test rows)
unless --keep-data is supplied. It never selects or edits the demo event.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import sys
import time
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

import httpx
from sqlalchemy import delete, func, select, text

from codechef_task.database import SessionLocal, engine
from codechef_task.models import (
    Booking,
    BookingStatus,
    Event,
    EventStatus,
    Payment,
    Reservation,
    ReservationStatus,
    Seat,
)


@dataclass
class Result:
    status_code: int | None
    duration_ms: float
    body: dict | None = None
    error_type: str | None = None


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 2)
    value = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return round(value, 2)


def summarize(
    results: list[Result], duration_seconds: float, expected: set[int]
) -> dict:
    statuses = Counter(str(result.status_code) for result in results)
    successes = sum(
        result.status_code is not None and 200 <= result.status_code < 300
        for result in results
    )
    rejections = sum(result.status_code == 409 for result in results)
    unexpected = sum(
        result.error_type is not None or result.status_code not in expected
        for result in results
    )
    latencies = [result.duration_ms for result in results]
    return {
        "request_count": len(results),
        "successes": successes,
        "rejections_409": rejections,
        "unexpected_errors": unexpected,
        "http_status_counts": dict(sorted(statuses.items())),
        "duration_seconds": round(duration_seconds, 3),
        "throughput_requests_per_second": (
            round(len(results) / duration_seconds, 2) if duration_seconds > 0 else 0
        ),
        "response_time_ms": {
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
            "p99": percentile(latencies, 99),
        },
    }


async def create_test_event(seat_count: int) -> tuple[int, str, list[int]]:
    slug = f"load-test-{uuid4().hex}"
    async with SessionLocal() as session:
        event = Event(
            slug=slug,
            name="Disposable Load Test Event",
            ticket_price=Decimal("25.00"),
            currency="USD",
            status=EventStatus.OPEN.value,
        )
        session.add(event)
        await session.flush()
        seats = [
            Seat(event_id=event.id, seat_number=n) for n in range(1, seat_count + 1)
        ]
        session.add_all(seats)
        await session.flush()
        event_id = event.id
        seat_ids = [seat.id for seat in seats]
        await session.commit()
    return event_id, slug, seat_ids


async def request(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    method: str,
    path: str,
    payload: dict | None = None,
) -> Result:
    started = time.perf_counter()
    async with semaphore:
        try:
            response = await client.request(method, path, json=payload)
            try:
                body = response.json()
            except ValueError:
                body = None
            return Result(
                status_code=response.status_code,
                duration_ms=(time.perf_counter() - started) * 1000,
                body=body if isinstance(body, dict) else None,
            )
        except Exception as exc:  # Report type only; avoid dumping internals.
            return Result(
                status_code=None,
                duration_ms=(time.perf_counter() - started) * 1000,
                error_type=type(exc).__name__,
            )


async def verify_database(event_id: int, seat_count: int) -> dict:
    async with SessionLocal() as session:
        server_info = (
            await session.execute(
                text("SELECT VERSION(), @@port, @@default_storage_engine")
            )
        ).one()
        seats = await session.scalar(
            select(func.count(Seat.id)).where(Seat.event_id == event_id)
        )
        reservations = await session.scalar(
            select(func.count(Reservation.id)).where(Reservation.event_id == event_id)
        )
        active = await session.scalar(
            select(func.count(Reservation.id)).where(
                Reservation.event_id == event_id,
                Reservation.status == ReservationStatus.ACTIVE.value,
            )
        )
        bookings = await session.scalar(
            select(func.count(Booking.id)).where(Booking.event_id == event_id)
        )
        confirmed = await session.scalar(
            select(func.count(Booking.id)).where(
                Booking.event_id == event_id,
                Booking.status == BookingStatus.CONFIRMED.value,
            )
        )
        payments = await session.scalar(
            select(func.count(Payment.id))
            .join(Booking, Payment.booking_id == Booking.id)
            .where(Booking.event_id == event_id)
        )
        active_duplicates = (
            await session.execute(
                select(Reservation.seat_id, func.count(Reservation.id))
                .where(
                    Reservation.event_id == event_id,
                    Reservation.status == ReservationStatus.ACTIVE.value,
                )
                .group_by(Reservation.seat_id)
                .having(func.count(Reservation.id) > 1)
            )
        ).all()
        confirmed_duplicates = (
            await session.execute(
                select(Booking.seat_id, func.count(Booking.id))
                .where(
                    Booking.event_id == event_id,
                    Booking.status == BookingStatus.CONFIRMED.value,
                )
                .group_by(Booking.seat_id)
                .having(func.count(Booking.id) > 1)
            )
        ).all()

    return {
        "database_server_version": server_info[0],
        "database_server_port": server_info[1],
        "default_storage_engine": server_info[2],
        "seat_count": seats,
        "reservation_count": reservations,
        "active_reservation_count": active,
        "booking_count": bookings,
        "confirmed_booking_count": confirmed,
        "payment_count": payments,
        "duplicate_active_seats": len(active_duplicates),
        "duplicate_confirmed_booking_seats": len(confirmed_duplicates),
        "invariants_passed": (
            seats == seat_count
            and active == 0
            and bookings == confirmed == payments == seat_count
            and not active_duplicates
            and not confirmed_duplicates
        ),
    }


async def verify_active_holds(event_id: int, expected_count: int) -> dict:
    """Capture inventory while the successful reservations are still active."""
    async with SessionLocal() as session:
        active_count = await session.scalar(
            select(func.count(Reservation.id)).where(
                Reservation.event_id == event_id,
                Reservation.status == ReservationStatus.ACTIVE.value,
            )
        )
        duplicates = (
            await session.execute(
                select(Reservation.seat_id, func.count(Reservation.id))
                .where(
                    Reservation.event_id == event_id,
                    Reservation.status == ReservationStatus.ACTIVE.value,
                )
                .group_by(Reservation.seat_id)
                .having(func.count(Reservation.id) > 1)
            )
        ).all()
    return {
        "active_reservation_count": active_count,
        "duplicate_active_seats": len(duplicates),
        "invariants_passed": active_count == expected_count and not duplicates,
    }


async def delete_test_event(event_id: int) -> None:
    async with SessionLocal.begin() as session:
        await session.execute(delete(Event).where(Event.id == event_id))
    async with SessionLocal() as session:
        leftovers = {
            "event": await session.scalar(
                select(func.count(Event.id)).where(Event.id == event_id)
            ),
            "seats": await session.scalar(
                select(func.count(Seat.id)).where(Seat.event_id == event_id)
            ),
            "reservations": await session.scalar(
                select(func.count(Reservation.id)).where(
                    Reservation.event_id == event_id
                )
            ),
            "bookings": await session.scalar(
                select(func.count(Booking.id)).where(Booking.event_id == event_id)
            ),
            "payments": await session.scalar(
                select(func.count(Payment.id))
                .join(Booking, Payment.booking_id == Booking.id)
                .where(Booking.event_id == event_id)
            ),
        }
    if any(leftovers.values()):
        raise RuntimeError(f"test event cleanup left related rows: {leftovers}")


async def run(args: argparse.Namespace) -> int:
    event_id: int | None = None
    slug: str | None = None
    report: dict = {}
    exit_code = 0
    cleanup = "not-needed"
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        event_id, slug, seat_ids = await create_test_event(args.seats)
        async with httpx.AsyncClient(
            base_url=args.base_url,
            headers={"X-User-Ref": "load-test"},
            timeout=httpx.Timeout(30),
            limits=httpx.Limits(max_connections=args.concurrency),
            trust_env=False,
        ) as client:
            health = await client.get("/health/db")
            if health.status_code != 200:
                raise RuntimeError(
                    "API database health check failed; no load requests were sent"
                )

            semaphore = asyncio.Semaphore(args.concurrency)
            reservation_started = time.perf_counter()
            reservation_results = await asyncio.gather(
                *(
                    request(
                        client,
                        semaphore,
                        "POST",
                        f"/api/events/{event_id}/reservations",
                        {"seat_id": seat_ids[index % args.seats]},
                    )
                    for index in range(args.requests)
                )
            )
            reservation_duration = time.perf_counter() - reservation_started

            reservation_ids = [
                result.body["id"]
                for result in reservation_results
                if result.status_code == 201
                and result.body is not None
                and "id" in result.body
            ]
            active_snapshot = await verify_active_holds(event_id, args.seats)
            checkout_started = time.perf_counter()
            checkout_results = await asyncio.gather(
                *(
                    request(
                        client,
                        semaphore,
                        "POST",
                        f"/api/reservations/{reservation_id}/checkout",
                        {"outcome": "success"},
                    )
                    for reservation_id in reservation_ids
                    for _ in range(args.checkout_retries)
                )
            )
            checkout_duration = time.perf_counter() - checkout_started
            total_duration = reservation_duration + checkout_duration

        database_report = await verify_database(event_id, args.seats)
        reservation_summary = summarize(
            reservation_results, reservation_duration, {201, 409}
        )
        checkout_summary = summarize(checkout_results, checkout_duration, {200, 201})
        all_results = reservation_results + checkout_results
        combined = summarize(all_results, total_duration, {200, 201, 409})
        expected_reservations = min(args.requests, args.seats)
        report = {
            "test_event_slug": slug,
            "event_id": event_id,
            "environment": {
                "operating_system": platform.system(),
                "python_version": sys.version.split()[0],
            },
            "load_settings": {
                "base_url": args.base_url,
                "concurrency_limit": args.concurrency,
                "reservation_requests": args.requests,
                "seat_inventory": args.seats,
                "checkout_requests_per_winner": args.checkout_retries,
            },
            "reservation_contention": reservation_summary,
            "concurrent_checkout_retries": checkout_summary,
            "combined": combined,
            "database_verification_after_reservations": active_snapshot,
            "database_verification_before_cleanup": database_report,
        }
        if (
            reservation_summary["successes"] != expected_reservations
            or reservation_summary["rejections_409"]
            != args.requests - expected_reservations
            or reservation_summary["unexpected_errors"] != 0
            or active_snapshot["active_reservation_count"] != expected_reservations
            or not active_snapshot["invariants_passed"]
            or checkout_summary["request_count"]
            != expected_reservations * args.checkout_retries
            or checkout_summary["unexpected_errors"] != 0
            or not database_report["invariants_passed"]
        ):
            exit_code = 1
    finally:
        if event_id is not None:
            if args.keep_data:
                cleanup = "kept-by-request"
            else:
                try:
                    await delete_test_event(event_id)
                    cleanup = "deleted-test-event-and-cascaded-test-rows"
                except Exception as exc:
                    cleanup = (
                        f"cleanup-failed:{type(exc).__name__}; event_id={event_id}"
                    )
                    exit_code = 1
        report["cleanup"] = cleanup
        await engine.dispose()

    print(json.dumps(report, indent=2))
    return exit_code


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--seats", type=int, default=10)
    parser.add_argument("--checkout-retries", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument(
        "--keep-data",
        action="store_true",
        help="Keep this run's uniquely named event and rows for inspection.",
    )
    args = parser.parse_args()
    if args.seats < 1 or args.requests < args.seats:
        parser.error("--seats must be positive and --requests must be >= --seats")
    if args.checkout_retries < 1 or args.concurrency < 1:
        parser.error("--checkout-retries and --concurrency must be positive")
    return args


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(run(parse_args())))
    except KeyboardInterrupt:
        raise SystemExit(
            "Load test interrupted; the dedicated event cleanup was attempted."
        )
