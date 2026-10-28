"""Builds the time-grid columns used by the admin and trainer calendars."""
from datetime import date, datetime, time, timedelta

from django.db.models import Count, Prefetch, Q
from django.urls import reverse
from django.utils import timezone

from .models import Booking, Event, TimeOff
from .services.booking import day_bounds, local_dt

GRID_START_HOUR = 5
GRID_END_HOUR = 21
PX_PER_MIN = 1.0


def hour_labels():
    return [
        {"top": (h - GRID_START_HOUR) * 60 * PX_PER_MIN, "label": datetime.combine(date.today(), time(h)).strftime("%-I %p")}
        for h in range(GRID_START_HOUR, GRID_END_HOUR)
    ]


def grid_height():
    return (GRID_END_HOUR - GRID_START_HOUR) * 60 * PX_PER_MIN


def _place(day, start, end):
    """top/height in px for an interval on day, clipped to the grid; None if outside."""
    grid_start = local_dt(day, time(GRID_START_HOUR))
    grid_end = local_dt(day, time(GRID_END_HOUR - 1, 59))
    s, e = max(start, grid_start), min(end, grid_end)
    if e <= s:
        return None
    return {
        "top": (s - grid_start).total_seconds() / 60 * PX_PER_MIN,
        "height": max((e - s).total_seconds() / 60 * PX_PER_MIN, 18),
    }


def events_qs(start, end, trainers):
    seated = Q(bookings__status__in=Booking.SEATED_STATUSES)
    return (
        Event.objects.filter(trainer__in=trainers, start__lt=end, end__gt=start, status=Event.Status.SCHEDULED)
        .select_related("trainer__user")
        .annotate(n_booked=Count("bookings", filter=seated), n_wait=Count("bookings", filter=Q(bookings__status="waitlisted")))
        .prefetch_related(
            Prefetch(
                "bookings",
                queryset=Booking.objects.filter(status__in=Booking.SEATED_STATUSES).select_related("client__user"),
                to_attr="seated",
            )
        )
        .order_by("start")
    )


def event_label(ev):
    if ev.kind == Event.Kind.SESSION:
        return ev.seated[0].client.name if ev.seated else ev.title
    return ev.title


def column(label, day, trainer, events, sublabel="", label_url=""):
    blocks = []
    for window in trainer.availability.all():
        if window.weekday == day.weekday():
            pos = _place(day, local_dt(day, window.start_time), local_dt(day, window.end_time))
            if pos:
                blocks.append({**pos, "cls": "avail"})
    day_start, day_end = day_bounds(day)
    for off in trainer.time_off.all():
        if off.start < day_end and off.end > day_start:
            pos = _place(day, off.start, off.end)
            if pos:
                blocks.append({**pos, "cls": "off", "title": "Time off", "sub": off.reason})
    for ev in events:
        if ev.trainer_id != trainer.pk or not (ev.start < day_end and ev.end > day_start):
            continue
        pos = _place(day, ev.start, ev.end)
        if not pos:
            continue
        ls = timezone.localtime(ev.start)
        sub = f"{ls:%-I:%M%p}".lower() + f" · {ev.duration_minutes}m"
        if ev.kind != Event.Kind.SESSION:
            sub += f" · {ev.n_booked}/{ev.capacity}"
            if ev.n_wait:
                sub += f" +{ev.n_wait}wl"
        blocks.append(
            {
                **pos,
                "cls": f"ev ev-{ev.kind}",
                "title": event_label(ev),
                "sub": sub,
                "url": reverse("event_detail", args=[ev.pk]),
                "color": ev.trainer.color,
            }
        )
    return {"label": label, "sublabel": sublabel, "label_url": label_url, "blocks": blocks}


def week_start(day):
    return day - timedelta(days=day.weekday())
