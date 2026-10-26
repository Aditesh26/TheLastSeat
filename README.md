# The Last Seat

The Last Seat is an asynchronous FastAPI backend for a college recruitment
task. It demonstrates a seat inventory, temporary reservations, and an atomic
booking plus simulated-payment flow using MariaDB and SQLAlchemy 2.x.

## Architecture

- `src/codechef_task/app.py` creates the FastAPI application and health routes.
- `reservation_routes.py` handles holds, reservation retrieval, and cancellation.
- `inventory_routes.py` lists seat availability, event and caller booking
  history, and confirmed-booking cancellation.
- `checkout_routes.py` converts one valid reservation into a booking and
  simulated payment, including delayed settlement.
- `idempotency.py` validates client keys, hashes canonical request parameters,
  and replays durable responses stored in MariaDB.
- `user_identity.py` validates the required caller reference for lightweight
  ownership checks; it does not authenticate callers.
- `models.py` defines the five booking-domain models and durable
  `IdempotencyRecord` storage.
- `database.py` configures the async SQLAlchemy engine/session using `asyncmy`.
- `schema.py`, `migrate.py`, and `seed.py` initialize tables, add checkout
  uniqueness constraints, and seed demo inventory respectively.
- Mermaid diagrams: [ERD](docs/ERD.md) and
  [reservation/booking workflow](docs/Booking-Workflow.md).
- Requirement-by-requirement review: [functional audit](docs/functional-audit.md).

MariaDB/InnoDB is the concurrency authority. SQLite is used for fast isolated
route and constraint tests; it does not reproduce InnoDB row locking.

## Prerequisites

- Python 3.14 or newer, as declared by `pyproject.toml` and `uv.lock`
- [uv](https://docs.astral.sh/uv/)
- For local execution: a reachable MariaDB server and a database/user with
  privileges to apply this project's schema changes.
- For container execution: Docker Engine or Docker Desktop with Docker Compose
  v2 (`docker compose`). Compose uses the `mariadb:13.0.2` image.

The repository does not pin a specific `uv`, Docker Desktop, or Compose version.
Use versions that support the commands shown below.

## Configuration

Copy the example environment file for local use and replace its placeholder
credentials with values for your own database. PowerShell:

```powershell
Copy-Item .env.example .env
```

The application reads these names through Pydantic Settings:

| Variable | Purpose | Application default |
|---|---|---|
| `DB_HOST` | MariaDB hostname | `127.0.0.1` |
| `DB_PORT` | MariaDB TCP port | `3307` |
| `DB_NAME` | Database name | `the_last_seat` |
| `DB_USER` | Database user | Empty; configure a user |
| `DB_PASSWORD` | Database password | Empty; configure a password |
| `RESERVATION_HOLD_SECONDS` | Hold duration | `300` |
| `LOG_LEVEL` | Application log threshold (`DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`) | `INFO` |

The `.env.example` also has Compose-only variables: `MARIADB_ROOT_PASSWORD`
sets the container database root password, and `DOCKER_DB_PORT` selects the
host port published for the database (default `3308`). Compose reads these
values for interpolation. The API container connects to Compose's database
service hostname `db` on port `3306`; it does not use the host's published
database port. When running the API directly on your computer, use the host
database address and port instead (the example is `127.0.0.1:3307`).

The application reads `.env` from its current working directory; explicit
process environment variables take precedence. `.env` is ignored by Git and
excluded from the Docker build context. Keep real credentials out of
`.env.example` and source control.

## Run locally

Install the locked project and development dependencies:

```powershell
uv sync --locked
```

For a host-installed MariaDB, `.env` uses `127.0.0.1:3307`. Create the database
and a least-privilege local user in a MariaDB client if they do not already
exist. Replace the example password before executing:

```sql
CREATE DATABASE the_last_seat;
CREATE USER 'lastseat'@'localhost' IDENTIFIED BY 'replace-with-a-local-password';
GRANT ALL PRIVILEGES ON the_last_seat.* TO 'lastseat'@'localhost';
```

If the database or user already exists, preserve it and adjust the grants as
needed; do not drop or recreate an existing database.

Initialize the database and seed demo inventory:

```powershell
uv run --locked python -m codechef_task.migrate
uv run --locked python -m codechef_task.seed
```

The migration and seed behavior is described in [Database setup and
migrations](#database-setup-and-migrations).

Start the API:

```powershell
uv run --locked uvicorn codechef_task.app:app --reload
```

The API listens at `http://127.0.0.1:8000`; Swagger UI is at
`http://127.0.0.1:8000/docs`.

## Database setup and migrations

Migrations are **not automatic** when the API starts. Run
`uv run --locked python -m codechef_task.migrate` after configuring database
access and before starting a new or updated deployment. The command first
calls SQLAlchemy `create_all()` to create missing tables, including the
`idempotency_records` table, then applies the repository's migration. It adds
unique keys on `bookings.reservation_id` and
`payments.booking_id`, and updates the booking status check to allow `pending`
delayed bookings. It checks for duplicate values before adding keys. It does
not drop tables or delete rows. It also adds nullable `user_ref` columns to
`reservations` and `bookings` when absent, preserving old rows with NULL values.
`create_all()` does not evolve existing table
layouts; this migration covers only schema changes currently implemented
here. Later schema evolution will need a versioned migration process. Never
use a database drop, volume deletion, or other destructive command as routine
setup.

The seed command creates or completes the `the-last-seat-demo` event with
exactly seat numbers 1 to 500. It is repeatable, does not duplicate seats, and
refuses to proceed if the event already has out-of-range seat numbers. It is
optional for deployment; do not seed production unless demo inventory is
intended.

## API endpoints

### `GET /health`

Checks that the API process responds:

```http
GET /health
```

```json
{"status":"healthy"}
```

### `GET /health/db`

Runs `SELECT 1`. On success:

```json
{"status":"healthy","database":"available"}
```

Request: `GET /health/db`. When the database is unavailable, it returns HTTP
503 with this generic response:

```json
{"detail":{"status":"unhealthy","database":"unavailable"}}
```

Credentials and internal exception details are not returned.

### Request IDs and logs

The API accepts an `X-Request-ID` header containing 1–128 ASCII letters,
digits, periods, underscores, colons, or hyphens. Missing or invalid values
are replaced with a generated UUID. The response always includes the chosen
value in `X-Request-ID`. Each request produces one access log with method,
path (without query parameters), status, and duration. The request ID is
included in log records, along with lifecycle entries for reservation
creation, expiry and cancellation, checkout, and delayed-payment settlement.
Those lifecycle records include `user_ref` alongside their existing entity IDs.
Unexpected exceptions include a traceback in server logs and return a generic
HTTP 500 response; expected HTTP errors retain their normal status and detail.
Request bodies, query strings, credentials, database URLs, tokens, and payment
secrets are not included in these logs.

Logging uses Python's standard `logging` module and writes to the process
console. Set `LOG_LEVEL=DEBUG` (or another listed level) in the local `.env`
to change the threshold. Docker Compose passes the same variable to the API
container and defaults it to `INFO`. Lower thresholds can produce more output;
use `INFO` for normal operation.

### Caller reference and ownership

Reservation creation, reservation retrieval/cancellation, checkout, delayed
payment settlement, and `GET /api/users/me/bookings` require `X-User-Ref`.
Values must contain 1–64 ASCII letters, digits, periods, underscores, colons,
or hyphens; missing or invalid values return HTTP 422. This caller-supplied
value supports lightweight ownership filtering only; it is **not
authentication**. A different non-NULL stored owner returns HTTP 403. Legacy
reservations and bookings with NULL ownership remain accessible to any caller
with a valid header. Owner comparisons are exact and case-sensitive.

### `POST /api/events/{event_id}/reservations`

Creates a temporary hold for an available seat in an open event:

```http
POST /api/events/1/reservations
X-User-Ref: alice-demo
Content-Type: application/json

{"seat_id":42}
```

Optionally send `Idempotency-Key: <client-generated-key>` to retry safely if a
client does not receive the response. Keys must be 1 to 128 characters, start
with an ASCII letter or digit, and contain only letters, digits, periods,
underscores, colons, or hyphens. A repeated key with the same
event, seat, and caller reference returns the stored original response; reusing
it with another caller or request returns HTTP 409. Without the key header,
existing seat-availability behavior is unchanged. Empty, malformed, or longer than 128-character keys
return HTTP 422.

HTTP 201 response:

```json
{
  "id": 17,
  "event_id": 1,
  "seat_id": 42,
  "status": "active",
  "created_at": "2026-10-16T12:00:00Z",
  "expires_at": "2026-10-16T12:05:00Z"
}
```

Unknown event/seat returns 404. Closed events or unavailable seats return 409.

PowerShell example (reuse the same `$headers` when retrying this logical
request):

```powershell
$headers = @{
  "X-User-Ref" = "alice-demo"
  "Idempotency-Key" = [guid]::NewGuid().ToString()
}
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/events/1/reservations" `
  -Headers $headers -ContentType "application/json" -Body '{"seat_id":42}'
```

### `GET /api/events/{event_id}/seats`

Lists seats in number order. Example request: `GET /api/events/1/seats`.
Example response:

```json
{
  "event_id": 1,
  "seats": [
    {"seat_id": 12, "seat_number": 1, "status": "available"},
    {"seat_id": 13, "seat_number": 2, "status": "reserved"},
    {"seat_id": 14, "seat_number": 3, "status": "booked"}
  ]
}
```

Statuses are `available`, `reserved`, or `booked`. The listing processes
elapsed active holds with the same locking and lazy-expiry behavior used by
reservation operations. It is a point-in-time view; a subsequent reservation
request can still lose a race and return 409. Unknown event returns 404.

### `GET /api/reservations/{reservation_id}`

Example request: `GET /api/reservations/17`. Returns HTTP 200 and the
reservation shape shown in the creation example, lazily persisting `expired`
when the hold deadline has passed. A different non-NULL owner returns 403;
unknown reservation returns 404.

### `DELETE /api/reservations/{reservation_id}`

Example request: `DELETE /api/reservations/17`. Cancels an active hold and
returns HTTP 200 with the updated reservation shape shown above (with `status`
set to `cancelled`). A terminal or expired hold returns 409. Cancellation does
not delete reservation history.
A different non-NULL owner returns 403.

### `POST /api/reservations/{reservation_id}/checkout`

Simulates checkout for an active, unexpired reservation. The request chooses
only a simulated outcome; price and currency are loaded from the event:

```http
POST /api/reservations/17/checkout
X-User-Ref: alice-demo
Content-Type: application/json

{"outcome":"success"}
```

This route also accepts the optional `Idempotency-Key` header with the same
format. Invalid keys return HTTP 422. The request fingerprint includes the
reservation ID, outcome, and caller reference. The same key and outcome from
the same caller replays the original response; another caller cannot replay
it. Changing the outcome with
that key returns HTTP 409. Without a key, the existing one-checkout-per-
reservation retry behavior remains in effect.

Immediate outcomes are `success` and `failure`. Example HTTP 201 response for
success:

```json
{
  "booking_id": 21,
  "reservation_id": 17,
  "booking_status": "confirmed",
  "payment_id": 33,
  "payment_status": "succeeded",
  "amount": "25.00",
  "currency": "USD"
}
```

For an asynchronous simulated response, send `{"outcome":"delayed"}`. Checkout
creates a `pending` booking and payment and leaves the reservation active. It
returns HTTP 201 in the same response shape, with both statuses set to
`pending`. Later, explicitly settle it:

```http
POST /api/payments/33/settle
X-User-Ref: alice-demo
Content-Type: application/json

{"outcome":"success"}
```

The settlement response is HTTP 200 with final booking/payment statuses. Use
`failure` to release the seat instead. Settlement is an explicit later
request, not a timer or background worker. If the reservation is cancelled or
expires first, its pending payment is changed to failed and its booking to
cancelled; it cannot later confirm. Repeated checkout or settlement returns
the stored result even if the retry supplies a different outcome. Expired or
cancelled reservations without an existing checkout return 409; unknown IDs
return 404. A different non-NULL owner returns 403. Database write errors
return a generic 409.

### `GET /api/events/{event_id}/bookings`

Returns event booking history, newest first, including cancelled and pending
records and optional payment details. Example request:
`GET /api/events/1/bookings?limit=100&offset=0`.

```json
{
  "event_id": 1,
  "items": [{
    "id": 21,
    "event_id": 1,
    "seat_id": 14,
    "reservation_id": 17,
    "status": "confirmed",
    "amount_paid": "25.00",
    "currency": "USD",
    "created_at": "2026-10-16T12:00:00Z",
    "payment": {
      "id": 33,
      "status": "succeeded",
      "amount": "25.00",
      "currency": "USD",
      "created_at": "2026-10-16T12:00:00Z"
    }
  }]
}
```

`limit` is 1–500 (default 100); `offset` defaults to 0. Unknown event returns
404. This is event-wide and unfiltered; it does not require an authenticated
owner. Use `/api/users/me/bookings` for caller-reference filtering.

### `GET /api/users/me/bookings`

Returns paginated booking records belonging to the caller-supplied
`X-User-Ref`, newest first, including payment details when present. Example:
`GET /api/users/me/bookings?limit=100&offset=0` with
`X-User-Ref: alice-demo`. `limit` is 1–500 (default 100), and `offset` defaults
to 0. The route name's “me” does not imply authentication; legacy NULL-owner
rows are not included in this filtered list.

### `POST /api/bookings/{booking_id}/cancel`

Cancels a confirmed booking while preserving its row. Example:
`POST /api/bookings/21/cancel`. The response is the booking-history item above
with `status: "cancelled"`; the seat becomes available for a new reservation.
Repeated cancellation returns the same record. The succeeded payment remains
in history: cancellation does not issue a refund. Only confirmed bookings can
be cancelled through this endpoint.

## API verification and troubleshooting

With the API running, check process and database health:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/health/db
```

The first endpoint verifies that the API responds; the second executes `SELECT
1` against the configured database and returns HTTP 503 when unavailable.
Open `http://127.0.0.1:8000/docs` for Swagger UI and interactive requests.
Useful routes include `GET /api/events/{event_id}/seats`,
`POST /api/events/{event_id}/reservations`,
`POST /api/reservations/{reservation_id}/checkout`,
`GET /api/reservations/{reservation_id}`, and
`GET /api/events/{event_id}/bookings`; request and response examples are in
[API endpoints](#api-endpoints).

For a local seeded demo, `seed.py` creates the event in `draft` state and the
reservation endpoint accepts only `open` events. There is no event-management
API. To make only that local demo event bookable, update it using a MariaDB
client after seeding:

```sql
UPDATE events SET status = 'open'
WHERE slug = 'the-last-seat-demo' AND status = 'draft';
```

For connection failures, check that MariaDB is running and reachable at the
configured host and port, verify `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`,
and `DB_PASSWORD`, then retry `/health/db`. For migration errors, inspect the
database error and user privileges; the migration can stop when duplicate
values prevent a unique constraint. Preserve the data and resolve reported
duplicates before retrying. If Uvicorn reports an occupied port, use
`--port 8001` and browse to port 8001. For Docker-specific startup steps, see
[Compose troubleshooting](#compose-troubleshooting).

## Schema, lifecycle, and concurrency

The five booking-domain models plus the idempotency record are shown in
[docs/ERD.md](docs/ERD.md); the state and lock flow is shown in
[docs/Booking-Workflow.md](docs/Booking-Workflow.md). Money uses
`NUMERIC(10, 2)`. Times use UTC. Composite seat foreign keys ensure a
reservation or booking points to a seat belonging to the same event.

Reservation states are `active`, `converted`, `expired`, and `cancelled`.
Checkout success changes `active → converted`; failure changes `active →
cancelled`. Failed payment keeps a cancelled booking and failed payment as
history while releasing inventory. Terminal reservations are not reactivated.
Booking states are `pending`, `confirmed`, and `cancelled`. A delayed checkout
persists pending booking/payment records while retaining its active hold. The
explicit settlement endpoint either confirms/converts/succeeds or
cancels/cancels/fails. Expiry or hold cancellation first also fails a pending
attempt.

Expiry is lazy: an API operation processes an elapsed hold; there is no expiry
worker. Until a request touches an expired hold, its stored status can remain
`active`, but reservation/checkout paths compare `expires_at` before proceeding.

For keyed reservation and checkout requests, the transaction first attempts
to insert its `(operation, key)` record. It then follows the same business
lock order as an unkeyed request: lock the concrete seat row with
`SELECT ... FOR UPDATE`, then lock the reservation. The seat lock serializes
business writers for that seat in InnoDB. Checkout writes booking, payment, and
reservation state inside one transaction, so an error rolls back all of them.
Database unique constraints back up application checks:

- `(event_id, active_seat_id)` permits at most one active reservation per seat.
- `(event_id, confirmed_seat_id)` permits at most one confirmed booking per
  seat. The generated seat key is NULL for pending and cancelled historical
  rows; pending inventory remains protected by its active reservation.
- Unique `bookings.reservation_id` permits one booking per reservation.
- Unique `payments.booking_id` permits one payment row per booking.
- `(event_id, seat_number)` prevents repeated seat numbers within an event.

Reservations and bookings have nullable `user_ref` columns. New reservation
requests persist their required header value; checkout copies it to the
booking. The migration adds these columns without backfilling legacy rows.
Ownership is checked inside existing seat-first reservation/checkout lock
flows, without adding locks. NULL-owner legacy rows remain accessible.

The existing checkout rule permits one attempt per reservation; a failed
payment is terminal and another attempt requires a new reservation. Explicit
idempotency responses have a durable `(operation, key)` scope: reservation
creation and checkout use distinct operation names, and fingerprints include
the path identifiers, relevant request fields, and `user_ref`. Since real
authentication is not implemented, keys remain globally unique per operation.
Clients should generate globally unique keys (UUIDs are suitable). A key reused
with different logical parameters or by another caller
returns HTTP 409. Different keys still pass through seat locks and the existing
database uniqueness constraints. Concurrent requests with the same key wait
on the database unique constraint and replay the committed response. The
business mutation and successful response record commit atomically; failed or
transient requests do not leave a cached result. Requests without a key retain
the prior behavior. In particular, checkout with a new key after an earlier
checkout still returns the original booking/payment under the reservation's
existing one-attempt rule. Idempotency records currently have no retention or
cleanup policy and remain stored indefinitely. A delayed outcome is resolved
by an explicit later settlement request; it does not sleep in a request or
create an untracked task.
This is a deterministic simulator, not payment provider integration, and it
does not create an external charge.

The MariaDB integration test exercises same-seat reservation, duplicate
checkout, and delayed-settlement concurrency. SQLite tests validate API and transaction behavior but
cannot establish InnoDB locking behavior. Locks coordinate only code paths
that follow the same seat-first protocol. There is no authentication: `X-User-Ref`
is caller-controlled identification, not proof of identity, so it is not a
security boundary and the API is not ready for public use. Event
administration, refunds, provider retries, and background expiry are not
implemented. A small reproducible load smoke test is documented in
[docs/load-test-results.md](docs/load-test-results.md); it is not a capacity
benchmark. No claim of 10,000 concurrent users is made. Scaling beyond a single
app/database setup would require representative load tests, database pool
tuning, and likely horizontally scaled API workers; shared database
constraints and row locks remain necessary across workers.

## Docker Compose

Docker Compose starts the API and MariaDB. The database has a persistent named
volume. The API connects to the service name `db` on container port 3306. Its
host-published port defaults to 3308 to avoid colliding with the existing local
MariaDB on 3307.

```powershell
Copy-Item .env.example .env
# Edit .env and replace all placeholder credentials.
docker compose up --build -d
docker compose ps
docker compose logs -f api
```

This starts `api` and `db`. The API container starts Uvicorn using the Dockerfile
command and waits for MariaDB's configured health check. The API connects to
`db:3306`; host access to the database is published on `127.0.0.1:3308` by
default. The API is published at `127.0.0.1:8000`. The API service does not
have a Compose health check configured; use the application health endpoints
to verify it.

After code changes, rebuild and recreate the API image with:

```powershell
docker compose up --build -d
docker compose ps
docker compose logs -f api
```

Inspect service status or logs at any time with `docker compose ps`,
`docker compose logs -f api`, or `docker compose logs -f db`.

Initialize schema and apply the checkout migration, then seed the demo event:

```powershell
docker compose exec api python -m codechef_task.migrate
docker compose exec api python -m codechef_task.seed
```

Check `http://127.0.0.1:8000/health` and
`http://127.0.0.1:8000/health/db`. The database can be reached from the host
on `127.0.0.1:3308` by default; containers use `db:3306`. To stop the services
while preserving the named database volume:

```powershell
docker compose down
```

Do not use `docker compose down -v` if you need to keep the container database.
The MariaDB image initializes credentials/database only for a new empty data
volume; changing `.env` later does not rewrite existing database users.
The Compose file was inspected, but the instructions below do not assert that
the stack has been started or runtime-tested in this environment.

### Compose troubleshooting

- If a port is occupied, local Uvicorn accepts another port, for example
  `uv run --locked uvicorn codechef_task.app:app --port 8001`. Compose publishes
  the API's host port as `8000`; change the host side of `8000:8000` in a local
  Compose override if that port is occupied. The database host port is
  configurable with `DOCKER_DB_PORT` (for example `3309`).
- If `docker compose up` cannot reach the daemon, start Docker Desktop (Windows)
  or the Docker Engine service, then retry `docker compose ps`.
- If the API starts before the database is healthy or cannot connect, check
  `docker compose ps` and `docker compose logs -f db`; confirm the API uses
  `DB_HOST=db` and `DB_PORT=3306` inside Compose. Never set the API container's
  database host to `127.0.0.1`, which points back to the API container.
- If MariaDB credentials or the database name are wrong, check `DB_NAME`,
  `DB_USER`, `DB_PASSWORD`, and `MARIADB_ROOT_PASSWORD` in the local `.env`.
  These initialization values are applied when the named volume is first
  created; changing them later does not rewrite accounts in an existing volume.
- If migration fails, inspect the API container output and MariaDB logs. Check
  database reachability and privileges, and resolve any duplicate values the
  migration reports before retrying. Do not delete the volume to bypass a
  migration error.

## Production deployment instructions

The repository contains a Dockerfile for the API and a Compose setup for local
API plus MariaDB development. No hosting provider or public deployment URL is
configured here; the public URL will be added after deployment. The Docker
image uses the startup command
`uvicorn codechef_task.app:app --host 0.0.0.0 --port 8000`, so configure the
hosting platform to route traffic to container port `8000`. No `PORT`
environment variable is currently read by the application. The platform's
HTTPS ingress should terminate TLS and forward traffic to the container over
its private network.

For a Docker-capable host, build from this repository's `Dockerfile` and run
the image as the API service. Configure `DB_HOST`, `DB_PORT`, `DB_NAME`,
`DB_USER`, `DB_PASSWORD`, and, when needed, `RESERVATION_HOLD_SECONDS` through
the provider's secret/environment settings. `DB_HOST` and `DB_PORT` must point
to a MariaDB-compatible TCP endpoint reachable from the API container. For a
hosted database, use the provider's private host and port, not `127.0.0.1`.
`MARIADB_ROOT_PASSWORD` and `DOCKER_DB_PORT` are used by this repository's
Compose database service; they are not API settings for an independently
managed production database.

Run `python -m codechef_task.migrate` as a one-off deployment task/container
using the same application image and database environment before routing user
traffic. The API does not apply migrations on startup. Only run
`python -m codechef_task.seed` if the demo event and inventory are wanted; the
seeded event is draft, and production event provisioning is not exposed as an
API. The migration currently creates missing tables and applies this project's
small explicit updates; later schema changes will need a maintained,
versioned migration process.

Use `/health` as a process/liveness check and `/health/db` as a database
readiness check. The latter executes a query. These endpoints exist in the
application, but Compose configures a health check only for MariaDB; configure
the hosting platform's API health probes separately. The repository does not
configure provider networking, HTTPS, domains, secrets storage, database
firewall rules, backups, or restore procedures. Keep those settings in the
hosting provider, restrict database access to the API service, and use secure
secret storage. Use a persistent hosted MariaDB service with backups; the
Compose named volume is for the local Compose environment and is not a
production backup plan.

The API can be demonstrated directly through its Swagger UI at
`https://<your-deployed-host>/docs` once the provider assigns a URL; there is
no custom frontend. Replace this placeholder with the real public URL after
deployment. This documentation does not claim the service is deployed or
production-ready: authentication/ownership checks, event administration,
refunds, real payment processing, and automatic expiry remain unimplemented.

### Pre-deployment checklist

- [ ] The full test suite passes.
- [ ] No `.env` file, credentials, or other secrets are committed.
- [ ] The database migration has been applied and reviewed for the target data.
- [ ] Production environment variables are configured in secure provider
      settings.
- [ ] The database is reachable from the API service over its configured
      private TCP endpoint.
- [ ] `/health` and `/health/db` return the expected responses.
- [ ] Swagger UI and representative seat, reservation, checkout, and history
      workflows have been tested.
- [ ] Persistent database storage, backups, and a restore approach are planned.

## Tests

Run the entire suite:

```powershell
uv run --locked --cache-dir .uv-cache pytest -q -p no:cacheprovider
```

SQLite-based tests need no MariaDB. The MariaDB concurrency test uses the
configured database when available and skips if the service cannot be reached.

## Basic load test

Start the API in one terminal, then run the repeatable contention test in
another:

```powershell
uv run --locked uvicorn codechef_task.app:app --host 127.0.0.1 --port 8000
uv run --locked python scripts/load_test.py --requests 100 --seats 10 --checkout-retries 3 --concurrency 50
```

The script requires the configured MariaDB and initialized schema. Each run
creates a uniquely named open event and its own small seat inventory, competes
reservation requests for those seats, then makes concurrent retries for each
successful checkout. It queries database invariants before deleting only that
run's event; event cascades remove its test rows. `--keep-data` retains the
isolated event for inspection. Results and limitations from the measured run
are recorded in [docs/load-test-results.md](docs/load-test-results.md).
