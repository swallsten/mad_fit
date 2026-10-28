from datetime import date, time, timedelta

import pytest
from django.utils import timezone

from studio.models import Availability, ClientProfile, CreditType, Event, Product, StudioSettings, TrainerProfile, User
from studio.services.booking import local_dt
from studio.services.credits import grant_package

# A fixed "now": Monday 2026-11-02 09:00 studio time. Tests pass `now=` to
# services so results don't depend on when the suite runs.
MONDAY = date(2026, 11, 2)


@pytest.fixture
def now():
    return local_dt(MONDAY, time(9, 0))


@pytest.fixture(autouse=True)
def studio_settings(db):
    cfg = StudioSettings.load()
    cfg.late_cancel_hours = 24
    cfg.waitlist_cutoff_hours = 2
    cfg.min_booking_notice_hours = 2
    cfg.booking_window_days = 21
    cfg.save()
    return cfg


@pytest.fixture
def make_trainer(db):
    counter = iter(range(1000))

    def make(name="Taylor", available=True):
        n = next(counter)
        user = User.objects.create_user(f"trainer{n}@studio.test", password="x", first_name=name, role=User.Role.TRAINER)
        trainer = TrainerProfile.objects.create(user=user)
        if available:
            for wd in range(7):
                Availability.objects.create(trainer=trainer, weekday=wd, start_time=time(6), end_time=time(20))
        return trainer

    return make


@pytest.fixture
def make_client(db):
    counter = iter(range(1000))

    def make(name="Casey"):
        n = next(counter)
        user = User.objects.create_user(f"client{n}@example.com", password="x", first_name=name, last_name=str(n), role=User.Role.CLIENT)
        return ClientProfile.objects.create(user=user)

    return make


@pytest.fixture
def pack60(db):
    return Product.objects.create(
        name="10 x 60-min", kind=Product.Kind.PACKAGE, credit_type=CreditType.PT60, credits=10, price_cents=85000, expires_days=180
    )


@pytest.fixture
def class_pack(db):
    return Product.objects.create(
        name="10 classes", kind=Product.Kind.PACKAGE, credit_type=CreditType.CLASS, credits=10, price_cents=20000, expires_days=180
    )


@pytest.fixture
def give(now):
    def give(client, product, credits=None, expires_days=None, at=None):
        return grant_package(client, product, credits=credits, expires_days=expires_days, now=at or now - timedelta(days=1))

    return give


@pytest.fixture
def make_class(make_trainer, now):
    def make(trainer=None, start=None, capacity=2, minutes=45):
        trainer = trainer or make_trainer("Coach")
        start = start or now + timedelta(days=2)
        return Event.objects.create(
            kind=Event.Kind.CLASS,
            title="HIIT",
            trainer=trainer,
            start=start,
            end=start + timedelta(minutes=minutes),
            capacity=capacity,
            credit_type=CreditType.CLASS,
        )

    return make


def at(day_offset, hour, minute=0):
    return local_dt(MONDAY + timedelta(days=day_offset), time(hour, minute))
