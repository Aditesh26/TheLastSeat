import logging

from fastapi.testclient import TestClient

from codechef_task.app import app


def test_request_id_is_preserved_or_replaced_and_logged(caplog) -> None:
    caplog.set_level(logging.INFO)
    with TestClient(app) as client:
        accepted = client.get("/health?private=value", headers={"X-Request-ID": "review:123"})
        rejected = client.get("/health", headers={"X-Request-ID": "bad id"})
        oversized = client.get("/health", headers={"X-Request-ID": "x" * 129})

    assert accepted.headers["X-Request-ID"] == "review:123"
    assert 1 <= len(rejected.headers["X-Request-ID"]) <= 128
    assert rejected.headers["X-Request-ID"] != "bad id"
    assert 1 <= len(oversized.headers["X-Request-ID"]) <= 128
    assert oversized.headers["X-Request-ID"] != "x" * 129

    access = [record for record in caplog.records if "http.request" in record.message]
    assert len(access) == 3
    accepted_record = next(record for record in access if record.request_id == "review:123")
    assert "method=GET" in accepted_record.message
    assert "path=/health" in accepted_record.message
    assert "status=200" in accepted_record.message
    assert "duration_ms=" in accepted_record.message
    assert "private" not in accepted_record.message


def test_unexpected_exception_is_logged_once_and_response_is_generic(caplog) -> None:
    async def broken_endpoint():
        raise RuntimeError("internal database detail")

    app.add_api_route("/__test_unexpected_error", broken_endpoint, methods=["GET"])
    caplog.set_level(logging.INFO)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/__test_unexpected_error", headers={"X-Request-ID": "failure-42"})
    finally:
        app.router.routes[:] = [
            route
            for route in app.router.routes
            if getattr(route, "path", None) != "/__test_unexpected_error"
        ]

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert response.headers["X-Request-ID"] == "failure-42"
    exception_records = [
        record for record in caplog.records if record.message == "Unhandled exception while processing request"
    ]
    assert len(exception_records) == 1
    assert exception_records[0].request_id == "failure-42"
    assert exception_records[0].exc_info is not None
    assert "internal database detail" not in response.text
    assert any("status=500" in record.message for record in caplog.records if "http.request" in record.message)
