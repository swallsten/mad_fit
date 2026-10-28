"""Client booking page (mobile first)."""
from datetime import datetime, timedelta

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from studio.models import (
    SESSION_CREDIT_FOR_DURATION,
    Booking,
    ClientPackage,
    CreditType,
    Event,
    Membership,
    Product,
    StudioSettings,
    TrainerProfile,
)
from studio.permissions import client_required
from studio.services import payments
from studio.services.booking import (
    BookingError,
    EventFull,
    available_slots,
    book_event,
    book_session,
    day_bounds,
    join_waitlist,
)
from studio.services.credits import active_unlimited_membership, balances, usable_packages


def _me(request):
    return request.user.client


def upcoming_bookings(client):
    return (
        Booking.objects.filter(
            client=client,
            status__in=[Booking.Status.BOOKED, Booking.Status.WAITLISTED],
            event__start__gte=timezone.now(),
            event__status=Event.Status.SCHEDULED,
        )
        .select_related("event__trainer__user", "package", "membership__product")
        .order_by("event__start")
    )


@client_required
def dashboard(request):
    client = _me(request)
    bal = balances(client)
    shown = [b for b in bal.values() if b["credits"] or b["unlimited"] or b["expired"]]
    return render(
        request,
        "studio/client/dashboard.html",
        {
            "client": client,
            "balances": shown,
            "memberships": client.memberships.exclude(status=Membership.Status.CANCELLED).select_related("product"),
            "upcoming": upcoming_bookings(client),
        },
    )


def slot_picker_context(request, trainers, now=None):
    """Shared by the client and staff booking pages: pick length, trainer, day, slot."""
    now = now or timezone.now()
    cfg = StudioSettings.load()
    try:
        duration = int(request.GET.get("duration", 60))
    except ValueError:
        duration = 60
    if duration not in SESSION_CREDIT_FOR_DURATION:
        duration = 60
    trainer = None
    if request.GET.get("trainer"):
        trainer = trainers.filter(pk=request.GET["trainer"]).first()
    today = timezone.localdate()
    days = [today + timedelta(days=i) for i in range(min(cfg.booking_window_days, 21))]
    try:
        day = datetime.strptime(request.GET.get("date", ""), "%Y-%m-%d").date()
    except ValueError:
        day = today
    day_slots = []
    if trainer:
        day_slots = available_slots(trainer, day, duration, now)
        if not day_slots and not request.GET.get("date"):
            # no day picked yet: jump to the first day with openings
            for d in days:
                day_slots = available_slots(trainer, d, duration, now)
                if day_slots:
                    day = d
                    break
    return {
        "duration": duration,
        "durations": [(30, CreditType.PT30.label), (60, CreditType.PT60.label)],
        "trainers": trainers,
        "trainer": trainer,
        "days": days,
        "day": day,
        "slots": day_slots,
    }


@client_required
def book(request):
    client = _me(request)
    ctx = slot_picker_context(request, TrainerProfile.objects.filter(active=True).select_related("user"))
    ctx["balance"] = balances(client)[SESSION_CREDIT_FOR_DURATION[ctx["duration"]].value]
    ctx["client"] = client
    ctx["confirm_url"] = reverse("client_book_confirm")
    return render(request, "studio/client/book.html", ctx)


def _parse_start(value):
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt


@client_required
def book_confirm(request):
    client = _me(request)
    src = request.POST if request.method == "POST" else request.GET
    trainer = get_object_or_404(TrainerProfile, pk=src.get("trainer"), active=True)
    start = _parse_start(src.get("start"))
    try:
        duration = int(src.get("duration", 60))
    except ValueError:
        duration = 60
    if not start or duration not in SESSION_CREDIT_FOR_DURATION:
        messages.error(request, "Pick a time first.")
        return redirect("client_book")
    if request.method == "POST":
        try:
            book_session(client, trainer, start, duration, booked_by=request.user)
        except BookingError as exc:
            messages.error(request, str(exc))
            return redirect(f"{reverse('client_book')}?duration={duration}&trainer={trainer.pk}&date={timezone.localtime(start):%Y-%m-%d}")
        messages.success(request, f"Booked: {duration}-min session with {trainer} on {timezone.localtime(start):%a %b %-d at %-I:%M %p}.")
        return redirect("client_dashboard")
    credit_type = SESSION_CREDIT_FOR_DURATION[duration]
    membership = active_unlimited_membership(client, credit_type)
    package = None if membership else usable_packages(client, credit_type, start).first()
    return render(
        request,
        "studio/client/book_confirm.html",
        {
            "client": client,
            "trainer": trainer,
            "start": start,
            "duration": duration,
            "membership": membership,
            "package": package,
            "late_hours": StudioSettings.load().late_cancel_hours,
        },
    )


@client_required
def classes(request):
    client = _me(request)
    try:
        offset = max(0, min(int(request.GET.get("week", 0)), 3))
    except ValueError:
        offset = 0
    today = timezone.localdate()
    start_day = today + timedelta(days=7 * offset)
    start, _ = day_bounds(start_day)
    _, end = day_bounds(start_day + timedelta(days=6))
    events = (
        Event.objects.filter(
            kind__in=[Event.Kind.CLASS, Event.Kind.GROUP],
            status=Event.Status.SCHEDULED,
            start__gte=max(start, timezone.now()),
            start__lt=end,
        )
        .select_related("trainer__user")
        .order_by("start")
    )
    mine = {
        b.event_id: b
        for b in Booking.objects.filter(
            client=client, event__in=events, status__in=[Booking.Status.BOOKED, Booking.Status.WAITLISTED]
        )
    }
    days = {}
    for ev in events:
        ev.my_booking = mine.get(ev.pk)
        ev.n_booked = ev.booked_count()
        ev.is_full = ev.n_booked >= ev.capacity
        if ev.my_booking and ev.my_booking.status == Booking.Status.WAITLISTED:
            ev.my_position = list(ev.waitlist().values_list("pk", flat=True)).index(ev.my_booking.pk) + 1
        days.setdefault(timezone.localtime(ev.start).date(), []).append(ev)
    return render(
        request,
        "studio/client/classes.html",
        {"client": client, "days": sorted(days.items()), "week": offset, "balances": balances(client)},
    )


@client_required
@require_POST
def class_join(request, event_id):
    client = _me(request)
    event = get_object_or_404(Event, pk=event_id, kind__in=[Event.Kind.CLASS, Event.Kind.GROUP])
    try:
        if request.POST.get("waitlist"):
            booking = join_waitlist(client, event, booked_by=request.user)
            messages.success(request, f"You're #{booking.waitlist_position} on the waitlist for {event.title}. We'll book you automatically if a spot opens.")
        else:
            book_event(client, event, booked_by=request.user)
            messages.success(request, f"Booked: {event.title} on {timezone.localtime(event.start):%a %b %-d at %-I:%M %p}.")
    except EventFull as exc:
        messages.warning(request, str(exc))
    except BookingError as exc:
        messages.error(request, str(exc))
    return redirect(request.POST.get("next") or "client_classes")


@client_required
def buy(request):
    client = _me(request)
    used_once = set(
        ClientPackage.objects.filter(client=client, product__one_per_client=True).values_list("product_id", flat=True)
    )
    active_memberships = set(
        client.memberships.filter(status__in=[Membership.Status.ACTIVE, Membership.Status.PAST_DUE]).values_list(
            "product_id", flat=True
        )
    )
    groups = {}
    for p in Product.objects.filter(active=True):
        if p.pk in used_once:
            continue
        p.already_member = p.pk in active_memberships
        groups.setdefault(p.get_kind_display(), []).append(p)
    order = [k.label for k in [Product.Kind.INTRO, Product.Kind.PACKAGE, Product.Kind.MEMBERSHIP, Product.Kind.DROP_IN]]
    return render(
        request,
        "studio/client/buy.html",
        {
            "client": client,
            "groups": [(k, groups[k]) for k in order if k in groups],
            "stripe_enabled": payments.stripe_enabled(),
        },
    )


@client_required
@require_POST
def buy_product(request, product_id):
    client = _me(request)
    product = get_object_or_404(Product, pk=product_id, active=True)
    try:
        url = payments.create_checkout_session(client, product)
    except (BookingError, payments.StripeNotConfigured) as exc:
        messages.error(request, str(exc))
        return redirect("client_buy")
    except Exception as exc:  # Stripe API errors
        messages.error(request, f"Couldn't start checkout: {exc}")
        return redirect("client_buy")
    return redirect(url)


@client_required
def checkout_success(request):
    session_id = request.GET.get("session_id", "")
    if session_id and payments.stripe_enabled():
        try:
            session, _ = payments.sync_checkout_session(session_id)
            if (session.get("metadata") or {}).get("client_id") != str(_me(request).pk):
                messages.error(request, "That checkout belongs to another account.")
                return redirect("client_dashboard")
            messages.success(request, "Payment received — thank you! Your balance is updated.")
        except Exception as exc:
            messages.warning(request, f"Payment is processing; your balance will update shortly. ({exc})")
    return redirect("client_dashboard")


@client_required
@require_POST
def billing_portal(request):
    try:
        url = payments.customer_portal_url(_me(request), request.build_absolute_uri(reverse("client_dashboard")))
    except payments.StripeNotConfigured as exc:
        messages.error(request, str(exc))
        return redirect("client_dashboard")
    except Exception as exc:
        messages.error(request, f"Couldn't open the billing portal: {exc}")
        return redirect("client_dashboard")
    return redirect(url)


@client_required
def history(request):
    client = _me(request)
    return render(
        request,
        "studio/client/history.html",
        {
            "client": client,
            "past": Booking.objects.filter(client=client)
            .exclude(status__in=[Booking.Status.BOOKED, Booking.Status.WAITLISTED], event__start__gte=timezone.now())
            .select_related("event__trainer__user", "package", "membership__product")
            .order_by("-event__start")[:100],
            "packages": client.packages.order_by("-purchased_at"),
            "ledger": client.credit_transactions.select_related("package", "booking__event")[:100],
            "now": timezone.now(),
        },
    )
