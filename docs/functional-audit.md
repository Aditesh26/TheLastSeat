# Functional audit

Audit date: 2026-10-18. This table reflects the implementation after the
availability, history, delayed settlement, and confirmed-booking cancellation
and client idempotency-key work in this milestone.

| Requirement | Status | Relevant endpoint, code, constraint, and tests | Remaining limitation |
|---|---|---|---|
| View available seats, reserve seats, and confirm bookings | **Implemented** | `GET /api/events/{event_id}/seats` in `src/codechef_task/inventory_routes.py:list_event_seats` reports `available`, `reserved`, or `booked`. Reservation creation is `POST /api/events/{event_id}/reservations`; checkout is `POST /api/reservations/{reservation_id}/checkout`. `tests/test_inventory_routes.py:test_seat_status_booking_history_and_confirmed_booking_cancellation` covers state before/after hold, expiry, cancellation, booking, and reallocation. | Availability is a point-in-time view and may change immediately; reservation creation remains the authoritative operation and may return 409. |
| Prevent multiple users from successfully booking the same seat | **Implemented** | Seat-first `SELECT ... FOR UPDATE` on lifecycle writes; unique `uq_bookings_confirmed_event_seat` on `(event_id, confirmed_seat_id)` and `uq_reservations_active_event_seat`. Tests: `tests/test_schema.py:test_unique_index_allows_only_one_confirmed_booking_per_event_seat`, `tests/test_reservation_mariadb.py:test_concurrent_reservations_are_safe_on_mariadb`. | Safety depends on InnoDB and writers respecting the same locking/constraint protocol. This is not a measured capacity guarantee. |
| Implement temporary reservations that automatically expire | **Partially implemented** | `Reservation.expires_at`, `ReservationStatus`, and `reservation_routes.py:_expire_if_elapsed`; the seat listing also processes elapsed holds using the same seat-first lock. Tests: `tests/test_reservation_routes.py:test_reservation_endpoints_and_expiration_with_sqlite`, `tests/test_inventory_routes.py:test_seat_status_booking_history_and_confirmed_booking_cancellation`, and delayed expiry in `tests/test_delayed_payment.py:test_delayed_payment_success_failure_expiry_cancel_and_replay`. | Expiry is lazy, not a background worker. An untouched hold can remain stored as `active` after its deadline until an API operation processes it. |
| Support booking cancellations and seat reallocation | **Implemented** | Temporary hold cancellation: `DELETE /api/reservations/{reservation_id}`. Confirmed booking cancellation: `POST /api/bookings/{booking_id}/cancel` in `inventory_routes.py:cancel_confirmed_booking`; it locks the seat then booking, checks `user_ref`, and changes the booking to `cancelled` while preserving history. `tests/test_inventory_routes.py::test_seat_status_booking_history_and_confirmed_booking_cancellation` covers seat reuse/repeat cancellation; `tests/test_user_ownership.py::test_user_header_ownership_booking_history_idempotency_and_legacy_rows` covers owner mismatch, header validation, and NULL-owner denial. | `X-User-Ref` is caller-controlled identification, not authentication. Cancellation does not issue a refund; the succeeded payment stays in history. |
| Simulate payment success, failure, and delayed transactions | **Implemented** | Checkout accepts `success`, `failure`, or `delayed`; `POST /api/payments/{payment_id}/settle` resolves pending attempts. Delayed success/failure, expiration/cancellation before settlement, and rollback are tested in `tests/test_delayed_payment.py`. Concurrent settlement retries are tested in the MariaDB integration test. | Delayed settlement is a later explicit API request, not an automatic timer or provider callback. This is simulation only and makes no real charge. |
| Maintain consistency when requests are repeated or concurrent | **Implemented** | Reservation creation and checkout accept `Idempotency-Key`; `src/codechef_task/idempotency.py` stores fingerprints and successful response bodies in `IdempotencyRecord`. Unique `uq_idempotency_operation_key` serializes a given operation/key claim, while the claim and business writes commit in one transaction. Existing unique booking/reservation/payment constraints remain in force. Tests: `tests/test_idempotency.py:test_idempotency_keys_replay_validate_and_rollback_atomically` and same-key concurrent reservation/checkout in `tests/test_reservation_mariadb.py:test_concurrent_reservations_are_safe_on_mariadb`. | Without authentication, `(operation, key)` is global across callers, so clients must use globally unique keys. Records have no retention/cleanup policy. MariaDB integration tests are required to verify InnoDB concurrent claim behavior; SQLite tests cover functional replay and atomic rollback only. |
| Provide APIs for booking history and reservation status | **Implemented with a demo-only unscoped route** | `GET /api/users/me/bookings` filters paginated booking/payment records by normalized `X-User-Ref`; `GET /api/events/{event_id}/bookings/all` returns every booking/payment for an event; `GET /api/reservations/{reservation_id}` returns reservation status. `tests/test_inventory_routes.py` covers both the denied original path and the all-bookings route. | The `/all` route has no authentication or staff authorization and exposes booking/payment details. Use only with non-sensitive demo data; protect it before deployment with real access control. |

## Lightweight ownership identification

`Reservation.user_ref` and `Booking.user_ref` are nullable ORM fields, with the
existing forward migration adding the same nullable MariaDB columns if absent.
New reservations store the normalized `X-User-Ref`; checkout copies it to the
booking. The reference is trimmed, uppercased, and validated as 6–20 ASCII
letters/digits. Existing rows are not backfilled. Owner mismatches and NULL
owners return 403 on reservation retrieval/cancellation, checkout, settlement,
and confirmed-booking cancellation. The original event-wide history path is
disabled with 403, but `/api/events/{event_id}/bookings/all` is deliberately
unscoped for the recruitment demo and exposes booking/payment details. This mechanism is
identification only: callers can choose arbitrary registration-number strings;
there is no authentication provider.

## Payment and expiry states

Delayed checkout creates a `pending` booking and `pending` payment while the
reservation remains `active`. Successful settlement changes them to a
`confirmed` booking, `succeeded` payment, and `converted` reservation. Failed
settlement changes the booking to `cancelled`, payment to `failed`, and
reservation to `cancelled`. If the hold is cancelled or lazily expires first,
the pending payment is failed and the booking is cancelled; later settlement
cannot confirm it.

There is no background expiry or payment-settlement worker. A pending delayed
payment remains pending until settlement is requested or an API operation
cancels/expires its reservation.
