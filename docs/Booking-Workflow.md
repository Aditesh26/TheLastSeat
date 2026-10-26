# Reservation and booking workflow

```mermaid
flowchart TD
    A[Client requests seat hold] --> B[Begin transaction and lock seat row]
    B --> C{Event open and seat available?}
    C -- No --> D[Return 404 or 409]
    C -- Yes --> E[Create active reservation with expires_at]
    E --> F[Commit hold]

    F --> G{Later reservation action}
    G -- Retrieve --> H[Lock seat then reservation; compare expiry to UTC now]
    H --> I{Expired?}
    I -- Yes --> J[Persist expired; seat becomes reusable]
    I -- No --> K[Return current reservation state]
    G -- Cancel --> L[Lock seat then reservation]
    L --> M{Still active and unexpired?}
    M -- Yes --> N[Persist cancelled; seat becomes reusable]
    M -- No --> O[Return conflict or preserve terminal state]

    F --> P[Client posts simulated checkout outcome]
    P --> Q[Locate reservation, then begin transaction]
    Q --> R[Lock seat row, then reservation row]
    R --> S{Existing checkout record?}
    S -- Yes --> T[Return original booking and payment; no new attempt]
    S -- No --> U{Active and unexpired?}
    U -- No, expired --> V[Persist expired and return 409]
    U -- No, cancelled or converted --> W[Return 409]
    U -- Yes --> X[Read authoritative ticket price from event]
    X --> Y{Simulated outcome}
    Y -- Success --> Z[Create confirmed booking and succeeded payment]
    Z --> ZA[Reservation becomes converted]
    Y -- Failure --> ZB[Create cancelled historical booking and failed payment]
    ZB --> ZC[Reservation becomes cancelled; seat is reusable]
    Y -- Delayed --> ZD[Create pending booking and payment; keep hold active]
    ZD --> ZE[Commit pending checkout]
    ZE --> ZF[Client later requests payment settlement]
    ZF --> ZG[Lock seat, reservation, booking, and payment]
    ZG --> ZH{Hold still active and unexpired?}
    ZH -- No --> ZI[Fail payment and cancel booking; seat reusable]
    ZH -- Yes, success --> ZJ[Confirm booking; succeed payment; convert hold]
    ZH -- Yes, failure --> ZK[Cancel booking and hold; fail payment; seat reusable]
    ZA --> ZD2[Commit checkout writes together]
    ZC --> ZD2
    ZJ --> ZD2
    ZI --> ZD2
    ZK --> ZD2
    ZD2 --> ZL[Commit state transition atomically]
    ZL --> ZM[Confirmed booking may later be cancelled]
    ZM --> ZN[Preserve booking/payment history; free seat]

    J --> AA[New reservation may reuse seat]
    N --> AA
    ZC --> AA
    ZI --> AA
    ZK --> AA
    ZN --> AA
```

Every mutating reservation/checkout path locks the concrete seat before the
reservation row and uses a database transaction. Settlement also locks the
booking and payment before updating them. A database error rolls back the
booking, payment, and reservation transition together. Delayed payment is
settled by an explicit later API request; no sleeping request or background
payment task is used. Expiry is lazy: no background job currently expires
untouched holds. Cancelling a confirmed booking preserves the succeeded
payment record and does not issue a refund.

Reservation creation, reservation reads/cancellation, checkout, settlement,
and caller booking history require a syntactically valid `X-User-Ref`. For
owned rows, mutations verify the owner after acquiring their existing seat-
first locks; no additional row locks are taken. A non-NULL owner mismatch
returns 403. Legacy rows whose nullable `user_ref` is NULL remain accessible
with any valid reference. This header is caller-controlled identification,
not authentication; comparisons are case-sensitive. Idempotency fingerprints include the caller reference,
so another caller cannot replay a keyed reservation or checkout response.
