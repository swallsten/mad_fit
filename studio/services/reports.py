"""Trainer payroll report."""
from datetime import time, timedelta

from django.db.models import Count, Q
from django.utils import timezone

from studio.models import Booking, CreditType, Event, TrainerProfile

from .booking import local_dt


def _delivered(qs, now):
    """Seats that count as delivered: marked attended, or still 'booked' once
    the session has ended (trainers don't always mark attendance)."""
    return qs.filter(
        Q(status=Booking.Status.ATTENDED) | Q(status=Booking.Status.BOOKED, event__end__lte=now)
    )


def trainer_report(start_date, end_date, now=None):
    """One row per trainer for events starting between start_date and
    end_date (inclusive, studio-local dates)."""
    now = now or timezone.now()
    start = local_dt(start_date, time.min)
    end = local_dt(end_date + timedelta(days=1), time.min)
    rows = []
    for trainer in TrainerProfile.objects.select_related("user"):
        bookings = Booking.objects.filter(event__trainer=trainer, event__start__gte=start, event__start__lt=end)
        delivered = _delivered(bookings, now)
        held = Event.objects.filter(
            trainer=trainer, start__gte=start, start__lt=end, end__lte=now, status=Event.Status.SCHEDULED
        )
        classes = held.filter(kind=Event.Kind.CLASS).annotate(
            n=Count("bookings", filter=Q(bookings__status__in=[Booking.Status.ATTENDED, Booking.Status.BOOKED]))
        )
        groups = held.filter(kind=Event.Kind.GROUP).annotate(
            n=Count("bookings", filter=Q(bookings__status__in=[Booking.Status.ATTENDED, Booking.Status.BOOKED]))
        ).filter(n__gt=0)
        row = {
            "trainer": trainer,
            "sessions_30": delivered.filter(event__kind=Event.Kind.SESSION, event__credit_type=CreditType.PT30).count(),
            "sessions_60": delivered.filter(event__kind=Event.Kind.SESSION, event__credit_type=CreditType.PT60).count(),
            "group_sessions": groups.count(),
            "group_clients": sum(g.n for g in groups),
            "classes": classes.count(),
            "class_attendees": sum(c.n for c in classes),
            "late_cancels": bookings.filter(status=Booking.Status.LATE_CANCEL).count(),
            "no_shows": bookings.filter(status=Booking.Status.NO_SHOW).count(),
        }
        row["session_hours"] = (row["sessions_30"] * 30 + row["sessions_60"] * 60) / 60
        rows.append(row)
    return rows


REPORT_COLUMNS = [
    ("trainer", "Trainer"),
    ("sessions_30", "1:1 30-min"),
    ("sessions_60", "1:1 60-min"),
    ("session_hours", "1:1 hours"),
    ("group_sessions", "Small-group sessions"),
    ("group_clients", "Small-group clients"),
    ("classes", "Classes taught"),
    ("class_attendees", "Class attendees"),
    ("late_cancels", "Late cancels"),
    ("no_shows", "No-shows"),
]
