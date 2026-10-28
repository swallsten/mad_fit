"""Trainer payroll report counts."""
from datetime import timedelta

from studio.models import Booking, Product
from studio.services.booking import book_event, book_session, cancel_booking, mark_attendance
from studio.services.reports import trainer_report

from .conftest import MONDAY, at


def test_report_counts_delivered_late_cancel_and_no_show(make_client, make_trainer, make_class, pack60, class_pack, give, now):
    trainer = make_trainer("Pay")
    pack30 = Product.objects.create(name="30s", kind="package", credit_type="pt30", credits=10, price_cents=1)
    clients = [make_client(str(i)) for i in range(5)]
    for c in clients:
        give(c, pack60), give(c, pack30), give(c, class_pack)

    attended = book_session(clients[0], trainer, at(1, 8), 60, now=now)
    unmarked = book_session(clients[1], trainer, at(1, 10), 30, now=now)  # never marked: still counts once ended
    no_show = book_session(clients[2], trainer, at(1, 12), 60, now=now)
    late = book_session(clients[3], trainer, at(1, 14), 60, now=now)
    on_time = book_session(clients[4], trainer, at(1, 16), 60, now=now)
    cancel_booking(late, now=at(1, 10))
    cancel_booking(on_time, now=now)
    cls = make_class(trainer=trainer, start=at(1, 18), capacity=5)
    for c in clients[:3]:
        book_event(c, cls, now=now)
    later = at(2, 9)
    mark_attendance(attended, Booking.Status.ATTENDED, now=later)
    mark_attendance(no_show, Booking.Status.NO_SHOW, now=later)

    other = make_trainer("Other")
    row = {r["trainer"].pk: r for r in trainer_report(MONDAY, MONDAY + timedelta(days=6), now=later)}[trainer.pk]
    assert row["sessions_60"] == 1  # the no-show, late cancel and on-time cancel don't count
    assert row["sessions_30"] == 1
    assert row["session_hours"] == 1.5
    assert row["classes"] == 1 and row["class_attendees"] == 3
    assert row["late_cancels"] == 1 and row["no_shows"] == 1
    other_row = [r for r in trainer_report(MONDAY, MONDAY + timedelta(days=6), now=later) if r["trainer"] == other][0]
    assert other_row["sessions_60"] == 0

    # date range excludes outside events; future events aren't "delivered" yet
    assert {r["trainer"].pk: r for r in trainer_report(MONDAY + timedelta(days=2), MONDAY + timedelta(days=6), now=later)}[trainer.pk]["sessions_60"] == 0
    assert {r["trainer"].pk: r for r in trainer_report(MONDAY, MONDAY + timedelta(days=6), now=now)}[trainer.pk]["sessions_30"] == 0
