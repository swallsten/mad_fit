"""Credit balances, deduction and refunds.

Rules:
- A booking first uses an active *unlimited* membership for that credit type.
- Otherwise it takes one credit from the client's package that expires soonest
  (packages with no expiry are used last), so credits aren't wasted.
- A package must still be valid when the session starts, not just today.
- Every change is written to CreditTransaction.
"""
from datetime import timedelta

from django.db.models import F, Q, Sum
from django.utils import timezone

from studio.models import (
    ClientPackage,
    CreditTransaction,
    CreditType,
    Membership,
)


class BookingError(Exception):
    """A rule stopped the booking. The message is safe to show to users."""


def active_unlimited_membership(client, credit_type):
    return (
        client.memberships.filter(
            status=Membership.Status.ACTIVE,
            product__credit_type=credit_type,
            product__credits__isnull=True,
        )
        .select_related("product")
        .first()
    )


def usable_packages(client, credit_type, at_time, now=None):
    """Packages that can pay for a session of this type starting at at_time."""
    now = now or timezone.now()
    valid_until = max(now, at_time)
    return (
        ClientPackage.objects.filter(client=client, credit_type=credit_type, credits_remaining__gt=0)
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=valid_until))
        .order_by(F("expires_at").asc(nulls_last=True), "purchased_at", "id")
    )


def can_pay_for(client, credit_type, at_time, now=None):
    return bool(active_unlimited_membership(client, credit_type)) or usable_packages(
        client, credit_type, at_time, now
    ).exists()


def consume_credit(client, booking, by_user=None, now=None):
    """Attach a payment source to booking and deduct a credit if needed.
    Caller must be inside transaction.atomic(). Raises BookingError before
    writing anything if the client can't pay."""
    now = now or timezone.now()
    event = booking.event
    credit_type = event.credit_type
    membership = active_unlimited_membership(client, credit_type)
    if membership:
        booking.membership = membership
        booking.package = None
        booking.save(update_fields=["membership", "package"])
        return None

    package = usable_packages(client, credit_type, event.start, now).select_for_update().first()
    if package is None:
        label = CreditType(credit_type).label.lower()
        expired = ClientPackage.objects.filter(
            client=client, credit_type=credit_type, credits_remaining__gt=0, expires_at__lte=max(now, event.start)
        ).exists()
        if expired:
            raise BookingError(f"Your {label} credits have expired (or expire before this date). Buy a new package to book.")
        raise BookingError(f"You don't have any {label} credits. Buy a package to book.")

    package.credits_remaining -= 1
    package.save(update_fields=["credits_remaining"])
    booking.package = package
    booking.membership = None
    booking.save(update_fields=["membership", "package"])
    CreditTransaction.objects.create(
        client=client,
        package=package,
        booking=booking,
        delta=-1,
        reason=CreditTransaction.Reason.BOOKING,
        created_by=by_user,
        created_at=now,
    )
    return package


def refund_credit(booking, reason, by_user=None, now=None):
    """Give back the credit a booking used (no-op for membership bookings)."""
    if not booking.package_id:
        return None
    package = ClientPackage.objects.select_for_update().get(pk=booking.package_id)
    package.credits_remaining = min(package.credits_remaining + 1, package.credits_total)
    package.save(update_fields=["credits_remaining"])
    CreditTransaction.objects.create(
        client=booking.client,
        package=package,
        booking=booking,
        delta=1,
        reason=reason,
        created_by=by_user,
        created_at=now or timezone.now(),
    )
    return package


def grant_package(
    client,
    product=None,
    *,
    name=None,
    credit_type=None,
    credits=None,
    expires_days=None,
    source=ClientPackage.Source.MANUAL,
    by_user=None,
    now=None,
    membership=None,
    checkout_session_id=None,
    invoice_id=None,
    note="",
):
    now = now or timezone.now()
    if product is not None:
        name = name or product.name
        credit_type = credit_type or product.credit_type
        credits = credits if credits is not None else product.credits
        expires_days = expires_days if expires_days is not None else product.expires_days
    if not credits:
        raise ValueError("A package needs a positive number of credits.")
    package = ClientPackage.objects.create(
        client=client,
        product=product,
        name=name,
        credit_type=credit_type,
        credits_total=credits,
        credits_remaining=credits,
        purchased_at=now,
        expires_at=now + timedelta(days=expires_days) if expires_days else None,
        source=source,
        membership=membership,
        stripe_checkout_session_id=checkout_session_id,
        stripe_invoice_id=invoice_id,
    )
    CreditTransaction.objects.create(
        client=client,
        package=package,
        delta=credits,
        reason=CreditTransaction.Reason.PURCHASE,
        note=note,
        created_by=by_user,
        created_at=now,
    )
    return package


def adjust_credits(package, delta, note, by_user=None, now=None):
    package = ClientPackage.objects.select_for_update().get(pk=package.pk)
    new_value = package.credits_remaining + delta
    if new_value < 0:
        raise BookingError("Adjustment would make the balance negative.")
    package.credits_remaining = new_value
    package.credits_total = max(package.credits_total, new_value)
    package.save(update_fields=["credits_remaining", "credits_total"])
    CreditTransaction.objects.create(
        client=package.client,
        package=package,
        delta=delta,
        reason=CreditTransaction.Reason.ADJUSTMENT,
        note=note,
        created_by=by_user,
        created_at=now or timezone.now(),
    )
    return package


def balances(client, now=None):
    """{credit_type: {"label", "credits", "unlimited", "next_expiry"}} for every type."""
    now = now or timezone.now()
    result = {}
    for ct in CreditType:
        live = ClientPackage.objects.filter(client=client, credit_type=ct, credits_remaining__gt=0).filter(
            Q(expires_at__isnull=True) | Q(expires_at__gt=now)
        )
        total = live.aggregate(n=Sum("credits_remaining"))["n"] or 0
        next_expiry = live.exclude(expires_at__isnull=True).order_by("expires_at").values_list("expires_at", flat=True).first()
        expired = (
            ClientPackage.objects.filter(client=client, credit_type=ct, credits_remaining__gt=0, expires_at__lte=now)
            .aggregate(n=Sum("credits_remaining"))["n"]
            or 0
        )
        result[ct.value] = {
            "label": ct.label,
            "credits": total,
            "unlimited": bool(active_unlimited_membership(client, ct)),
            "next_expiry": next_expiry,
            "expired": expired,
        }
    return result
