from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class used by all ORM models."""

    pass


class EventStatus(StrEnum):
    DRAFT = "draft"
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class ReservationStatus(StrEnum):
    ACTIVE = "active"
    CONVERTED = "converted"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class BookingStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUNDED = "refunded"


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint("ticket_price >= 0", name="ck_events_ticket_price_nonnegative"),
        CheckConstraint(
            "status IN ('draft', 'open', 'closed', 'cancelled')",
            name="ck_events_status",
        ),
        Index("ix_events_status_starts_at", "status", "starts_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ticket_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, default="USD")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=EventStatus.DRAFT.value
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class Seat(Base):
    __tablename__ = "seats"
    __table_args__ = (
        CheckConstraint("seat_number > 0", name="ck_seats_seat_number_positive"),
        UniqueConstraint("event_id", "seat_number", name="uq_seats_event_seat_number"),
        # Supports composite foreign keys that ensure a booking or reservation's
        # event matches the event that owns its seat.
        UniqueConstraint("event_id", "id", name="uq_seats_event_id_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    seat_number: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["event_id", "seat_id"],
            ["seats.event_id", "seats.id"],
            name="fk_reservations_event_seat",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status IN ('active', 'converted', 'expired', 'cancelled')",
            name="ck_reservations_status",
        ),
        Index(
            "uq_reservations_active_event_seat",
            "event_id",
            "active_seat_id",
            unique=True,
        ),
        Index("ix_reservations_status_expires_at", "status", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    seat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ReservationStatus.ACTIVE.value
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    active_seat_id: Mapped[int | None] = mapped_column(
        Integer,
        Computed(
            "CASE WHEN status = 'active' THEN seat_id ELSE NULL END", persisted=True
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["event_id", "seat_id"],
            ["seats.event_id", "seats.id"],
            name="fk_bookings_event_seat",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["reservation_id"],
            ["reservations.id"],
            name="fk_bookings_reservation",
            ondelete="SET NULL",
        ),
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'cancelled')",
            name="ck_bookings_status",
        ),
        CheckConstraint("amount_paid >= 0", name="ck_bookings_amount_nonnegative"),
        UniqueConstraint("reservation_id", name="uq_bookings_reservation_id"),
        Index(
            "uq_bookings_confirmed_event_seat",
            "event_id",
            "confirmed_seat_id",
            unique=True,
        ),
        Index("ix_bookings_status_created_at", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    seat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    reservation_id: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=BookingStatus.CONFIRMED.value
    )
    amount_paid: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    confirmed_seat_id: Mapped[int | None] = mapped_column(
        Integer,
        Computed(
            "CASE WHEN status = 'confirmed' THEN seat_id ELSE NULL END", persisted=True
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payments_amount_positive"),
        CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed', 'refunded')",
            name="ck_payments_status",
        ),
        UniqueConstraint("booking_id", name="uq_payments_booking_id"),
        Index("ix_payments_booking_status", "booking_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PaymentStatus.PENDING.value
    )
    provider_reference: Mapped[str | None] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class IdempotencyRecord(Base):
    """Durable successful response for one client request key and operation."""

    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("operation", "key", name="uq_idempotency_operation_key"),
        Index("ix_idempotency_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    key: Mapped[str] = mapped_column(
        String(128).with_variant(
            String(128, collation="utf8mb4_bin"), "mysql"
        ),
        nullable=False,
    )
    request_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
