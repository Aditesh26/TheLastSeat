"""Central standard-library logging setup and request correlation context."""

from __future__ import annotations

import contextvars
import logging

from codechef_task.config import settings

request_id_context: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


class RequestIdFilter(logging.Filter):
    """Add the current request ID to each record, including non-request logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_context.get()
        return True


def configure_logging() -> None:
    """Configure one concise console handler using the configured log level."""
    root = logging.getLogger()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s request_id=%(request_id)s "
        "%(name)s %(message)s"
    )
    if not root.handlers:
        logging.basicConfig(
            level=settings.log_level,
            handlers=[logging.StreamHandler()],
        )
    else:
        root.setLevel(settings.log_level)
    for handler in root.handlers:
        handler.setFormatter(formatter)
        if not any(isinstance(item, RequestIdFilter) for item in handler.filters):
            handler.addFilter(RequestIdFilter())

    # The factory also adds the field to records captured by handlers that are
    # installed later (for example, pytest's caplog handler).
    current_factory = logging.getLogRecordFactory()
    if not getattr(current_factory, "_last_seat_request_id_factory", False):
        def record_factory(*args, **kwargs):
            record = current_factory(*args, **kwargs)
            record.request_id = request_id_context.get()
            return record

        record_factory._last_seat_request_id_factory = True  # type: ignore[attr-defined]
        logging.setLogRecordFactory(record_factory)
