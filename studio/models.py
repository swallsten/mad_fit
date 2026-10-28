"""Data model, using PT Minder's vocabulary: trainers, clients, sessions,
classes, packages, memberships.

Credits are typed (30-min session, 60-min session, small-group, class). A
client's spendable balance lives on ClientPackage.credits_remaining; every
change to it is also written to CreditTransaction so balances can be audited
and, later, imported from a PT Minder export.
"""
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import Q
from django.utils import timezone


class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        TRAINER = "trainer", "Trainer"
        CLIENT = "client", "Client"

    role = models.CharField(max_length=10, choices=Role.choices, default=Role.CLIENT)
    phone = models.CharField(max_length=30, blank=True)

    @property
    def is_studio_admin(self):
        return self.role == self.Role.ADMIN or self.is_superuser

    @property
    def is_trainer(self):
        return self.role == self.Role.TRAINER

    @property
    def is_client(self):
        return self.role == self.Role.CLIENT

    def __str__(self):
        return self.get_full_name() or self.username


class StudioSettings(models.Model):
    """Singleton (pk=1). Use StudioSettings.load()."""

    studio_name = models.CharField(max_length=100, default="MAD Fitness")
    late_cancel_hours = models.PositiveIntegerField(
        default=24, help_text="Cancelling within this many hours of the start uses the credit."
    )
    waitlist_cutoff_hours = models.PositiveIntegerField(
        default=2, help_text="No automatic waitlist promotion this close to the start."
    )
    min_booking_notice_hours = models.PositiveIntegerField(
        default=2, help_text="Clients can't book 1:1 sessions starting sooner than this."
    )
    booking_window_days = models.PositiveIntegerField(
        default=21, help_text="How far ahead clients can see and book."
    )
    slot_step_minutes = models.PositiveIntegerField(default=30)

    class Meta:
        verbose_name_plural = "studio settings"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "Studio settings"


class CreditType(models.TextChoices):
    PT30 = "pt30", "30-min session"
    PT60 = "pt60", "60-min session"
    GROUP = "group", "Small-group session"
    CLASS = "class", "Class"


SESSION_CREDIT_FOR_DURATION = {30: CreditType.PT30, 60: CreditType.PT60}


class TrainerProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="trainer")
    bio = models.TextField(blank=True)
    color = models.CharField(max_length=7, default="#3b82f6")
    active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "user__first_name"]

    @property
    def name(self):
        return str(self.user)

    def __str__(self):
        return self.name


class Availability(models.Model):
    """A recurring weekly window in which the trainer takes 1:1 bookings.
    Times are studio-local wall-clock times."""

    WEEKDAYS = [(0, "Mon"), (1, "Tue"), (2, "Wed"), (3, "Thu"), (4, "Fri"), (5, "Sat"), (6, "Sun")]
    trainer = models.ForeignKey(TrainerProfile, on_delete=models.CASCADE, related_name="availability")
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAYS)
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        ordering = ["trainer", "weekday", "start_time"]
        verbose_name_plural = "availability"
        constraints = [
            models.CheckConstraint(condition=Q(end_time__gt=models.F("start_time")), name="availability_end_after_start")
        ]

    def __str__(self):
        return f"{self.trainer} {self.get_weekday_display()} {self.start_time:%H:%M}-{self.end_time:%H:%M}"


class TimeOff(models.Model):
    trainer = models.ForeignKey(TrainerProfile, on_delete=models.CASCADE, related_name="time_off")
    start = models.DateTimeField()
    end = models.DateTimeField()
    reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["start"]
        verbose_name_plural = "time off"
        constraints = [models.CheckConstraint(condition=Q(end__gt=models.F("start")), name="timeoff_end_after_start")]

    def __str__(self):
        return f"{self.trainer} off {self.start:%Y-%m-%d %H:%M}"


class ClientProfile(models.Model):
    class PaymentStatus(models.TextChoices):
        OK = "ok", "Good standing"
        FAILED = "failed", "Payment failed"

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="client")
    primary_trainer = models.ForeignKey(
        TrainerProfile, null=True, blank=True, on_delete=models.SET_NULL, related_name="primary_clients"
    )
    stripe_customer_id = models.CharField(max_length=100, blank=True)
    payment_status = models.CharField(max_length=10, choices=PaymentStatus.choices, default=PaymentStatus.OK)
    payment_flag_reason = models.CharField(max_length=255, blank=True)
    payment_flagged_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    # Placeholder for a future PT Minder import (maps their client id to ours).
    external_id = models.CharField(max_length=100, blank=True, db_index=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["user__last_name", "user__first_name"]

    @property
    def name(self):
        return str(self.user)

    @property
    def is_payment_flagged(self):
        return self.payment_status == self.PaymentStatus.FAILED

    def __str__(self):
        return self.name


class Product(models.Model):
    """Something a client can buy: a package, membership, drop-in or intro offer."""

    class Kind(models.TextChoices):
        PACKAGE = "package", "Session package"
        MEMBERSHIP = "membership", "Monthly membership"
        DROP_IN = "drop_in", "Drop-in"
        INTRO = "intro", "Intro offer"

    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=12, choices=Kind.choices)
    description = models.CharField(max_length=255, blank=True)
    credit_type = models.CharField(max_length=8, choices=CreditType.choices)
    credits = models.PositiveIntegerField(
        null=True, blank=True, help_text="Credits granted per purchase (or per month for memberships). Blank = unlimited."
    )
    price_cents = models.PositiveIntegerField()
    expires_days = models.PositiveIntegerField(
        null=True, blank=True, help_text="Credits expire this many days after purchase. Blank = never."
    )
    one_per_client = models.BooleanField(default=False, help_text="E.g. intro offers.")
    active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)
    stripe_price_id = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["sort_order", "price_cents"]

    @property
    def is_recurring(self):
        return self.kind == self.Kind.MEMBERSHIP

    @property
    def is_unlimited(self):
        return self.credits is None

    @property
    def price_display(self):
        s = f"${self.price_cents / 100:,.2f}".replace(".00", "")
        return f"{s}/mo" if self.is_recurring else s

    def __str__(self):
        return self.name


class Membership(models.Model):
    """A client's recurring subscription (backed by a Stripe subscription)."""

    class Status(models.TextChoices):
        INCOMPLETE = "incomplete", "Incomplete"
        ACTIVE = "active", "Active"
        PAST_DUE = "past_due", "Past due"
        CANCELLED = "cancelled", "Cancelled"

    client = models.ForeignKey(ClientProfile, on_delete=models.CASCADE, related_name="memberships")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.INCOMPLETE)
    stripe_subscription_id = models.CharField(max_length=100, blank=True, null=True, unique=True)
    started_at = models.DateTimeField(default=timezone.now)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.client} — {self.product} ({self.status})"


class ClientPackage(models.Model):
    """A block of credits a client owns: one package purchase, one drop-in,
    one month of a session membership, or a manual grant."""

    class Source(models.TextChoices):
        STRIPE = "stripe", "Stripe"
        MANUAL = "manual", "Manual (cash/comp)"
        MEMBERSHIP = "membership", "Membership renewal"
        IMPORT = "import", "Imported"

    client = models.ForeignKey(ClientProfile, on_delete=models.CASCADE, related_name="packages")
    product = models.ForeignKey(Product, null=True, blank=True, on_delete=models.SET_NULL)
    name = models.CharField(max_length=100)
    credit_type = models.CharField(max_length=8, choices=CreditType.choices)
    credits_total = models.PositiveIntegerField()
    credits_remaining = models.PositiveIntegerField()
    purchased_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=12, choices=Source.choices, default=Source.MANUAL)
    membership = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="grants")
    # Idempotency keys so a replayed webhook never grants credits twice.
    stripe_checkout_session_id = models.CharField(max_length=100, null=True, blank=True, unique=True)
    stripe_invoice_id = models.CharField(max_length=100, null=True, blank=True, unique=True)

    class Meta:
        ordering = ["expires_at", "purchased_at"]

    def is_expired(self, now=None):
        now = now or timezone.now()
        return self.expires_at is not None and self.expires_at <= now

    def __str__(self):
        return f"{self.client}: {self.name} ({self.credits_remaining}/{self.credits_total})"


class ClassTemplate(models.Model):
    """A recurring weekly class. Concrete dated classes are Events generated from it."""

    title = models.CharField(max_length=100)
    trainer = models.ForeignKey(TrainerProfile, on_delete=models.PROTECT, related_name="class_templates")
    weekday = models.PositiveSmallIntegerField(choices=Availability.WEEKDAYS)
    start_time = models.TimeField()
    duration_minutes = models.PositiveIntegerField(default=45)
    capacity = models.PositiveIntegerField(default=10)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["weekday", "start_time"]

    def __str__(self):
        return f"{self.title} — {self.get_weekday_display()} {self.start_time:%H:%M}"


class Event(models.Model):
    """Anything on a trainer's calendar: a 1:1 session, a small-group session or a class."""

    class Kind(models.TextChoices):
        SESSION = "session", "1:1 session"
        GROUP = "group", "Small-group session"
        CLASS = "class", "Class"

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        CANCELLED = "cancelled", "Cancelled"

    kind = models.CharField(max_length=8, choices=Kind.choices)
    title = models.CharField(max_length=100)
    trainer = models.ForeignKey(TrainerProfile, on_delete=models.PROTECT, related_name="events")
    start = models.DateTimeField(db_index=True)
    end = models.DateTimeField()
    capacity = models.PositiveIntegerField(default=1)
    credit_type = models.CharField(max_length=8, choices=CreditType.choices)
    template = models.ForeignKey(ClassTemplate, null=True, blank=True, on_delete=models.SET_NULL, related_name="events")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SCHEDULED)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["start"]
        constraints = [
            models.CheckConstraint(condition=Q(end__gt=models.F("start")), name="event_end_after_start"),
            models.UniqueConstraint(fields=["template", "start"], name="one_event_per_template_start"),
        ]

    @property
    def duration_minutes(self):
        return int((self.end - self.start).total_seconds() // 60)

    @property
    def is_cancelled(self):
        return self.status == self.Status.CANCELLED

    def booked_count(self):
        return self.bookings.filter(status__in=Booking.SEATED_STATUSES).count()

    def spots_left(self):
        return max(self.capacity - self.booked_count(), 0)

    def waitlist(self):
        return self.bookings.filter(status=Booking.Status.WAITLISTED).order_by("waitlist_position", "created_at")

    def __str__(self):
        local = timezone.localtime(self.start)
        return f"{self.title} — {self.trainer} {local:%a %b %-d %-I:%M%p}"


class Booking(models.Model):
    class Status(models.TextChoices):
        BOOKED = "booked", "Booked"
        WAITLISTED = "waitlisted", "Waitlisted"
        ATTENDED = "attended", "Attended"
        NO_SHOW = "no_show", "No-show"
        LATE_CANCEL = "late_cancel", "Late cancel"
        CANCELLED = "cancelled", "Cancelled"
        REMOVED = "removed", "Left waitlist"

    # Statuses that occupy a seat in the event.
    SEATED_STATUSES = [Status.BOOKED, Status.ATTENDED, Status.NO_SHOW]
    # Statuses that block the client's own calendar (for double-booking checks).
    ACTIVE_STATUSES = [Status.BOOKED, Status.ATTENDED]

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="bookings")
    client = models.ForeignKey(ClientProfile, on_delete=models.CASCADE, related_name="bookings")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.BOOKED)
    waitlist_position = models.PositiveIntegerField(null=True, blank=True)
    package = models.ForeignKey(ClientPackage, null=True, blank=True, on_delete=models.SET_NULL, related_name="bookings")
    membership = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="bookings")
    booked_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["event__start"]
        constraints = [
            # A client can hold at most one live booking/waitlist spot per event.
            models.UniqueConstraint(
                fields=["event", "client"],
                condition=Q(status__in=["booked", "waitlisted", "attended", "no_show"]),
                name="one_live_booking_per_client_event",
            )
        ]

    @property
    def paid_with(self):
        if self.membership_id:
            return self.membership.product.name
        if self.package_id:
            return self.package.name
        return "—"

    def __str__(self):
        return f"{self.client} @ {self.event} [{self.status}]"


class CreditTransaction(models.Model):
    """Audit ledger. delta is negative when a credit is used."""

    class Reason(models.TextChoices):
        PURCHASE = "purchase", "Purchase"
        BOOKING = "booking", "Booked"
        CANCEL_REFUND = "cancel_refund", "Cancelled in time (refund)"
        WAIVED = "waived", "Late cancel waived (refund)"
        EVENT_CANCELLED = "event_cancelled", "Studio cancelled (refund)"
        ADJUSTMENT = "adjustment", "Manual adjustment"

    client = models.ForeignKey(ClientProfile, on_delete=models.CASCADE, related_name="credit_transactions")
    package = models.ForeignKey(ClientPackage, on_delete=models.CASCADE, related_name="transactions")
    booking = models.ForeignKey(Booking, null=True, blank=True, on_delete=models.SET_NULL, related_name="transactions")
    delta = models.IntegerField()
    reason = models.CharField(max_length=16, choices=Reason.choices)
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-id"]


class StripeEvent(models.Model):
    """Processed webhook ids, so replays are ignored."""

    event_id = models.CharField(max_length=100, unique=True)
    type = models.CharField(max_length=100)
    received_at = models.DateTimeField(auto_now_add=True)


class Notification(models.Model):
    """Outbox for future email/SMS reminders. Nothing sends these yet."""

    class Channel(models.TextChoices):
        EMAIL = "email", "Email"
        SMS = "sms", "SMS"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    channel = models.CharField(max_length=5, choices=Channel.choices, default=Channel.EMAIL)
    subject = models.CharField(max_length=200)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
