"""Package balance deduction and expired packages."""
from datetime import timedelta

import pytest

from studio.models import Booking, ClientPackage, CreditTransaction, CreditType, Membership, Product
from studio.services.booking import BookingError, book_event, book_session
from studio.services.credits import balances

from .conftest import at


def test_booking_deducts_one_credit_and_logs_it(make_client, make_trainer, pack60, give, now):
    client, trainer = make_client(), make_trainer()
    pkg = give(client, pack60)
    booking = book_session(client, trainer, at(1, 10), 60, now=now)

    pkg.refresh_from_db()
    assert pkg.credits_remaining == 9
    assert booking.package == pkg
    tx = CreditTransaction.objects.get(booking=booking)
    assert tx.delta == -1 and tx.reason == CreditTransaction.Reason.BOOKING
    # ledger reconciles with the balance
    assert sum(CreditTransaction.objects.filter(package=pkg).values_list("delta", flat=True)) == pkg.credits_remaining


def test_uses_soonest_expiring_package_first(make_client, make_trainer, pack60, give, now):
    client, trainer = make_client(), make_trainer()
    later = give(client, pack60, expires_days=180)
    sooner = give(client, pack60, expires_days=30)
    never = give(client, pack60, expires_days=0)  # 0 -> no expiry
    assert never.expires_at is None

    book_session(client, trainer, at(1, 10), 60, now=now)
    for p in (later, sooner, never):
        p.refresh_from_db()
    assert (sooner.credits_remaining, later.credits_remaining, never.credits_remaining) == (9, 10, 10)


def test_credit_type_must_match_session_length(make_client, make_trainer, pack60, give, now):
    client, trainer = make_client(), make_trainer()
    give(client, pack60)
    with pytest.raises(BookingError, match="30-min session credits"):
        book_session(client, trainer, at(1, 10), 30, now=now)
    assert not Booking.objects.exists()


def test_no_credits_blocks_booking_and_leaves_nothing_behind(make_client, make_trainer, now):
    client, trainer = make_client(), make_trainer()
    with pytest.raises(BookingError, match="don't have any"):
        book_session(client, trainer, at(1, 10), 60, now=now)
    assert not Booking.objects.exists()
    assert not trainer.events.exists()  # the slot isn't left blocked


def test_last_credit_then_empty(make_client, make_trainer, pack60, give, now):
    client, trainer = make_client(), make_trainer()
    give(client, pack60, credits=1)
    book_session(client, trainer, at(1, 10), 60, now=now)
    with pytest.raises(BookingError):
        book_session(client, trainer, at(1, 12), 60, now=now)
    assert balances(client, now)[CreditType.PT60]["credits"] == 0


def test_expired_package_cannot_be_used(make_client, make_trainer, pack60, give, now):
    client, trainer = make_client(), make_trainer()
    give(client, pack60, expires_days=10, at=now - timedelta(days=11))  # expired yesterday
    with pytest.raises(BookingError, match="expired"):
        book_session(client, trainer, at(1, 10), 60, now=now)
    bal = balances(client, now)[CreditType.PT60]
    assert bal["credits"] == 0 and bal["expired"] == 10


def test_package_must_be_valid_on_session_date(make_client, make_trainer, pack60, give, now):
    """A package that's valid today but expires before the session can't pay for it."""
    client, trainer = make_client(), make_trainer()
    give(client, pack60, expires_days=3, at=now - timedelta(days=1))  # expires day after tomorrow, 9am
    book_session(client, trainer, at(1, 10), 60, now=now)  # tomorrow: fine
    with pytest.raises(BookingError, match="expire"):
        book_session(client, trainer, at(5, 10), 60, now=now)  # after expiry


def test_falls_back_to_valid_package_when_one_expired(make_client, make_trainer, pack60, give, now):
    client, trainer = make_client(), make_trainer()
    expired = give(client, pack60, expires_days=5, at=now - timedelta(days=30))
    good = give(client, pack60)
    book_session(client, trainer, at(1, 10), 60, now=now)
    expired.refresh_from_db(), good.refresh_from_db()
    assert expired.credits_remaining == 10 and good.credits_remaining == 9


def test_unlimited_membership_books_without_using_credits(make_client, make_class, class_pack, give, now):
    client = make_client()
    pkg = give(client, class_pack)
    unlimited = Product.objects.create(
        name="Unlimited classes", kind=Product.Kind.MEMBERSHIP, credit_type=CreditType.CLASS, credits=None, price_cents=14900
    )
    m = Membership.objects.create(client=client, product=unlimited, status=Membership.Status.ACTIVE)
    booking = book_event(client, make_class(), now=now)
    pkg.refresh_from_db()
    assert booking.membership == m and booking.package is None
    assert pkg.credits_remaining == 10


def test_past_due_membership_does_not_cover_bookings(make_client, make_class, now):
    client = make_client()
    unlimited = Product.objects.create(
        name="Unlimited classes", kind=Product.Kind.MEMBERSHIP, credit_type=CreditType.CLASS, credits=None, price_cents=14900
    )
    Membership.objects.create(client=client, product=unlimited, status=Membership.Status.PAST_DUE)
    with pytest.raises(BookingError):
        book_event(client, make_class(), now=now)


def test_balance_sums_live_packages(make_client, pack60, give, now):
    client = make_client()
    give(client, pack60, credits=3)
    give(client, pack60, credits=4)
    give(client, pack60, credits=5, expires_days=1, at=now - timedelta(days=2))
    bal = balances(client, now)[CreditType.PT60]
    assert bal["credits"] == 7 and bal["expired"] == 5
    assert ClientPackage.objects.count() == 3
