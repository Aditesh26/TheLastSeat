# Basic concurrent load test

## How to reproduce

Use the configured MariaDB database and apply the schema plus checkout
migration first. Start one local Uvicorn worker in a terminal:

```powershell
uv run --locked uvicorn codechef_task.app:app --host 127.0.0.1 --port 8000
```

Run the test from another terminal:

```powershell
uv run --locked python scripts/load_test.py --base-url http://127.0.0.1:8000 --requests 100 --seats 10 --checkout-retries 3 --concurrency 50
```

The script creates an open event with a random `load-test-<uuid>` slug and the
requested number of seats. It sends reservation requests round-robin across
that limited inventory, then sends the requested number of simultaneous
checkout calls for each successful reservation. The first checkout succeeds;
the concurrent retries should return the stored response. It queries active
holds before checkout and checks both active-hold and confirmed-booking
uniqueness after all requests finish.

By default, cleanup deletes only the newly created event by its primary key.
InnoDB cascading foreign keys remove its test seats, reservations, bookings,
and payments. The script never selects, updates, or deletes the demo event or
other event IDs. `--keep-data` opts out of cleanup for inspection. If the
process is forcibly terminated before cleanup, its printed unique slug can be
used to identify the leftover isolated event; verify the slug before removing
anything manually.

Options can be changed to repeat the run at another inventory, request count,
retry count, or client concurrency limit. The script exits nonzero if expected
status counts or database invariants do not match. The readiness probe is not
included in the reported load request counts.

## Measured run

Run date: 2026-10-16. One measured run was made on Windows with Python 3.14.1,
one local Uvicorn worker, and MariaDB 13.0.2 using InnoDB on port 3307. The
script used 50 concurrent client request slots, 10 seats, and a local HTTP
connection to `127.0.0.1:8765`. Other machine specifications and competing
system load were not captured. Response time includes time waiting for one of
the configured client request slots.

| Phase                       | Requests | 2xx successes | Expected 409 rejections | Unexpected errors | Duration |   Throughput |       p50 |       p95 |       p99 |
| --------------------------- | -------: | ------------: | ----------------------: | ----------------: | -------: | -----------: | --------: | --------: | --------: |
| Reservation contention      |      100 |            10 |                      90 |                 0 |  0.322 s | 311.01 req/s | 229.88 ms | 297.96 ms | 301.24 ms |
| Concurrent checkout retries |       30 |            30 |                       0 |                 0 |  0.189 s | 158.59 req/s | 141.31 ms | 171.17 ms | 175.52 ms |
| Combined request phases     |      130 |            40 |                      90 |                 0 |  0.511 s | 254.55 req/s | 183.30 ms | 294.14 ms | 301.03 ms |

The reservation phase returned 10 HTTP 201 responses and 90 HTTP 409
responses. Checkout returned 10 HTTP 201 responses and 20 HTTP 200 idempotent
replays. There were no other response codes, network errors, or timeouts.

Database checks passed after reservation contention: 10 active reservations
and zero duplicate active seats. Before cleanup, the event had 10 seats, 10
reservations, zero active reservations, 10 bookings (all confirmed), and 10
payments. Both duplicate-active-seat and duplicate-confirmed-seat queries
returned zero rows. The script reported successful cleanup of the test event
and its cascading rows, then checked that no rows remained for the test event.

## Measured run: 1,000 requests and 500 seats

Run date: 2026-10-26. The run targeted the local API at
`http://127.0.0.1:8000` on Windows with Python 3.14.1 and MariaDB
13.0.2-MariaDB, InnoDB, port 3307. The client concurrency limit was 1,000;
the workload made 1,000 reservation requests against 500 seats, then made
three concurrent checkout calls per successful reservation (1,500 checkout
requests). The API worker count and machine CPU/RAM were not captured.

| Phase                       | Requests | 2xx successes | Expected 409 rejections | Unexpected errors | Duration |   Throughput |       p50 |       p95 |       p99 |
| --------------------------- | -------: | ------------: | ----------------------: | ----------------: | -------: | -----------: | --------: | --------: | --------: |
| Reservation contention      |    1,000 |           500 |                     500 |                 0 | 11.795 s |  84.78 req/s | 6015.64 ms | 10832.13 ms | 11316.25 ms |
| Concurrent checkout retries |    1,500 |         1,500 |                       0 |                 0 | 22.286 s |  67.31 req/s | 12036.08 ms | 20436.69 ms | 21067.82 ms |
| Combined request phases     |    2,500 |         2,000 |                     500 |                 0 | 34.081 s |  73.36 req/s | 8366.02 ms | 20109.42 ms | 20785.90 ms |

Reservation responses were 500 HTTP 201 and 500 HTTP 409. Checkout responses
were 500 HTTP 201 and 1,000 HTTP 200 retries. No unexpected status codes,
network errors, or timeouts were reported. The database check found exactly
500 seats, 500 reservations (zero still active after checkout), 500 confirmed
bookings, and 500 payments. There were zero duplicate active-reservation seats
and zero duplicate confirmed-booking seats. All invariants passed, and the
script deleted its unique test event and cascaded test rows.

This was 1,000 simultaneous client requests, **not 1,000 distinct users**:
the script sends the same valid `X-User-Ref` (`LOADTEST01`) for every request.
Checkout retries use the application's existing repeated-checkout behavior;
this run does not benchmark distinct user identities or an explicit
`Idempotency-Key` workload. The approximately 6–21 second response-time
percentiles show substantial latency under this local contention workload.
This is a correctness/contention smoke test, not evidence of production capacity
or a users-per-second guarantee. Server process configuration, worker count,
host hardware, and background machine load were not captured.

## Measured run: 10,000 requests and 500 seats

Run date: 2026-10-26, based on the terminal output supplied for this run.
The test targeted `http://127.0.0.1:8000` on Windows with Python 3.14.1 and
MariaDB 13.0.2-MariaDB, InnoDB, port 3307. The configured client concurrency
limit was 100, with 10,000 reservation requests against 500 seats, followed
by three checkout calls per successful reservation (1,500 checkout requests).
The API worker count and machine CPU/RAM were not captured.

| Phase                       | Requests | 2xx successes | Expected 409 rejections | Unexpected errors | Duration |    Throughput |       p50 |       p95 |       p99 |
| --------------------------- | -------: | ------------: | ----------------------: | ----------------: | -------: | ------------: | --------: | --------: | --------: |
| Reservation contention      |   10,000 |           500 |                   9,500 |                 0 | 61.869 s | 161.63 req/s | 27774.40 ms | 58462.22 ms | 61224.85 ms |
| Concurrent checkout retries |    1,500 |         1,500 |                       0 |                 0 |  9.501 s | 157.88 req/s | 5025.47 ms | 9127.30 ms | 9329.46 ms |
| Combined request phases     |   11,500 |         2,000 |                   9,500 |                 0 | 71.370 s | 161.13 req/s | 22798.70 ms | 57948.19 ms | 61141.83 ms |

Reservation responses were 500 HTTP 201 and 9,500 HTTP 409. Checkout returned
500 HTTP 201 and 1,000 HTTP 200 retries. The supplied output reports no
unexpected statuses or request errors. Database checks reported 500 seats,
500 reservations (zero still active after checkout), 500 confirmed bookings,
and 500 payments; both duplicate-seat checks returned zero. All invariants
passed, and cleanup deleted the unique test event and its cascading test rows.

This is **10,000 requests at a concurrency limit of 100**, not 10,000
simultaneous users. The reported response-time percentiles are high: reservation
p95 was about 58.5 seconds and p99 about 61.2 seconds. These are local results
from the supplied output; the script uses one `X-User-Ref` for every request,
so this also does not model 10,000 distinct identities. It is a contention and
correctness exercise, not a capacity claim. Worker count, hardware, and
background system load were not recorded.

## What this does and does not demonstrate

This is a small local smoke/load test of HTTP routes plus MariaDB row locking;
it is not a production capacity or endurance benchmark. It covers one
reservation contention burst and concurrent idempotent checkout retries for
the winners. It does not test multiple API workers, remote network latency,
connection-pool saturation at high volume, long-duration traffic, client
think-time, or 10,000 concurrent users. Results are a single run and vary with
hardware, server load, database configuration, and connection-pool settings.
