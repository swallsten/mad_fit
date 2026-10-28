"""Screens shared by admin and trainers. Trainers are scoped to their own
calendar and clients; admin sees all trainers."""
from datetime import datetime, timedelta

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from studio import calendar as cal
from studio.forms import AdjustCreditsForm, AvailabilityForm, GrantProductForm, GroupSessionForm, TimeOffForm
from studio.models import Availability, Booking, ClientProfile, Event, Membership, TimeOff, TrainerProfile
from studio.permissions import can_manage_booking, can_manage_event, staff_required, visible_clients
from studio.services.booking import (
    BookingError,
    CancelOutcome,
    available_slots,
    EventFull,
    book_event,
    book_session,
    cancel_booking,
    cancel_event,
    create_group_session,
    day_bounds,
    is_late_cancel,
    join_waitlist,
    mark_attendance,
)
from studio.services.credits import balances

from .client import _parse_start, slot_picker_context


def _parse_day(value):
    try:
        return datetime.strptime(value or "", "%Y-%m-%d").date()
    except ValueError:
        return timezone.localdate()


def _staff_trainers(user):
    qs = TrainerProfile.objects.filter(active=True).select_related("user").prefetch_related("availability", "time_off")
    return qs if user.is_studio_admin else qs.filter(pk=user.trainer.pk)


@staff_required
def schedule(request):
    """Admin: all trainers side by side (day) or trainer x day table (week).
    Trainer: their own week as a day-column grid, or one day."""
    user = request.user
    day = _parse_day(request.GET.get("date"))
    view = request.GET.get("view") or ("day" if user.is_studio_admin else "week")
    trainers = _staff_trainers(user)
    if user.is_studio_admin and request.GET.get("trainer"):
        trainers = trainers.filter(pk=request.GET["trainer"])
    ctx = {"view": view, "day": day, "hours": cal.hour_labels(), "grid_height": cal.grid_height()}

    if view == "week":
        monday = cal.week_start(day)
        days = [monday + timedelta(days=i) for i in range(7)]
        start, _ = day_bounds(days[0])
        _, end = day_bounds(days[-1])
        events = list(cal.events_qs(start, end, trainers))
        if trainers.count() == 1:
            t = trainers.first()
            ctx["columns"] = [
                cal.column(f"{d:%a}", d, t, events, sublabel=f"{d:%b %-d}", label_url=f"?view=day&date={d:%Y-%m-%d}&trainer={t.pk}")
                for d in days
            ]
            ctx["single_trainer"] = t
        else:
            rows = []
            for t in trainers:
                cells = []
                for d in days:
                    ds, de = day_bounds(d)
                    cells.append(
                        {
                            "day": d,
                            "events": [e for e in events if e.trainer_id == t.pk and e.start < de and e.end > ds],
                            "off": any(o.start < de and o.end > ds for o in t.time_off.all()),
                        }
                    )
                rows.append({"trainer": t, "cells": cells})
            ctx["rows"] = rows
        ctx.update({"days": days, "prev": monday - timedelta(days=7), "next": monday + timedelta(days=7)})
    else:
        start, end = day_bounds(day)
        events = list(cal.events_qs(start, end, trainers))
        ctx["columns"] = [
            cal.column(t.name, day, t, events, label_url=f"?view=week&date={day:%Y-%m-%d}&trainer={t.pk}") for t in trainers
        ]
        ctx.update({"prev": day - timedelta(days=1), "next": day + timedelta(days=1)})
    ctx["trainer_param"] = request.GET.get("trainer", "")
    ctx["today"] = timezone.localdate()
    return render(request, "studio/staff/schedule.html", ctx)


@staff_required
def event_detail(request, event_id):
    event = get_object_or_404(Event.objects.select_related("trainer__user", "template"), pk=event_id)
    if not can_manage_event(request.user, event):
        raise PermissionDenied
    bookings = event.bookings.select_related("client__user", "package", "membership__product").order_by(
        "waitlist_position", "created_at"
    )
    return render(
        request,
        "studio/staff/event_detail.html",
        {
            "event": event,
            "seated": [b for b in bookings if b.status in Booking.SEATED_STATUSES],
            "waitlist": [b for b in bookings if b.status == Booking.Status.WAITLISTED],
            "other": [b for b in bookings if b.status not in Booking.SEATED_STATUSES + [Booking.Status.WAITLISTED]],
            "clients": visible_clients(request.user).filter(active=True) if event.kind != Event.Kind.SESSION else [],
            "started": event.start <= timezone.now(),
            "late": is_late_cancel(event),
        },
    )


@staff_required
@require_POST
def event_add_client(request, event_id):
    event = get_object_or_404(Event, pk=event_id)
    if not can_manage_event(request.user, event):
        raise PermissionDenied
    client = get_object_or_404(visible_clients(request.user), pk=request.POST.get("client"))
    try:
        if request.POST.get("waitlist"):
            join_waitlist(client, event, booked_by=request.user, staff_override=True)
            messages.success(request, f"{client} added to the waitlist.")
        else:
            book_event(client, event, booked_by=request.user, staff_override=True)
            messages.success(request, f"{client} booked.")
    except EventFull as exc:
        messages.warning(request, f"{exc} Use 'Add to waitlist'.")
    except BookingError as exc:
        messages.error(request, str(exc))
    return redirect("event_detail", event.pk)


@staff_required
@require_POST
def event_cancel(request, event_id):
    event = get_object_or_404(Event, pk=event_id)
    if not can_manage_event(request.user, event):
        raise PermissionDenied
    cancel_event(event, by_user=request.user)
    messages.success(request, f"{event.title} cancelled. All booked clients were refunded.")
    return redirect(f"{reverse('schedule')}?date={timezone.localtime(event.start):%Y-%m-%d}")


@staff_required
@require_POST
def booking_attendance(request, booking_id):
    booking = get_object_or_404(Booking.objects.select_related("event"), pk=booking_id)
    if not can_manage_booking(request.user, booking):
        raise PermissionDenied
    try:
        mark_attendance(booking, request.POST.get("status"), by_user=request.user)
    except BookingError as exc:
        messages.error(request, str(exc))
    return redirect("event_detail", booking.event_id)


def cancel_view(request, booking_id):
    """Shared by clients and staff. GET shows the policy, POST cancels."""
    if not request.user.is_authenticated:
        return redirect("login")
    booking = get_object_or_404(Booking.objects.select_related("event__trainer__user", "client__user"), pk=booking_id)
    if not can_manage_booking(request.user, booking):
        raise PermissionDenied
    staff = not request.user.is_client
    late = is_late_cancel(booking.event)
    back = reverse("event_detail", args=[booking.event_id]) if staff else reverse("client_dashboard")
    if request.method == "POST":
        try:
            outcome = cancel_booking(booking, by_user=request.user, waive=staff and bool(request.POST.get("waive")))
        except BookingError as exc:
            messages.error(request, str(exc))
            return redirect(back)
        text = {
            CancelOutcome.REFUNDED: "Cancelled. The credit is back in the balance.",
            CancelOutcome.LATE: "Cancelled late — per the cancellation policy the credit was used.",
            CancelOutcome.WAIVED: "Cancelled. Late-cancel fee waived and the credit refunded.",
            CancelOutcome.LEFT_WAITLIST: "Removed from the waitlist.",
        }[outcome]
        (messages.warning if outcome == CancelOutcome.LATE else messages.success)(request, text)
        return redirect(back)
    return render(request, "studio/cancel_confirm.html", {"booking": booking, "late": late, "staff": staff, "back": back})


@staff_required
def clients(request):
    qs = visible_clients(request.user).filter(active=True)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(user__first_name__icontains=q) | Q(user__last_name__icontains=q) | Q(user__email__icontains=q))
    flt = request.GET.get("filter", "")
    if flt == "flagged":
        qs = qs.filter(payment_status=ClientProfile.PaymentStatus.FAILED)
    now = timezone.now()
    rows = []
    for c in qs:
        bal = balances(c, now)
        row = {"client": c, "bal": bal, "upcoming": c.bookings.filter(status="booked", event__start__gte=now).count()}
        row["memberships"] = [m for m in c.memberships.all() if m.status != Membership.Status.CANCELLED]
        row["has_expired"] = any(b["expired"] for b in bal.values())
        row["no_credits"] = not any(b["credits"] or b["unlimited"] for b in bal.values())
        rows.append(row)
    if flt == "expired":
        rows = [r for r in rows if r["has_expired"]]
    if flt == "empty":
        rows = [r for r in rows if r["no_credits"]]
    return render(request, "studio/staff/clients.html", {"rows": rows, "q": q, "filter": flt})


@staff_required
def client_detail(request, client_id):
    client = get_object_or_404(visible_clients(request.user), pk=client_id)
    now = timezone.now()
    bookings = client.bookings.select_related("event__trainer__user", "package", "membership__product")
    if request.user.is_trainer:
        bookings = bookings.filter(event__trainer=request.user.trainer)
    return render(
        request,
        "studio/staff/client_detail.html",
        {
            "client": client,
            "balances": balances(client, now).values(),
            "upcoming": bookings.filter(status__in=["booked", "waitlisted"], event__start__gte=now).order_by("event__start"),
            "past": bookings.exclude(status__in=["booked", "waitlisted"], event__start__gte=now).order_by("-event__start")[:50],
            "packages": client.packages.order_by("-purchased_at"),
            "memberships": client.memberships.select_related("product"),
            "ledger": client.credit_transactions.select_related("package", "booking__event", "created_by")[:50],
            "adjust_form": AdjustCreditsForm(client=client),
            "grant_form": GrantProductForm(),
            "now": now,
        },
    )


@staff_required
def staff_book(request):
    """Book a 1:1 for a client. Staff may book outside posted availability."""
    clients_qs = visible_clients(request.user).filter(active=True)
    client = clients_qs.filter(pk=request.GET.get("client") or request.POST.get("client")).first()
    trainers = _staff_trainers(request.user)
    if request.method == "POST":
        trainer = get_object_or_404(trainers, pk=request.POST.get("trainer"))
        start = _parse_start(request.POST.get("start"))
        try:
            duration = int(request.POST.get("duration", 60))
        except ValueError:
            duration = 60
        if not client or not start:
            messages.error(request, "Choose a client and a time.")
            return redirect("staff_book")
        try:
            booking = book_session(client, trainer, start, duration, booked_by=request.user, staff_override=True)
        except BookingError as exc:
            messages.error(request, str(exc))
            return redirect(f"{reverse('staff_book')}?client={client.pk}&trainer={trainer.pk}&duration={duration}&date={timezone.localtime(start):%Y-%m-%d}")
        messages.success(request, f"Booked {client} with {trainer} on {timezone.localtime(start):%a %b %-d at %-I:%M %p}.")
        return redirect("event_detail", booking.event_id)
    ctx = slot_picker_context(request, trainers)
    if request.user.is_trainer and not ctx["trainer"]:
        ctx["trainer"] = request.user.trainer
        ctx["slots"] = available_slots(ctx["trainer"], ctx["day"], ctx["duration"])
    ctx.update({"client": client, "clients": clients_qs})
    if client:
        ctx["balances"] = balances(client)
    return render(request, "studio/staff/book.html", ctx)


@staff_required
def group_new(request):
    trainers = _staff_trainers(request.user)
    form = GroupSessionForm(request.POST or None, initial={"trainer": None if request.user.is_studio_admin else request.user.trainer})
    form.fields["trainer"].queryset = trainers
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            ev = create_group_session(d["trainer"], d["start"], d["duration_minutes"], d["capacity"], d["title"])
        except BookingError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Small-group session created. Add clients below or let them book from the class schedule.")
            return redirect("event_detail", ev.pk)
    return render(request, "studio/staff/form_page.html", {"form": form, "title": "New small-group session", "submit": "Create"})


@staff_required
def availability(request):
    user = request.user
    if user.is_studio_admin:
        trainer = get_object_or_404(TrainerProfile, pk=request.GET.get("trainer") or TrainerProfile.objects.first().pk)
    else:
        trainer = user.trainer
    avail_form = AvailabilityForm(prefix="a")
    off_form = TimeOffForm(prefix="o")
    if request.method == "POST":
        if "add_availability" in request.POST:
            avail_form = AvailabilityForm(request.POST, prefix="a")
            if avail_form.is_valid():
                obj = avail_form.save(commit=False)
                obj.trainer = trainer
                obj.save()
                messages.success(request, "Availability added.")
                return redirect(f"{reverse('availability')}?trainer={trainer.pk}")
        elif "add_timeoff" in request.POST:
            off_form = TimeOffForm(request.POST, prefix="o")
            if off_form.is_valid():
                obj = off_form.save(commit=False)
                obj.trainer = trainer
                clashes = Event.objects.filter(trainer=trainer, status="scheduled", start__lt=obj.end, end__gt=obj.start)
                obj.save()
                if clashes.exists():
                    messages.warning(
                        request,
                        f"Time off saved, but {clashes.count()} booked session(s)/class(es) fall in it — reschedule or cancel them.",
                    )
                else:
                    messages.success(request, "Time off blocked.")
                return redirect(f"{reverse('availability')}?trainer={trainer.pk}")
    return render(
        request,
        "studio/staff/availability.html",
        {
            "trainer": trainer,
            "trainers": TrainerProfile.objects.select_related("user") if user.is_studio_admin else [],
            "windows": trainer.availability.all(),
            "time_off": trainer.time_off.filter(end__gte=timezone.now()),
            "avail_form": avail_form,
            "off_form": off_form,
        },
    )


@staff_required
@require_POST
def availability_delete(request, pk):
    obj = get_object_or_404(Availability, pk=pk)
    if not (request.user.is_studio_admin or obj.trainer_id == request.user.trainer.pk):
        raise PermissionDenied
    obj.delete()
    messages.success(request, "Availability removed. Existing bookings are not affected.")
    return redirect(f"{reverse('availability')}?trainer={obj.trainer_id}")


@staff_required
@require_POST
def timeoff_delete(request, pk):
    obj = get_object_or_404(TimeOff, pk=pk)
    if not (request.user.is_studio_admin or obj.trainer_id == request.user.trainer.pk):
        raise PermissionDenied
    obj.delete()
    messages.success(request, "Time off removed.")
    return redirect(f"{reverse('availability')}?trainer={obj.trainer_id}")
