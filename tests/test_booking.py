"""Double-booking prevention, availability and booking rules."""
import threading
from datetime import time, timedelta

import pytest
from django.db import connection

from studio.models import Booking, ClientProfile, Event, TimeOff
from studio.services.booking import (
    BookingError,
    EventFull,
    available_slots,
    book_event,
    book_session,
    cancel_booking,
    create_group_session,
)

from .conftest import MONDAY, at


def test_trainer_cannot_be_double_booked(make_client, make_trainer, pack60, give, now):
    trainer = make_trainer()
    a, b = make_client("A"), make_client("B")
    give(a, pack60), give(b, pack60)
    book_session(a, trainer, at(1, 10), 60, now=now)
    with pytest.raises(BookingError, match="already booked"):
        book_session(b, trainer, at(1, 10), 60, now=now)
    # partial overlap too
    with pytest.raises(BookingError, match="already booked"):
        book_session(b, trainer, at(1, 10, 30), 30, now=now)
    # back-to-back is fine
    book_session(b, trainer, at(1, 11), 60, now=now)


def test_trainer_class_blocks_1on1(make_client, make_trainer, make_class, pack60, give, now):
    trainer = make_trainer()
    make_class(trainer=trainer, start=at(1, 10))
    client = make_client()
    give(client, pack60)
    with pytest.raises(BookingError, match="already booked"):
        book_session(client, trainer, at(1, 10, 30), 60, now=now)


def test_client_cannot_be_double_booked(make_client, make_trainer, make_class, pack60, class_pack, give, now):
    client = make_client()
    give(client, pack60), give(client, class_pack)
    t1, t2 = make_trainer("One"), make_trainer("Two")
    book_session(client, t1, at(1, 10), 60, now=now)
    with pytest.raises(BookingError, match="overlaps"):
        book_session(client, t2, at(1, 10, 30), 60, now=now)
    cls = make_class(start=at(1, 10, 15))
    with pytest.raises(BookingError, match="overlaps"):
        book_event(client, cls, now=now)


def test_cancelled_booking_frees_the_slot(make_client, make_trainer, pack60, give, now):
    trainer = make_trainer()
    a, b = make_client("A"), make_client("B")
    give(a, pack60), give(b, pack60)
    booking = book_session(a, trainer, at(3, 10), 60, now=now)
    assert at(3, 10) not in available_slots(trainer, MONDAY + timedelta(days=3), 60, now)
    cancel_booking(booking, now=now)
    assert at(3, 10) in available_slots(trainer, MONDAY + timedelta(days=3), 60, now)
    book_session(b, trainer, at(3, 10), 60, now=now)


def test_same_client_cannot_book_class_twice(make_client, make_class, class_pack, give, now):
    client = make_client()
    give(client, class_pack)
    cls = make_class(capacity=5)
    book_event(client, cls, now=now)
    with pytest.raises(BookingError, match="Already booked"):
        book_event(client, cls, now=now)


def test_class_capacity_enforced(make_client, make_class, class_pack, give, now):
    cls = make_class(capacity=2)
    clients = [make_client(str(i)) for i in range(3)]
    for c in clients:
        give(c, class_pack)
    book_event(clients[0], cls, now=now)
    book_event(clients[1], cls, now=now)
    with pytest.raises(EventFull):
        book_event(clients[2], cls, now=now)
    assert cls.booked_count() == 2


def test_respects_availability_and_time_off(make_client, make_trainer, pack60, give, now):
    trainer = make_trainer(available=False)
    trainer.availability.create(weekday=1, start_time=time(8), end_time=time(12))  # Tuesdays 8-12
    client = make_client()
    give(client, pack60)
    with pytest.raises(BookingError, match="isn't available"):
        book_session(client, trainer, at(1, 12), 60, now=now)  # Tue 12-1
    TimeOff.objects.create(trainer=trainer, start=at(1, 8), end=at(1, 10))
    with pytest.raises(BookingError, match="is off"):
        book_session(client, trainer, at(1, 9), 60, now=now)
    # staff can override availability but never time off
    with pytest.raises(BookingError, match="is off"):
        book_session(client, trainer, at(1, 9), 60, now=now, staff_override=True)
    book_session(client, trainer, at(1, 13), 60, now=now, staff_override=True)
    slots = available_slots(trainer, MONDAY + timedelta(days=1), 60, now)
    assert slots == [at(1, 10), at(1, 10, 30), at(1, 11)]


def test_minimum_notice_and_past_times(make_client, make_trainer, pack60, give, now):
    trainer, client = make_trainer(), make_client()
    give(client, pack60)
    with pytest.raises(BookingError, match="passed"):
        book_session(client, trainer, now - timedelta(hours=1), 60, now=now)
    with pytest.raises(BookingError, match="at least 2 hours"):
        book_session(client, trainer, now + timedelta(hours=1), 60, now=now)


def test_payment_flag_blocks_new_bookings(make_client, make_trainer, make_class, pack60, class_pack, give, now):
    client, trainer = make_client(), make_trainer()
    give(client, pack60), give(client, class_pack)
    existing = book_session(client, trainer, at(2, 10), 60, now=now)
    client.payment_status = ClientProfile.PaymentStatus.FAILED
    client.save()
    with pytest.raises(BookingError, match="payment"):
        book_session(client, trainer, at(1, 10), 60, now=now)
    with pytest.raises(BookingError, match="payment"):
        book_event(client, make_class(), now=now)
    existing.refresh_from_db()
    assert existing.status == Booking.Status.BOOKED  # existing bookings are kept
    cancel_booking(existing, now=now)  # and can still be cancelled


def test_group_session_cannot_overlap_trainer(make_trainer, make_class, now):
    trainer = make_trainer()
    make_class(trainer=trainer, start=at(1, 10))
    with pytest.raises(BookingError):
        create_group_session(trainer, at(1, 10, 30), 60, 3)
    ev = create_group_session(trainer, at(1, 11), 60, 3)
    assert ev.kind == Event.Kind.GROUP


@pytest.mark.django_db(transaction=True)
def test_simultaneous_requests_cannot_double_book(make_client, make_trainer, pack60, give, now):
    """Two clients hit 'book' for the same trainer slot at the same moment."""
    trainer = make_trainer()
    clients = [make_client(str(i)) for i in range(4)]
    for c in clients:
        give(c, pack60)
    barrier = threading.Barrier(len(clients))
    results = []

    def attempt(client):
        try:
            barrier.wait()
            book_session(client, trainer, at(1, 10), 60, now=now)
            results.append("ok")
        except BookingError:
            results.append("rejected")
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt, args=(c,)) for c in clients]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["ok", "rejected", "rejected", "rejected"]
    assert Event.objects.filter(trainer=trainer, status="scheduled").count() == 1
