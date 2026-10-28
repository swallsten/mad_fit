"""Booking, cancellation, waitlist and scheduling rules.

Double-booking protection: every write path runs inside transaction.atomic()
and locks the trainer, event and client rows (select_for_update on Postgres;
on SQLite the IMMEDIATE transaction mode serializes writers). Conflict checks
run after the locks are held, so two simultaneous requests can't both pass.
Lock order is always trainer -> event -> client to avoid deadlocks.
"""
from datetime import datetime, time, timedelta

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from studio.models import (
    SESSION_CREDIT_FOR_DURATION,
    Availability,
    Booking,
    ClassTemplate,
    ClientProfile,
    CreditTransaction,
    CreditType,
    Event,
    Notification,
    StudioSettings,
    TimeOff,
    TrainerProfile,
)

from .credits import BookingError, can_pay_for, consume_credit, refund_credit


class EventFull(BookingError):
    pass


def local_dt(day, t):
    """Aware datetime for a studio-local date and wall-clock time."""
    return timezone.make_aware(datetime.combine(day, t))


def day_bounds(day):
    return local_dt(day, time.min), local_dt(day + timedelta(days=1), time.min)


# ---------------------------------------------------------------- conflicts


def trainer_conflicts(trainer, start, end, exclude_event_id=None):
    qs = Event.objects.filter(trainer=trainer, status=Event.Status.SCHEDULED, start__lt=end, end__gt=start)
    if exclude_event_id:
        qs = qs.exclude(pk=exclude_event_id)
    return qs


def client_conflicts(client, start, end, exclude_event_id=None):
    qs = Booking.objects.filter(
        client=client,
        status__in=Booking.ACTIVE_STATUSES,
        event__status=Event.Status.SCHEDULED,
        event__start__lt=end,
        event__end__gt=start,
    )
    if exclude_event_id:
        qs = qs.exclude(event_id=exclude_event_id)
    return qs


def on_time_off(trainer, start, end):
    return TimeOff.objects.filter(trainer=trainer, start__lt=end, end__gt=start).exists()


def within_availability(trainer, start, end):
    ls, le = timezone.localtime(start), timezone.localtime(end)
    if ls.date() != le.date() and le.time() != time.min:
        return False
    end_t = le.time() if le.time() != time.min else time.max
    return Availability.objects.filter(
        trainer=trainer, weekday=ls.weekday(), start_time__lte=ls.time(), end_time__gte=end_t
    ).exists()


def check_client_can_book(client):
    if not client.active:
        raise BookingError("This client account is inactive.")
    if client.is_payment_flagged:
        raise BookingError(
            "There's a problem with the last payment on this account. "
            "Update the card on file (Billing) before making new bookings."
        )


# ---------------------------------------------------------------- slots


def available_slots(trainer, day, duration_minutes, now=None, enforce_notice=True):
    """Open start times for a 1:1 session with trainer on a studio-local date."""
    now = now or timezone.now()
    cfg = StudioSettings.load()
    step = timedelta(minutes=cfg.slot_step_minutes)
    length = timedelta(minutes=duration_minutes)
    earliest = now + timedelta(hours=cfg.min_booking_notice_hours) if enforce_notice else now
    day_start, day_end = day_bounds(day)
    busy = list(
        Event.objects.filter(
            trainer=trainer, status=Event.Status.SCHEDULED, start__lt=day_end, end__gt=day_start
        ).values_list("start", "end")
    )
    busy += list(TimeOff.objects.filter(trainer=trainer, start__lt=day_end, end__gt=day_start).values_list("start", "end"))
    slots = []
    for window in trainer.availability.filter(weekday=day.weekday()):
        t = local_dt(day, window.start_time)
        window_end = local_dt(day, window.end_time)
        while t + length <= window_end:
            if t >= earliest and not any(s < t + length and e > t for s, e in busy):
                slots.append(t)
            t += step
    return sorted(set(slots))


# ---------------------------------------------------------------- booking


def book_session(client, trainer, start, duration_minutes, booked_by=None, now=None, staff_override=False):
    """Book a 1:1 session. staff_override lets admins/trainers book outside
    posted availability and inside the notice period — never on top of
    another booking or during time off."""
    now = now or timezone.now()
    if duration_minutes not in SESSION_CREDIT_FOR_DURATION:
        raise BookingError("Sessions are 30 or 60 minutes.")
    end = start + timedelta(minutes=duration_minutes)
    cfg = StudioSettings.load()

    with transaction.atomic():
        trainer = TrainerProfile.objects.select_for_update().get(pk=trainer.pk)
        client = ClientProfile.objects.select_for_update().get(pk=client.pk)
        check_client_can_book(client)
        if start <= now:
            raise BookingError("That time has already passed.")
        if not staff_override:
            if start < now + timedelta(hours=cfg.min_booking_notice_hours):
                raise BookingError(f"Sessions must be booked at least {cfg.min_booking_notice_hours} hours ahead.")
            if start > now + timedelta(days=cfg.booking_window_days):
                raise BookingError(f"You can book up to {cfg.booking_window_days} days ahead.")
            if not within_availability(trainer, start, end):
                raise BookingError(f"{trainer} isn't available at that time.")
        if on_time_off(trainer, start, end):
            raise BookingError(f"{trainer} is off at that time.")
        if trainer_conflicts(trainer, start, end).exists():
            raise BookingError(f"{trainer} is already booked at that time.")
        if client_conflicts(client, start, end).exists():
            raise BookingError(f"{client} already has a booking that overlaps this time.")

        event = Event.objects.create(
            kind=Event.Kind.SESSION,
            title=f"{duration_minutes}-min session",
            trainer=trainer,
            start=start,
            end=end,
            capacity=1,
            credit_type=SESSION_CREDIT_FOR_DURATION[duration_minutes],
        )
        booking = Booking.objects.create(event=event, client=client, booked_by=booked_by, created_at=now)
        consume_credit(client, booking, booked_by, now)  # raises -> whole transaction rolls back
    return booking


def book_event(client, event, booked_by=None, now=None, staff_override=False):
    """Book a seat in a class or small-group session."""
    now = now or timezone.now()
    cfg = StudioSettings.load()
    with transaction.atomic():
        event = Event.objects.select_for_update().get(pk=event.pk)
        client = ClientProfile.objects.select_for_update().get(pk=client.pk)
        check_client_can_book(client)
        _check_event_open(event, now, cfg, staff_override)
        if Booking.objects.filter(event=event, client=client, status__in=Booking.SEATED_STATUSES).exists():
            raise BookingError("Already booked.")
        if client_conflicts(client, event.start, event.end, exclude_event_id=event.pk).exists():
            raise BookingError(f"{client} already has a booking that overlaps this time.")
        if event.booked_count() >= event.capacity:
            raise EventFull("This class is full. You can join the waitlist.")
        booking = Booking.objects.filter(event=event, client=client, status=Booking.Status.WAITLISTED).first()
        if booking:  # booking directly from the waitlist when a spot is open
            booking.status, booking.waitlist_position = Booking.Status.BOOKED, None
            booking.save(update_fields=["status", "waitlist_position"])
        else:
            booking = Booking.objects.create(event=event, client=client, booked_by=booked_by, created_at=now)
        consume_credit(client, booking, booked_by, now)
    return booking


def join_waitlist(client, event, booked_by=None, now=None, staff_override=False):
    now = now or timezone.now()
    cfg = StudioSettings.load()
    with transaction.atomic():
        event = Event.objects.select_for_update().get(pk=event.pk)
        client = ClientProfile.objects.select_for_update().get(pk=client.pk)
        check_client_can_book(client)
        _check_event_open(event, now, cfg, staff_override)
        if event.kind == Event.Kind.SESSION:
            raise BookingError("1:1 sessions don't have waitlists.")
        if event.booked_count() < event.capacity:
            raise BookingError("There's an open spot — book it instead.")
        if Booking.objects.filter(
            event=event, client=client, status__in=Booking.SEATED_STATUSES + [Booking.Status.WAITLISTED]
        ).exists():
            raise BookingError("Already booked or on the waitlist.")
        if not can_pay_for(client, event.credit_type, event.start, now):
            raise BookingError(
                f"You need a {CreditType(event.credit_type).label.lower()} credit or membership to join the waitlist."
            )
        last = event.bookings.filter(status=Booking.Status.WAITLISTED).aggregate(m=Max("waitlist_position"))["m"] or 0
        return Booking.objects.create(
            event=event,
            client=client,
            status=Booking.Status.WAITLISTED,
            waitlist_position=last + 1,
            booked_by=booked_by,
            created_at=now,
        )


def _check_event_open(event, now, cfg, staff_override):
    if event.is_cancelled:
        raise BookingError("This class has been cancelled.")
    if event.start <= now:
        raise BookingError("This class has already started.")
    if not staff_override and event.start > now + timedelta(days=cfg.booking_window_days):
        raise BookingError(f"You can book up to {cfg.booking_window_days} days ahead.")


def promote_waitlist(event, now=None, by_user=None):
    """Fill open seats from the waitlist in order. Clients who can no longer
    book (no credits, payment flag, now double-booked) are skipped and removed.
    Must be called inside a transaction holding the event lock."""
    now = now or timezone.now()
    cfg = StudioSettings.load()
    promoted = []
    if event.is_cancelled or event.start - now < timedelta(hours=cfg.waitlist_cutoff_hours):
        return promoted
    while event.booked_count() < event.capacity:
        nxt = event.waitlist().first()
        if nxt is None:
            break
        client = ClientProfile.objects.select_for_update().get(pk=nxt.client_id)
        try:
            check_client_can_book(client)
            if client_conflicts(client, event.start, event.end, exclude_event_id=event.pk).exists():
                raise BookingError("double-booked")
            with transaction.atomic():  # savepoint: a failed deduction leaves nothing behind
                nxt.status, nxt.waitlist_position = Booking.Status.BOOKED, None
                nxt.save(update_fields=["status", "waitlist_position"])
                consume_credit(client, nxt, by_user, now)
        except BookingError as exc:
            nxt.refresh_from_db()
            nxt.status, nxt.waitlist_position = Booking.Status.REMOVED, None
            nxt.note = f"Skipped at waitlist promotion: {exc}"[:255]
            nxt.cancelled_at = now
            nxt.save(update_fields=["status", "waitlist_position", "note", "cancelled_at"])
            continue
        promoted.append(nxt)
        local = timezone.localtime(event.start)
        Notification.objects.create(
            user=client.user,
            subject=f"You're in: {event.title}",
            body=f"A spot opened up and you've been moved from the waitlist into {event.title} on {local:%a %b %-d at %-I:%M %p}.",
        )
    return promoted


# ---------------------------------------------------------------- cancel / attendance


class CancelOutcome:
    REFUNDED = "refunded"
    LATE = "late"
    WAIVED = "waived"
    LEFT_WAITLIST = "left_waitlist"


def is_late_cancel(event, now=None):
    now = now or timezone.now()
    cfg = StudioSettings.load()
    return now >= event.start - timedelta(hours=cfg.late_cancel_hours)


def cancel_booking(booking, by_user=None, now=None, waive=False):
    """Cancel a booking. Inside the late-cancel window the credit is kept
    (status late_cancel) unless staff waive it. Frees the seat or slot and
    promotes the waitlist."""
    now = now or timezone.now()
    with transaction.atomic():
        booking = Booking.objects.select_related("event").get(pk=booking.pk)
        event = Event.objects.select_for_update().get(pk=booking.event_id)
        booking = Booking.objects.select_for_update().get(pk=booking.pk)

        if booking.status == Booking.Status.WAITLISTED:
            booking.status, booking.waitlist_position, booking.cancelled_at = Booking.Status.REMOVED, None, now
            booking.save(update_fields=["status", "waitlist_position", "cancelled_at"])
            return CancelOutcome.LEFT_WAITLIST
        if booking.status != Booking.Status.BOOKED:
            raise BookingError("Only upcoming bookings can be cancelled.")
        if event.start <= now:
            raise BookingError("This has already started. Staff can mark it as a no-show instead.")

        late = is_late_cancel(event, now)
        booking.cancelled_at = now
        if late and not waive:
            booking.status = Booking.Status.LATE_CANCEL
            outcome = CancelOutcome.LATE
        else:
            booking.status = Booking.Status.CANCELLED
            reason = CreditTransaction.Reason.WAIVED if late else CreditTransaction.Reason.CANCEL_REFUND
            refund_credit(booking, reason, by_user, now)
            outcome = CancelOutcome.WAIVED if late else CancelOutcome.REFUNDED
        booking.save(update_fields=["status", "cancelled_at"])

        if event.kind == Event.Kind.SESSION:
            event.status = Event.Status.CANCELLED  # frees the trainer's slot
            event.save(update_fields=["status"])
        else:
            promote_waitlist(event, now, by_user)
    return outcome


def mark_attendance(booking, status, by_user=None, now=None):
    """Mark attended / no-show after the start. A no-show keeps the credit used."""
    now = now or timezone.now()
    if status not in (Booking.Status.ATTENDED, Booking.Status.NO_SHOW, Booking.Status.BOOKED):
        raise BookingError("Invalid attendance status.")
    with transaction.atomic():
        booking = Booking.objects.select_for_update().select_related("event").get(pk=booking.pk)
        if booking.status not in Booking.SEATED_STATUSES:
            raise BookingError("Only booked clients can be marked.")
        if booking.event.start > now:
            raise BookingError("Attendance can be marked once the session starts.")
        booking.status = status
        booking.save(update_fields=["status"])
    return booking


def cancel_event(event, by_user=None, now=None):
    """Studio cancels a class/session: everyone is refunded, waitlist cleared."""
    now = now or timezone.now()
    with transaction.atomic():
        event = Event.objects.select_for_update().get(pk=event.pk)
        if event.is_cancelled:
            return event
        for booking in event.bookings.select_for_update().filter(status=Booking.Status.BOOKED):
            booking.status, booking.cancelled_at = Booking.Status.CANCELLED, now
            booking.save(update_fields=["status", "cancelled_at"])
            refund_credit(booking, CreditTransaction.Reason.EVENT_CANCELLED, by_user, now)
        event.bookings.filter(status=Booking.Status.WAITLISTED).update(
            status=Booking.Status.REMOVED, waitlist_position=None, cancelled_at=now
        )
        event.status = Event.Status.CANCELLED
        event.save(update_fields=["status"])
    return event


# ---------------------------------------------------------------- creating events


def create_group_session(trainer, start, duration_minutes, capacity, title="Small-group session"):
    end = start + timedelta(minutes=duration_minutes)
    with transaction.atomic():
        trainer = TrainerProfile.objects.select_for_update().get(pk=trainer.pk)
        if on_time_off(trainer, start, end):
            raise BookingError(f"{trainer} is off at that time.")
        if trainer_conflicts(trainer, start, end).exists():
            raise BookingError(f"{trainer} is already booked at that time.")
        return Event.objects.create(
            kind=Event.Kind.GROUP,
            title=title,
            trainer=trainer,
            start=start,
            end=end,
            capacity=capacity,
            credit_type=CreditType.GROUP,
        )


def generate_classes(from_date, weeks=3):
    """Create dated class Events from active templates. Safe to re-run.
    Returns (created, skipped_conflicts)."""
    created, skipped = [], []
    days = [from_date + timedelta(days=i) for i in range(weeks * 7)]
    for tpl in ClassTemplate.objects.filter(active=True).select_related("trainer__user"):
        for day in days:
            if day.weekday() != tpl.weekday:
                continue
            start = local_dt(day, tpl.start_time)
            end = start + timedelta(minutes=tpl.duration_minutes)
            if Event.objects.filter(template=tpl, start=start).exists():
                continue
            with transaction.atomic():
                trainer = TrainerProfile.objects.select_for_update().get(pk=tpl.trainer_id)
                if trainer_conflicts(trainer, start, end).exists() or on_time_off(trainer, start, end):
                    skipped.append((tpl, start))
                    continue
                created.append(
                    Event.objects.create(
                        kind=Event.Kind.CLASS,
                        title=tpl.title,
                        trainer=trainer,
                        start=start,
                        end=end,
                        capacity=tpl.capacity,
                        credit_type=CreditType.CLASS,
                        template=tpl,
                    )
                )
    return created, skipped
