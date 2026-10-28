"""Every screen renders for the roles allowed to see it, and role boundaries hold."""
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from studio.models import Booking, ClientProfile, Event, TrainerProfile, User


@pytest.fixture
def seeded(db):
    call_command("seed", verbosity=0)


def login(client, username):
    client.force_login(User.objects.get(username=username))
    return client


STAFF_PAGES = [
    ("schedule", {}),
    ("schedule", {"view": "week"}),
    ("clients", {}),
    ("clients", {"filter": "flagged"}),
    ("clients", {"filter": "expired"}),
    ("staff_book", {}),
    ("group_new", {}),
    ("availability", {}),
]
ADMIN_PAGES = [("office_classes", {}), ("office_products", {}), ("office_settings", {}), ("office_report", {})]
CLIENT_PAGES = [("client_dashboard", {}), ("client_book", {}), ("client_classes", {}), ("client_buy", {}), ("client_history", {})]


def test_admin_pages_render(seeded, client):
    login(client, "owner@studio.test")
    for name, params in STAFF_PAGES + ADMIN_PAGES:
        resp = client.get(reverse(name), params)
        assert resp.status_code == 200, name
    resp = client.get(reverse("office_report"), {"format": "csv"})
    assert resp["Content-Type"] == "text/csv" and b"Alex Kim" in resp.content
    for ev in Event.objects.all()[:20]:
        assert client.get(reverse("event_detail", args=[ev.pk])).status_code == 200
    for c in ClientProfile.objects.all()[:10]:
        assert client.get(reverse("client_detail", args=[c.pk])).status_code == 200
    c = ClientProfile.objects.first()
    assert client.get(reverse("staff_book"), {"client": c.pk, "trainer": TrainerProfile.objects.first().pk}).status_code == 200


def test_admin_day_view_shows_each_trainer_only_their_events(seeded, client):
    login(client, "owner@studio.test")
    ev = Event.objects.filter(kind="class", start__gte=timezone.now()).first()
    resp = client.get(reverse("schedule"), {"date": timezone.localtime(ev.start).date().isoformat()})
    same_title_same_time = sum(
        1 for col in resp.context["columns"] for b in col["blocks"] if b.get("url") == reverse("event_detail", args=[ev.pk])
    )
    assert same_title_same_time == 1


def test_trainer_sees_only_own_schedule_and_clients(seeded, client):
    login(client, "alex@studio.test")
    alex = TrainerProfile.objects.get(user__username="alex@studio.test")
    for name, params in STAFF_PAGES:
        assert client.get(reverse(name), params).status_code == 200, name
    for name, params in ADMIN_PAGES:
        assert client.get(reverse(name), params).status_code == 403, name
    resp = client.get(reverse("schedule"), {"view": "week"})
    assert [c["label"] for c in resp.context["columns"]][0] == "Mon"  # one trainer: day columns

    others_event = Event.objects.exclude(trainer=alex).first()
    assert client.get(reverse("event_detail", args=[others_event.pk])).status_code == 403
    stranger = ClientProfile.objects.exclude(primary_trainer=alex).exclude(bookings__event__trainer=alex).first()
    assert client.get(reverse("client_detail", args=[stranger.pk])).status_code == 404
    listed = {r["client"].pk for r in client.get(reverse("clients")).context["rows"]}
    assert stranger.pk not in listed and listed
    # trainers can't adjust credits or flag clients
    mine = ClientProfile.objects.filter(primary_trainer=alex).first()
    assert client.post(reverse("client_flag", args=[mine.pk])).status_code == 403


def test_client_sees_only_own_data(seeded, client):
    login(client, "emily.carter@example.com")
    for name, params in CLIENT_PAGES:
        assert client.get(reverse(name), params).status_code == 200, name
    for name, params in STAFF_PAGES + ADMIN_PAGES:
        assert client.get(reverse(name), params).status_code == 403, name
    someone_else = Booking.objects.exclude(client__user__username="emily.carter@example.com").filter(status="booked").first()
    assert client.get(reverse("booking_cancel", args=[someone_else.pk])).status_code == 403
    assert client.post(reverse("booking_cancel", args=[someone_else.pk])).status_code == 403
    someone_else.refresh_from_db()
    assert someone_else.status == "booked"


def test_client_books_and_cancels_through_the_ui(seeded, client):
    login(client, "emily.carter@example.com")
    emily = ClientProfile.objects.get(user__username="emily.carter@example.com")
    alex = emily.primary_trainer
    day = timezone.localdate() + timedelta(days=4)
    while not alex.availability.filter(weekday=day.weekday()).exists():
        day += timedelta(days=1)
    resp = client.get(reverse("client_book"), {"duration": 60, "trainer": alex.pk, "date": day.isoformat()})
    slot = resp.context["slots"][0]
    assert client.get(reverse("client_book_confirm"), {"trainer": alex.pk, "duration": 60, "start": slot.isoformat()}).status_code == 200
    before = emily.packages.get(credit_type="pt60").credits_remaining
    resp = client.post(reverse("client_book_confirm"), {"trainer": alex.pk, "duration": 60, "start": slot.isoformat()})
    assert resp.status_code == 302
    booking = Booking.objects.get(client=emily, event__start=slot)
    assert emily.packages.get(credit_type="pt60").credits_remaining == before - 1
    client.post(reverse("booking_cancel", args=[booking.pk]))
    booking.refresh_from_db()
    assert booking.status == "cancelled"
    assert emily.packages.get(credit_type="pt60").credits_remaining == before


def test_flagged_client_sees_banner_and_cannot_book(seeded, client):
    login(client, "marcus.bell@example.com")
    resp = client.get(reverse("client_dashboard"))
    assert b"Payment problem" in resp.content
    ev = Event.objects.filter(kind="class", start__gte=timezone.now() + timedelta(days=1)).first()
    client.post(reverse("client_class_join", args=[ev.pk]))
    assert not Booking.objects.filter(event=ev, client__user__username="marcus.bell@example.com", status="booked").exists()


def test_seed_has_required_scenarios(seeded):
    from studio.services.credits import balances

    assert TrainerProfile.objects.count() == 7
    assert ClientProfile.objects.count() == 40
    dana = ClientProfile.objects.get(user__username="dana.whitfield@example.com")
    assert balances(dana)["pt60"]["expired"] == 3 and balances(dana)["pt60"]["credits"] == 0
    assert ClientProfile.objects.get(user__username="marcus.bell@example.com").is_payment_flagged
    assert Booking.objects.filter(status="waitlisted").count() == 2


def test_demo_mode_login_page(seeded, client, settings):
    settings.DEMO_MODE = False
    resp = client.get(reverse("login"))
    assert b"Log in as" not in resp.content and b"demo1234" not in resp.content
    settings.DEMO_MODE = True
    resp = client.get(reverse("login"))
    assert resp.content.count(b"Log in as") == 6
    # every one-click account really exists and logs in
    for _, _, email, _ in resp.context["demo_accounts"]:
        c = client.__class__()
        r = c.post(reverse("login"), {"username": email, "password": resp.context["demo_password"]})
        assert r.status_code == 302, email
