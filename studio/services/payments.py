"""Stripe integration (TEST MODE ONLY).

Network calls live in the top half. Fulfilment (the bottom half) works on
plain dicts, so the same code handles webhooks, the checkout success page
(which lets purchases work locally without webhook forwarding), and tests.
Every handler is idempotent: replays never grant credits twice.
"""
import json
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from studio.models import ClientPackage, ClientProfile, Membership, Product, StripeEvent

from .credits import BookingError, grant_package


class StripeNotConfigured(Exception):
    pass


def stripe_enabled():
    return settings.STRIPE_SECRET_KEY.startswith(("sk_test_", "rk_test_"))


def _stripe():
    import stripe

    key = settings.STRIPE_SECRET_KEY
    if not key:
        raise StripeNotConfigured("Stripe isn't set up yet. Add test keys to .env (see README).")
    if not key.startswith(("sk_test_", "rk_test_")):
        raise ImproperlyConfigured("Refusing to use a live Stripe key. This prototype is test-mode only.")
    stripe.api_key = key
    return stripe


def as_dict(obj):
    return obj if isinstance(obj, dict) else json.loads(str(obj))


# ---------------------------------------------------------------- outbound calls


def ensure_customer(client):
    if client.stripe_customer_id:
        return client.stripe_customer_id
    customer = _stripe().Customer.create(
        email=client.user.email, name=client.name, metadata={"client_id": str(client.pk)}
    )
    client.stripe_customer_id = customer.id
    client.save(update_fields=["stripe_customer_id"])
    return customer.id


def ensure_price(product):
    if product.stripe_price_id:
        return product.stripe_price_id
    kwargs = {
        "currency": "usd",
        "unit_amount": product.price_cents,
        "product_data": {"name": product.name, "metadata": {"product_id": str(product.pk)}},
    }
    if product.is_recurring:
        kwargs["recurring"] = {"interval": "month"}
    price = _stripe().Price.create(**kwargs)
    product.stripe_price_id = price.id
    product.save(update_fields=["stripe_price_id"])
    return price.id


def check_can_buy(client, product):
    if not product.active:
        raise BookingError("That product isn't available.")
    if product.one_per_client and ClientPackage.objects.filter(client=client, product=product).exists():
        raise BookingError(f"{product.name} can only be purchased once.")
    if product.is_recurring and client.memberships.filter(
        product=product, status__in=[Membership.Status.ACTIVE, Membership.Status.PAST_DUE]
    ).exists():
        raise BookingError(f"You already have {product.name}. Manage it under Billing.")


def create_checkout_session(client, product):
    check_can_buy(client, product)
    stripe = _stripe()
    metadata = {"client_id": str(client.pk), "product_id": str(product.pk)}
    params = {
        "mode": "subscription" if product.is_recurring else "payment",
        "customer": ensure_customer(client),
        "success_url": settings.SITE_URL + reverse("checkout_success") + "?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": settings.SITE_URL + reverse("client_buy"),
        "metadata": metadata,
        "client_reference_id": str(client.pk),
    }
    if product.is_recurring:
        params["subscription_data"] = {"metadata": metadata}
    for attempt in range(2):
        params["line_items"] = [{"price": ensure_price(product), "quantity": 1}]
        try:
            return stripe.checkout.Session.create(**params).url
        except stripe.InvalidRequestError:
            if attempt:  # stale price id (e.g. keys changed to another account): recreate once
                raise
            product.stripe_price_id = ""
            product.save(update_fields=["stripe_price_id"])


def customer_portal_url(client, return_url):
    stripe = _stripe()
    session = stripe.billing_portal.Session.create(customer=ensure_customer(client), return_url=return_url)
    return session.url


def sync_checkout_session(session_id):
    """Called from the success page: fetch the session and fulfil it now,
    instead of waiting for (or depending on) the webhook."""
    stripe = _stripe()
    session = as_dict(stripe.checkout.Session.retrieve(session_id, expand=["subscription.latest_invoice"]))
    result = fulfill_checkout_session(session)
    sub = session.get("subscription")
    if isinstance(sub, dict) and isinstance(sub.get("latest_invoice"), dict):
        invoice = sub["latest_invoice"]
        if invoice.get("status") == "paid":
            handle_invoice_paid(invoice, subscription=sub)
    return session, result


# ---------------------------------------------------------------- flags


def flag_client(client, reason, now=None):
    client.payment_status = ClientProfile.PaymentStatus.FAILED
    client.payment_flag_reason = reason[:255]
    client.payment_flagged_at = now or timezone.now()
    client.save(update_fields=["payment_status", "payment_flag_reason", "payment_flagged_at"])


def clear_flag(client):
    client.payment_status = ClientProfile.PaymentStatus.OK
    client.payment_flag_reason = ""
    client.payment_flagged_at = None
    client.save(update_fields=["payment_status", "payment_flag_reason", "payment_flagged_at"])


# ---------------------------------------------------------------- fulfilment (dict in, no network)


def _id(value):
    return value.get("id") if isinstance(value, dict) else value


def _ts(value):
    return datetime.fromtimestamp(value, tz=dt_timezone.utc) if value else None


def _client_product(metadata):
    try:
        client = ClientProfile.objects.get(pk=int(metadata["client_id"]))
        product = Product.objects.get(pk=int(metadata["product_id"]))
    except (KeyError, ValueError, ClientProfile.DoesNotExist, Product.DoesNotExist):
        return None, None
    return client, product


def fulfill_checkout_session(session, now=None):
    """checkout.session.completed: grant a package, or record the membership.
    Membership credits are granted on invoice.paid."""
    client, product = _client_product(session.get("metadata") or {})
    if not client:
        return None
    with transaction.atomic():
        if session.get("customer") and not client.stripe_customer_id:
            client.stripe_customer_id = _id(session["customer"])
            client.save(update_fields=["stripe_customer_id"])
        if session.get("mode") == "subscription":
            sub_id = _id(session.get("subscription"))
            if not sub_id:
                return None
            membership, _ = Membership.objects.get_or_create(
                stripe_subscription_id=sub_id, defaults={"client": client, "product": product}
            )
            return membership
        if session.get("payment_status") != "paid":
            return None
        existing = ClientPackage.objects.filter(stripe_checkout_session_id=session["id"]).first()
        if existing:
            return existing
        return grant_package(
            client,
            product,
            source=ClientPackage.Source.STRIPE,
            checkout_session_id=session["id"],
            now=now,
            note="Stripe Checkout",
        )


def _invoice_subscription(invoice):
    """(subscription_id, metadata) across Stripe API versions: older ones put
    `subscription` on the invoice; newer ones nest it under `parent`."""
    sub = invoice.get("subscription")
    meta = {}
    parent = invoice.get("parent") or {}
    details = parent.get("subscription_details") or invoice.get("subscription_details") or {}
    if not sub:
        sub = details.get("subscription")
    meta = details.get("metadata") or {}
    lines = (invoice.get("lines") or {}).get("data") or []
    if lines:
        line = lines[0]
        if not sub:
            sub = line.get("subscription") or ((line.get("parent") or {}).get("subscription_item_details") or {}).get(
                "subscription"
            )
        meta = meta or line.get("metadata") or {}
    if isinstance(sub, dict):
        meta = meta or sub.get("metadata") or {}
    return _id(sub), meta


def _period_end(invoice):
    lines = (invoice.get("lines") or {}).get("data") or []
    if lines and lines[0].get("period"):
        return _ts(lines[0]["period"].get("end"))
    return _ts(invoice.get("period_end"))


def _membership_for(sub_id, meta):
    membership = Membership.objects.filter(stripe_subscription_id=sub_id).select_related("client", "product").first()
    if membership:
        return membership
    client, product = _client_product(meta)
    if not client:
        return None
    # invoice.paid can arrive before checkout.session.completed
    membership, _ = Membership.objects.get_or_create(
        stripe_subscription_id=sub_id, defaults={"client": client, "product": product}
    )
    return membership


def handle_invoice_paid(invoice, subscription=None, now=None):
    sub_id, meta = _invoice_subscription(invoice)
    if not sub_id and subscription:
        sub_id, meta = subscription.get("id"), subscription.get("metadata") or {}
    if not sub_id:
        return None
    if not meta and subscription:
        meta = subscription.get("metadata") or {}
    with transaction.atomic():
        membership = _membership_for(sub_id, meta)
        if not membership:
            return None
        membership.status = Membership.Status.ACTIVE
        membership.current_period_end = _period_end(invoice) or membership.current_period_end
        membership.save(update_fields=["status", "current_period_end"])
        product = membership.product
        if product.credits and not ClientPackage.objects.filter(stripe_invoice_id=invoice["id"]).exists():
            grant_package(
                membership.client,
                product,
                source=ClientPackage.Source.MEMBERSHIP,
                membership=membership,
                invoice_id=invoice["id"],
                now=now,
                note="Monthly membership credits",
            )
        client = membership.client
        if client.is_payment_flagged and not client.memberships.filter(status=Membership.Status.PAST_DUE).exists():
            clear_flag(client)
    return membership


def handle_invoice_failed(invoice, now=None):
    sub_id, meta = _invoice_subscription(invoice)
    with transaction.atomic():
        membership = _membership_for(sub_id, meta) if sub_id else None
        client = membership.client if membership else None
        if membership:
            membership.status = Membership.Status.PAST_DUE
            membership.save(update_fields=["status"])
        if client is None and invoice.get("customer"):
            client = ClientProfile.objects.filter(stripe_customer_id=_id(invoice["customer"])).first()
        if client is None:
            return None
        what = membership.product.name if membership else "an invoice"
        amount = (invoice.get("amount_due") or 0) / 100
        flag_client(client, f"Payment of ${amount:,.2f} for {what} failed. Update your card to keep booking.", now)
    return client


SUB_STATUS = {
    "active": Membership.Status.ACTIVE,
    "trialing": Membership.Status.ACTIVE,
    "past_due": Membership.Status.PAST_DUE,
    "unpaid": Membership.Status.PAST_DUE,
    "incomplete": Membership.Status.INCOMPLETE,
    "incomplete_expired": Membership.Status.CANCELLED,
    "canceled": Membership.Status.CANCELLED,
}


def handle_subscription_changed(sub, now=None):
    membership = _membership_for(sub["id"], sub.get("metadata") or {})
    if not membership:
        return None
    status = SUB_STATUS.get(sub.get("status"), membership.status)
    membership.status = status
    if status == Membership.Status.CANCELLED and not membership.cancelled_at:
        membership.cancelled_at = now or timezone.now()
    membership.save(update_fields=["status", "cancelled_at"])
    return membership


def process_event(event, now=None):
    """Dispatch one Stripe webhook event (as a dict). Returns False if it was
    already processed."""
    with transaction.atomic():
        if StripeEvent.objects.filter(event_id=event["id"]).exists():
            return False
        obj = event["data"]["object"]
        etype = event["type"]
        if etype == "checkout.session.completed":
            fulfill_checkout_session(obj, now)
        elif etype in ("invoice.paid", "invoice.payment_succeeded"):
            handle_invoice_paid(obj, now=now)
        elif etype == "invoice.payment_failed":
            handle_invoice_failed(obj, now)
        elif etype in ("customer.subscription.updated", "customer.subscription.deleted", "customer.subscription.created"):
            handle_subscription_changed(obj, now)
        StripeEvent.objects.create(event_id=event["id"], type=etype)
    return True
