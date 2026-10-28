"""Late-cancel window, no-shows, waivers and studio cancellations."""
from datetime import timedelta

import pytest

from studio.models import Booking, CreditTransaction, StudioSettings
from studio.services.booking import (
    BookingError,
    CancelOutcome,
    book_event,
    book_session,
    cancel_booking,
    cancel_event,
    join_waitlist,
    mark_attendance,
)

from .conftest import at


@pytest.fixture
def booked(make_client, make_trainer, pack60, give, now):
    client, trainer = make_client(), make_trainer()
    pkg = give(client, pack60)
    booking = book_session(client, trainer, at(2, 10), 60, now=now)  # Wed 10:00, 49h away
    return booking, pkg


def test_cancel_outside_window_refunds(booked, now):
    booking, pkg = booked
    assert cancel_booking(booking, now=now) == CancelOutcome.REFUNDED
    booking.refresh_from_db(), pkg.refresh_from_db()
    assert booking.status == Booking.Status.CANCELLED
    assert pkg.credits_remaining == 10
    assert CreditTransaction.objects.filter(booking=booking, reason="cancel_refund", delta=1).exists()


def test_cancel_inside_window_uses_credit(booked):
    booking, pkg = booked
    just_inside = booking.event.start - timedelta(hours=23, minutes=59)
    assert cancel_booking(booking, now=just_inside) == CancelOutcome.LATE
    booking.refresh_from_db(), pkg.refresh_from_db()
    assert booking.status == Booking.Status.LATE_CANCEL
    assert pkg.credits_remaining == 9


def test_exactly_at_window_boundary_is_late(booked):
    booking, pkg = booked
    assert cancel_booking(booking, now=booking.event.start - timedelta(hours=24)) == CancelOutcome.LATE


def test_just_outside_window_is_not_late(booked):
    booking, pkg = booked
    assert cancel_booking(booking, now=booking.event.start - timedelta(hours=24, seconds=1)) == CancelOutcome.REFUNDED


def test_window_is_configurable(booked):
    booking, pkg = booked
    cfg = StudioSettings.load()
    cfg.late_cancel_hours = 12
    cfg.save()
    assert cancel_booking(booking, now=booking.event.start - timedelta(hours=20)) == CancelOutcome.REFUNDED


def test_staff_can_waive_late_cancel(booked):
    booking, pkg = booked
    assert cancel_booking(booking, now=booking.event.start - timedelta(hours=2), waive=True) == CancelOutcome.WAIVED
    pkg.refresh_from_db()
    assert pkg.credits_remaining == 10
    assert CreditTransaction.objects.filter(booking=booking, reason="waived").exists()


def test_late_cancel_still_frees_trainer_slot(booked, make_client, pack60, give):
    booking, _ = booked
    late = booking.event.start - timedelta(hours=3)
    cancel_booking(booking, now=late)
    other = make_client("Other")
    give(other, pack60)
    book_session(other, booking.event.trainer, booking.event.start, 60, now=late)


def test_no_show_keeps_credit_used(booked):
    booking, pkg = booked
    after = booking.event.start + timedelta(minutes=15)
    mark_attendance(booking, Booking.Status.NO_SHOW, now=after)
    booking.refresh_from_db(), pkg.refresh_from_db()
    assert booking.status == Booking.Status.NO_SHOW
    assert pkg.credits_remaining == 9


def test_cannot_cancel_after_start(booked):
    booking, _ = booked
    with pytest.raises(BookingError, match="no-show"):
        cancel_booking(booking, now=booking.event.start + timedelta(minutes=1))


def test_cannot_mark_attendance_before_start(booked, now):
    booking, _ = booked
    with pytest.raises(BookingError):
        mark_attendance(booking, Booking.Status.NO_SHOW, now=now)


def test_cannot_cancel_twice(booked, now):
    booking, pkg = booked
    cancel_booking(booking, now=now)
    with pytest.raises(BookingError):
        cancel_booking(booking, now=now)
    pkg.refresh_from_db()
    assert pkg.credits_remaining == 10  # not refunded twice


def test_studio_cancelling_class_refunds_everyone(make_client, make_class, class_pack, give, now):
    cls = make_class(capacity=1)
    a, b = make_client("A"), make_client("B")
    pa, _ = give(a, class_pack), give(b, class_pack)
    book_event(a, cls, now=now)
    wl = join_waitlist(b, cls, now=now)
    cancel_event(cls, now=cls.start - timedelta(hours=1))  # even inside the late window
    pa.refresh_from_db(), wl.refresh_from_db()
    assert pa.credits_remaining == 10
    assert wl.status == Booking.Status.REMOVED
