"""Waitlist joins and auto-promotion."""
from datetime import timedelta

import pytest

from studio.models import Booking, ClientProfile, Notification
from studio.services.booking import BookingError, book_event, book_session, cancel_booking, join_waitlist

from .conftest import at


@pytest.fixture
def full_class(make_client, make_class, class_pack, give, now):
    cls = make_class(capacity=2, start=at(3, 18))
    seated = [make_client(f"Seat{i}") for i in range(2)]
    for c in seated:
        give(c, class_pack)
        book_event(c, cls, now=now)
    return cls, seated


def test_join_waitlist_when_full_in_order(full_class, make_client, class_pack, give, now):
    cls, _ = full_class
    w1, w2 = make_client("W1"), make_client("W2")
    give(w1, class_pack), give(w2, class_pack)
    b1 = join_waitlist(w1, cls, now=now)
    b2 = join_waitlist(w2, cls, now=now)
    assert (b1.waitlist_position, b2.waitlist_position) == (1, 2)
    assert list(cls.waitlist()) == [b1, b2]


def test_waitlist_does_not_deduct_until_promoted(full_class, make_client, class_pack, give, now):
    cls, _ = full_class
    w = make_client("W")
    pkg = give(w, class_pack)
    join_waitlist(w, cls, now=now)
    pkg.refresh_from_db()
    assert pkg.credits_remaining == 10


def test_cannot_waitlist_open_class(make_client, make_class, class_pack, give, now):
    c = make_client()
    give(c, class_pack)
    with pytest.raises(BookingError, match="open spot"):
        join_waitlist(c, make_class(capacity=5), now=now)


def test_cannot_waitlist_without_credits(full_class, make_client, now):
    with pytest.raises(BookingError, match="credit"):
        join_waitlist(make_client("Broke"), full_class[0], now=now)


def test_cancel_promotes_first_in_line_and_charges_them(full_class, make_client, class_pack, give, now):
    cls, seated = full_class
    w1, w2 = make_client("W1"), make_client("W2")
    p1 = give(w1, class_pack)
    give(w2, class_pack)
    b1 = join_waitlist(w1, cls, now=now)
    b2 = join_waitlist(w2, cls, now=now)

    cancel_booking(Booking.objects.get(event=cls, client=seated[0]), now=now)

    b1.refresh_from_db(), b2.refresh_from_db(), p1.refresh_from_db()
    assert b1.status == Booking.Status.BOOKED and b1.waitlist_position is None
    assert b1.package == p1 and p1.credits_remaining == 9
    assert b2.status == Booking.Status.WAITLISTED
    assert cls.booked_count() == 2
    assert Notification.objects.filter(user=w1.user, subject__contains="You're in").exists()


def test_late_cancel_also_promotes(full_class, make_client, class_pack, give):
    cls, seated = full_class
    w = make_client("W")
    give(w, class_pack)
    b = join_waitlist(w, cls, now=cls.start - timedelta(days=2))
    late = cls.start - timedelta(hours=5)  # inside 24h window, outside 2h cutoff
    cancel_booking(Booking.objects.get(event=cls, client=seated[0]), now=late)
    b.refresh_from_db()
    assert b.status == Booking.Status.BOOKED


def test_no_promotion_inside_cutoff(full_class, make_client, class_pack, give, now):
    cls, seated = full_class
    w = make_client("W")
    give(w, class_pack)
    b = join_waitlist(w, cls, now=now)
    cancel_booking(Booking.objects.get(event=cls, client=seated[0]), now=cls.start - timedelta(minutes=90))
    b.refresh_from_db()
    assert b.status == Booking.Status.WAITLISTED
    assert cls.booked_count() == 1


def test_skips_clients_who_can_no_longer_book(full_class, make_client, make_trainer, class_pack, pack60, give, now):
    cls, seated = full_class
    flagged, conflicted, used_up, good = (make_client(n) for n in ("Flagged", "Conflicted", "UsedUp", "Good"))
    for c in (flagged, conflicted, used_up, good):
        give(c, class_pack, credits=1)
    give(conflicted, pack60)
    wl = {c.pk: join_waitlist(c, cls, now=now) for c in (flagged, conflicted, used_up, good)}

    ClientProfile.objects.filter(pk=flagged.pk).update(payment_status="failed")
    book_session(conflicted, make_trainer(), cls.start, 60, now=now)  # now busy at class time
    other = cls.__class__.objects.create(
        kind="class", title="Other", trainer=cls.trainer, start=cls.start + timedelta(days=1),
        end=cls.end + timedelta(days=1), capacity=5, credit_type="class",
    )
    book_event(used_up, other, now=now)  # spends their only class credit

    cancel_booking(Booking.objects.get(event=cls, client=seated[0]), now=now)

    statuses = {pk: Booking.objects.get(pk=b.pk).status for pk, b in wl.items()}
    assert statuses[flagged.pk] == Booking.Status.REMOVED
    assert statuses[conflicted.pk] == Booking.Status.REMOVED
    assert statuses[used_up.pk] == Booking.Status.REMOVED
    assert statuses[good.pk] == Booking.Status.BOOKED


def test_leaving_waitlist(full_class, make_client, class_pack, give, now):
    cls, _ = full_class
    w = make_client("W")
    give(w, class_pack)
    b = join_waitlist(w, cls, now=now)
    cancel_booking(b, now=now)
    b.refresh_from_db()
    assert b.status == Booking.Status.REMOVED
    # can rejoin later
    join_waitlist(w, cls, now=now)
