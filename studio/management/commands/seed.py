"""Fill the database with a realistic fake studio, relative to today.

    python manage.py seed           # only if the database is empty
    python manage.py seed --reset   # wipe and re-seed
"""
import random
from datetime import time, timedelta

from django.contrib.auth.hashers import make_password
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from studio.models import (
    Availability,
    Booking,
    ClassTemplate,
    ClientPackage,
    ClientProfile,
    CreditType,
    Event,
    Membership,
    Product,
    StudioSettings,
    TimeOff,
    TrainerProfile,
    User,
)
from studio.services import payments
from studio.services.booking import (
    BookingError,
    available_slots,
    book_event,
    book_session,
    cancel_booking,
    create_group_session,
    generate_classes,
    join_waitlist,
    local_dt,
    mark_attendance,
)
from studio.services.credits import adjust_credits, grant_package

PASSWORD = "demo1234"

TRAINERS = [
    # name, color, weekly availability {weekday: [(start, end), ...]}
    ("Alex Kim", "#2563eb", {d: [(6, 12)] for d in range(5)} | {5: [(8, 12)]}),
    ("Maya Patel", "#db2777", {d: [(12, 20)] for d in range(5)}),
    ("Chris Okafor", "#16a34a", {0: [(6, 14)], 2: [(6, 14)], 4: [(6, 14)], 5: [(7, 13)]}),
    ("Sam Rivera", "#ea580c", {1: [(9, 19)], 3: [(9, 19)], 5: [(8, 14)], 6: [(9, 13)]}),
    ("Priya Shah", "#7c3aed", {d: [(7, 11), (16, 20)] for d in range(5)}),
    ("Dev Morgan", "#0891b2", {0: [(14, 20)], 1: [(6, 12)], 2: [(14, 20)], 3: [(6, 12)], 4: [(10, 16)]}),
    ("Leah Chen", "#ca8a04", {1: [(6, 14)], 2: [(6, 14)], 3: [(6, 14)], 4: [(6, 14)], 5: [(8, 12)]}),
]

PRODUCTS = [
    # name, kind, credit_type, credits, price $, expires_days, one_per_client, description
    ("Intro Offer: 3 Sessions", "intro", "pt60", 3, 99, 30, True, "New clients only. Three 60-minute sessions."),
    ("10 x 60-min Sessions", "package", "pt60", 10, 850, 180, False, "Our most popular package."),
    ("5 x 60-min Sessions", "package", "pt60", 5, 450, 90, False, ""),
    ("10 x 30-min Sessions", "package", "pt30", 10, 500, 180, False, "Express sessions."),
    ("10 Small-Group Sessions", "package", "group", 10, 400, 120, False, "Semi-private, 2–4 clients."),
    ("10-Class Pack", "package", "class", 10, 200, 180, False, ""),
    ("Class Drop-in", "drop_in", "class", 1, 25, 30, False, "One group class."),
    ("Unlimited Classes", "membership", "class", None, 149, None, False, "Every group class, billed monthly."),
    ("4 Sessions / Month", "membership", "pt60", 4, 340, 90, False, "Four 60-min sessions added each month; unused ones roll over."),
]

CLASSES = [
    # title, trainer index, weekdays, start (h, m), minutes, capacity
    ("Sunrise HIIT", 0, [0, 2, 4], (6, 0), 45, 10),
    ("Strength Foundations", 6, [1, 3], (7, 0), 60, 8),
    ("Lunch Express Core", 1, [0, 2], (12, 15), 30, 12),
    ("Bootcamp", 4, [1, 3], (18, 0), 45, 6),
    ("Mobility & Stretch", 3, [5], (9, 0), 45, 12),
    ("Kettlebell Flow", 2, [5], (10, 30), 45, 8),
    ("Evening Strength", 5, [0, 2], (18, 30), 60, 10),
]

FIRST = ["Olivia", "Liam", "Ava", "Noah", "Sophia", "Ethan", "Mia", "Lucas", "Isabella", "Mason", "Harper", "Logan",
         "Ella", "James", "Aria", "Benjamin", "Chloe", "Henry", "Grace", "Daniel", "Zoe", "Owen", "Nora", "Caleb",
         "Lily", "Ryan", "Hannah", "Nathan", "Layla", "Isaac", "Riley", "Aaron", "Stella", "Julian", "Maria", "Tyler", "Wren"]
LAST = ["Brooks", "Nguyen", "Garcia", "Thompson", "Martinez", "Wilson", "Anderson", "Lee", "Hernandez", "Moore",
        "Jackson", "Martin", "Lopez", "Clark", "Lewis", "Walker", "Hall", "Allen", "Young", "King", "Wright", "Scott",
        "Green", "Baker", "Adams", "Nelson", "Hill", "Campbell", "Mitchell", "Roberts", "Turner", "Phillips", "Evans",
        "Parker", "Edwards", "Collins", "Foster"]


class Command(BaseCommand):
    help = "Seed the database with a fake studio (7 trainers, ~40 clients, classes, bookings)."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete all data first.")

    def handle(self, *args, reset=False, **opts):
        if reset:
            call_command("flush", interactive=False, verbosity=0)
        elif User.objects.exists():
            self.stdout.write("Database already has data — skipping seed (use --reset to start over).")
            return
        self.rng = random.Random(42)
        self.now = timezone.now()
        self.today = timezone.localdate()
        self.hashed = make_password(PASSWORD)
        with transaction.atomic():
            self.seed()
        self.stdout.write(self.style.SUCCESS("Seeded. Log in as owner@studio.test / demo1234 (see README for others)."))

    # ------------------------------------------------------------ helpers

    def user(self, email, first, last, role, **extra):
        # hash once and reuse: hashing 48 passwords separately takes ~10s
        return User.objects.create(username=email, email=email, password=self.hashed, first_name=first, last_name=last, role=role, **extra)

    def client(self, first, last, trainer=None, phone=""):
        u = self.user(f"{first}.{last}@example.com".lower(), first, last, User.Role.CLIENT, phone=phone)
        return ClientProfile.objects.create(user=u, primary_trainer=trainer)

    def give(self, client, key, days_ago, **kw):
        return grant_package(client, self.p[key], now=self.now - timedelta(days=days_ago), note="Seed data", **kw)

    def membership(self, client, key, status=Membership.Status.ACTIVE, days_ago=40):
        m = Membership.objects.create(
            client=client,
            product=self.p[key],
            status=status,
            started_at=self.now - timedelta(days=days_ago),
            current_period_end=self.now + timedelta(days=self.rng.randint(3, 25)),
            stripe_subscription_id=f"sub_seed_{client.pk}_{key[:6]}",
        )
        if self.p[key].credits:
            grant_package(client, self.p[key], source=ClientPackage.Source.MEMBERSHIP, membership=m,
                          now=self.now - timedelta(days=days_ago % 30), note="Monthly membership credits")
        return m

    def random_slot(self, trainer, duration, day_range, now):
        days = [self.today + timedelta(days=d) for d in day_range]
        self.rng.shuffle(days)
        for day in days:
            slots = available_slots(trainer, day, duration, now=now, enforce_notice=(now == self.now))
            if slots:
                return self.rng.choice(slots)
        return None

    def past_session(self, client, trainer, duration):
        start = self.random_slot(trainer, duration, range(-14, 0), now=self.now - timedelta(days=20))
        if not start:
            return
        try:
            b = book_session(client, trainer, start, duration, now=start - timedelta(days=4), staff_override=True)
        except BookingError:
            return
        roll = self.rng.random()
        if roll < 0.08:
            cancel_booking(b, now=start - timedelta(hours=5))  # late cancel
        elif roll < 0.15:
            mark_attendance(b, Booking.Status.NO_SHOW, now=start + timedelta(minutes=20))
        else:
            mark_attendance(b, Booking.Status.ATTENDED, now=start + timedelta(minutes=50))

    def future_session(self, client, trainer, duration):
        start = self.random_slot(trainer, duration, range(0, 12), now=self.now)
        if start:
            try:
                book_session(client, trainer, start, duration, booked_by=client.user, now=self.now)
            except BookingError:
                pass

    # ------------------------------------------------------------ seed

    def seed(self):
        cfg = StudioSettings.load()
        cfg.studio_name = "MAD Fitness"
        cfg.save()

        self.user("owner@studio.test", "Jordan", "Reyes", User.Role.ADMIN, is_staff=True, is_superuser=True)

        trainers = []
        for i, (name, color, hours) in enumerate(TRAINERS):
            first, last = name.split()
            u = self.user(f"{first.lower()}@studio.test", first, last, User.Role.TRAINER)
            t = TrainerProfile.objects.create(user=u, color=color, sort_order=i, bio=f"{first} is a certified personal trainer.")
            for wd, windows in hours.items():
                for s, e in windows:
                    Availability.objects.create(trainer=t, weekday=wd, start_time=time(s), end_time=time(e))
            trainers.append(t)
        # time off: Maya out all day 3 days from now; Chris out a morning next week
        off_day = self.today + timedelta(days=3)
        TimeOff.objects.create(trainer=trainers[1], start=local_dt(off_day, time(0)), end=local_dt(off_day + timedelta(days=1), time(0)), reason="Personal day")
        nxt = self.today + timedelta(days=8)
        TimeOff.objects.create(trainer=trainers[2], start=local_dt(nxt, time(6)), end=local_dt(nxt, time(10)), reason="Doctor's appointment")

        self.p = {}
        for i, (name, kind, ct, credits, price, exp, once, desc) in enumerate(PRODUCTS):
            key = {"Intro": "intro", "10 x 60": "pk60", "5 x 60": "pk60s", "10 x 30": "pk30", "10 Small": "group",
                   "10-Class": "classpack", "Class Drop": "dropin", "Unlimited": "unlimited", "4 Sessions": "monthly4"}
            k = next(v for prefix, v in key.items() if name.startswith(prefix))
            self.p[k] = Product.objects.create(name=name, kind=kind, credit_type=ct, credits=credits, price_cents=price * 100,
                                               expires_days=exp, one_per_client=once, description=desc, sort_order=i)

        for title, ti, weekdays, (h, m), minutes, cap in CLASSES:
            for wd in weekdays:
                ClassTemplate.objects.create(title=title, trainer=trainers[ti], weekday=wd, start_time=time(h, m),
                                             duration_minutes=minutes, capacity=cap)
        generate_classes(self.today - timedelta(days=14), weeks=5)

        # ---- named demo clients (documented in README)
        emily = self.client("Emily", "Carter", trainers[0], "555-0101")
        self.give(emily, "pk60", 35)
        self.membership(emily, "unlimited")
        dana = self.client("Dana", "Whitfield", trainers[1], "555-0102")
        old = self.give(dana, "pk60", 200)  # expired 20 days ago with 3 credits left
        adjust_credits(old, -7, "Sessions used before this system", now=self.now - timedelta(days=60))
        marcus = self.client("Marcus", "Bell", trainers[4], "555-0103")
        self.give(marcus, "pk60s", 20)
        self.membership(marcus, "unlimited", days_ago=62)
        olivia = self.client("Olivia", "Brooks", trainers[3], "555-0104")
        self.give(olivia, "classpack", 10)
        self.give(olivia, "pk30", 15)

        # ---- 36 more clients with a mix of packages and memberships
        names = [(f, l) for f, l in zip(FIRST[1:], LAST[1:])]
        clients = []
        for idx, (first, last) in enumerate(names):
            c = self.client(first, last, trainers[idx % 7], f"555-{1000 + idx}")
            kind = idx % 9
            if kind in (0, 1, 2):
                self.give(c, "pk60", self.rng.randint(10, 90))
            elif kind == 3:
                self.give(c, "pk30", self.rng.randint(10, 60))
            elif kind == 4:
                self.give(c, "intro", self.rng.randint(2, 15))
            elif kind == 5:
                self.membership(c, "unlimited")
            elif kind == 6:
                self.give(c, "classpack", self.rng.randint(5, 60))
                self.give(c, "pk60s", self.rng.randint(5, 40))
            elif kind == 7:
                self.give(c, "group", self.rng.randint(5, 40))
                self.give(c, "classpack", self.rng.randint(5, 40))
            else:
                self.membership(c, "monthly4")
            clients.append((c, kind))

        # ---- 1:1 sessions: past (for the trainer report) and upcoming
        for c, kind in [(emily, 0), (marcus, 6)] + clients:
            pkg = c.packages.filter(credit_type__in=[CreditType.PT60, CreditType.PT30]).first()
            if not pkg:
                continue
            duration = 30 if pkg.credit_type == CreditType.PT30 else 60
            trainer = c.primary_trainer
            for _ in range(self.rng.randint(1, 3)):
                self.past_session(c, trainer, duration)
            for _ in range(self.rng.randint(1, 2)):
                self.future_session(c, trainer, duration)

        # ---- class bookings
        class_clients = [c for c, k in clients if k in (5, 6, 7)] + [emily, marcus]
        past_classes = list(Event.objects.filter(kind="class", start__lt=self.now, start__gte=self.now - timedelta(days=14)))
        future_classes = list(Event.objects.filter(kind="class", start__gte=self.now + timedelta(hours=3)))
        for c in class_clients:
            for ev in self.rng.sample(past_classes, min(len(past_classes), self.rng.randint(2, 4))):
                try:
                    b = book_event(c, ev, now=ev.start - timedelta(days=2), staff_override=True)
                except BookingError:
                    continue
                status = Booking.Status.NO_SHOW if self.rng.random() < 0.08 else Booking.Status.ATTENDED
                mark_attendance(b, status, now=ev.end)
            for ev in self.rng.sample(future_classes, min(len(future_classes), self.rng.randint(1, 3))):
                try:
                    book_event(c, ev, booked_by=c.user, now=self.now)
                except BookingError:
                    pass

        # ---- a full Bootcamp with a waitlist: Olivia is booked, Emily is #1 on the waitlist
        bootcamp = (
            Event.objects.filter(title="Bootcamp", start__gte=self.now + timedelta(hours=30)).order_by("start").first()
        )
        second = next(c for c, k in clients if k == 5)
        Booking.objects.filter(event=bootcamp, client__in=[emily, second]).delete()  # unlimited members: no credit to refund
        fillers = [olivia] + [c for c, k in clients if k in (6, 7)]
        for c in fillers:
            if bootcamp.booked_count() >= bootcamp.capacity:
                break
            try:
                book_event(c, bootcamp, now=self.now)
            except BookingError:
                pass
        # top up with class credits for anyone needed to fill it
        spare = iter([c for c, k in clients if k in (0, 1, 2)])
        while bootcamp.booked_count() < bootcamp.capacity:
            c = next(spare)
            self.give(c, "dropin", 1)
            try:
                book_event(c, bootcamp, now=self.now)
            except BookingError:
                pass
        join_waitlist(emily, bootcamp, now=self.now)
        join_waitlist(second, bootcamp, now=self.now)

        # ---- small-group sessions with Sam
        sam = trainers[3]
        group_clients = [c for c, k in clients if k == 7]
        for offset, h in [(-3, 17), (2, 17), (6, 17)]:
            day = self.today + timedelta(days=offset)
            try:
                ev = create_group_session(sam, local_dt(day, time(h)), 60, 4, "Small-group strength")
            except BookingError:
                continue
            for c in group_clients[:3]:
                try:
                    b = book_event(c, ev, now=min(self.now, ev.start - timedelta(days=1)), staff_override=True)
                    if ev.end < self.now:
                        mark_attendance(b, Booking.Status.ATTENDED, now=ev.end)
                except BookingError:
                    pass

        # ---- Marcus: existing booking kept, then his membership payment fails
        payments.flag_client(
            marcus,
            "Payment of $149.00 for Unlimited Classes failed (card declined). Update your card to keep booking.",
            now=self.now - timedelta(days=2),
        )
        marcus.memberships.update(status=Membership.Status.PAST_DUE)
