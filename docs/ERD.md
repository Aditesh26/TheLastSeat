# The Last Seat entity relationship diagram

This diagram reflects the five booking-domain SQLAlchemy models and the
additional durable idempotency model, with their current foreign
keys. `Seat.event_id` plus `Seat.id` is the referenced composite key used to
ensure reservations and bookings use seats from the same event.

```mermaid
erDiagram
    EVENT ||--o{ SEAT : inventory
    SEAT ||--o{ RESERVATION : held_as
    SEAT ||--o{ BOOKING : booked_as
    RESERVATION o|--o| BOOKING : converts_to
    BOOKING ||--o| PAYMENT : has_attempt

    EVENT {
        int id PK
        string slug UK
        string name
        datetime starts_at
        decimal ticket_price
        char currency
        string status
        datetime created_at
        datetime updated_at
    }
    SEAT {
        int id PK
        int event_id FK
        int seat_number
        datetime created_at
    }
    RESERVATION {
        int id PK
        int event_id FK
        int seat_id FK
        string status
        datetime expires_at
        int active_seat_id "generated while active"
        datetime created_at
        datetime updated_at
    }
    BOOKING {
        int id PK
        int event_id FK
        int seat_id FK
        int reservation_id FK "nullable, unique"
        string status "pending, confirmed, cancelled"
        decimal amount_paid
        char currency
        int confirmed_seat_id "generated while confirmed"
        datetime created_at
        datetime updated_at
    }
    PAYMENT {
        int id PK
        int booking_id FK "unique"
        decimal amount
        char currency
        string status
        string provider_reference UK
        datetime created_at
        datetime updated_at
    }
    IDEMPOTENCY_RECORD {
        int id PK
        string operation
        string key
        char request_fingerprint
        int response_status
        json response_body
        datetime created_at
    }
```

The active-reservation and confirmed-booking generated keys have unique
indexes with `event_id`. The nullable generated key lets historical terminal
rows coexist while preventing two active holds or two confirmed bookings for
the same event seat. A unique nullable `Booking.reservation_id` links at most
one booking to a reservation; a unique `Payment.booking_id` allows at most one
payment record for a booking. Pending bookings do not set the confirmed-seat
key; their active reservation protects that seat until settlement, cancellation,
or expiry. `IdempotencyRecord` is independent of those domain foreign keys;
`uq_idempotency_operation_key` uniquely scopes the client key by operation.
Its fingerprint and response are committed in the same transaction as the
associated successful reservation or checkout.
