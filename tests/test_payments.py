"""Stripe fulfilment and failed-payment handling, using event payloads
shaped like Stripe's (no network calls)."""
import pytest

from studio.models import ClientPackage, ClientProfile, CreditType, Membership, Product
from studio.services import payments
from studio.services.booking import BookingError, book_session

from .conftest import at


@pytest.fixture
def unlimited(db):
    return Product.objects.create(
        name="Unlimited classes", kind=Product.Kind.MEMBERSHIP, credit_type=CreditType.CLASS, credits=None, price_cents=14900
    )


@pytest.fixture
def monthly4(db):
    return Product.objects.create(
        name="4 sessions / month", kind=Product.Kind.MEMBERSHIP, credit_type=CreditType.PT60, credits=4,
        price_cents=34000, expires_days=90,
    )


def meta(client, product):
    return {"client_id": str(client.pk), "product_id": str(product.pk)}


def checkout_event(eid, session):
    return {"id": eid, "type": "checkout.session.completed", "data": {"object": session}}


def invoice(iid, sub_id, client, product, status="paid", new_style=True):
    inv = {
        "id": iid,
        "status": status,
        "amount_due": product.price_cents,
        "customer": "cus_test",
        "lines": {"data": [{"period": {"start": 1793000000, "end": 1795600000}, "metadata": {}}]},
    }
    if new_style:  # API 2025-03-31+
        inv["parent"] = {"subscription_details": {"subscription": sub_id, "metadata": meta(client, product)}}
    else:
        inv["subscription"] = sub_id
        inv["lines"]["data"][0]["metadata"] = meta(client, product)
    return inv


def test_package_checkout_grants_credits_once(make_client, pack60):
    client = make_client()
    session = {"id": "cs_1", "mode": "payment", "payment_status": "paid", "customer": "cus_1", "metadata": meta(client, pack60)}
    assert payments.process_event(checkout_event("evt_1", session)) is True
    assert payments.process_event(checkout_event("evt_1", session)) is False  # webhook replay
    payments.fulfill_checkout_session(session)  # success page after the webhook
    pkgs = ClientPackage.objects.filter(client=client)
    assert pkgs.count() == 1 and pkgs.get().credits_remaining == 10
    client.refresh_from_db()
    assert client.stripe_customer_id == "cus_1"


def test_unpaid_checkout_grants_nothing(make_client, pack60):
    client = make_client()
    payments.fulfill_checkout_session({"id": "cs_2", "mode": "payment", "payment_status": "unpaid", "metadata": meta(client, pack60)})
    assert not ClientPackage.objects.exists()


@pytest.mark.parametrize("new_style", [True, False])
def test_monthly_session_membership_adds_credits_each_paid_invoice(make_client, monthly4, new_style):
    client = make_client()
    payments.fulfill_checkout_session(
        {"id": "cs_3", "mode": "subscription", "subscription": "sub_1", "metadata": meta(client, monthly4)}
    )
    m = Membership.objects.get(stripe_subscription_id="sub_1")
    assert m.status == Membership.Status.INCOMPLETE

    for n, iid in enumerate(["in_1", "in_1", "in_2"]):
        payments.process_event({"id": f"evt_inv_{n}", "type": "invoice.paid", "data": {"object": invoice(iid, "sub_1", client, monthly4, new_style=new_style)}})
    m.refresh_from_db()
    assert m.status == Membership.Status.ACTIVE
    # two distinct invoices -> two grants of 4, and credits roll over (10 is 10)
    assert sum(ClientPackage.objects.filter(client=client).values_list("credits_remaining", flat=True)) == 8


def test_invoice_before_checkout_event_still_works(make_client, unlimited):
    client = make_client()
    payments.handle_invoice_paid(invoice("in_9", "sub_9", client, unlimited))
    m = Membership.objects.get(stripe_subscription_id="sub_9")
    assert m.client == client and m.status == Membership.Status.ACTIVE
    assert not ClientPackage.objects.exists()  # unlimited: no credits


def test_failed_payment_flags_client_and_blocks_booking(make_client, make_trainer, unlimited, pack60, give, now):
    client = make_client()
    give(client, pack60)
    payments.handle_invoice_paid(invoice("in_1", "sub_1", client, unlimited))
    payments.process_event(
        {"id": "evt_f", "type": "invoice.payment_failed", "data": {"object": invoice("in_2", "sub_1", client, unlimited, status="open")}}
    )
    client.refresh_from_db()
    assert client.payment_status == ClientProfile.PaymentStatus.FAILED
    assert "failed" in client.payment_flag_reason
    assert Membership.objects.get().status == Membership.Status.PAST_DUE
    with pytest.raises(BookingError, match="payment"):
        book_session(client, make_trainer(), at(1, 10), 60, now=now)

    # card updated in the portal -> Stripe retries -> invoice.paid clears the flag
    payments.process_event({"id": "evt_ok", "type": "invoice.paid", "data": {"object": invoice("in_2", "sub_1", client, unlimited)}})
    client.refresh_from_db()
    assert client.payment_status == ClientProfile.PaymentStatus.OK
    book_session(client, make_trainer(), at(1, 10), 60, now=now)


def test_subscription_cancelled(make_client, unlimited):
    client = make_client()
    payments.handle_invoice_paid(invoice("in_1", "sub_1", client, unlimited))
    payments.process_event(
        {"id": "evt_d", "type": "customer.subscription.deleted", "data": {"object": {"id": "sub_1", "status": "canceled", "metadata": {}}}}
    )
    m = Membership.objects.get()
    assert m.status == Membership.Status.CANCELLED and m.cancelled_at


def test_intro_offer_once_per_client(make_client):
    intro = Product.objects.create(
        name="Intro", kind=Product.Kind.INTRO, credit_type=CreditType.PT60, credits=3, price_cents=9900, one_per_client=True
    )
    client = make_client()
    payments.check_can_buy(client, intro)
    payments.fulfill_checkout_session({"id": "cs_i", "mode": "payment", "payment_status": "paid", "metadata": meta(client, intro)})
    with pytest.raises(BookingError, match="once"):
        payments.check_can_buy(client, intro)


def test_live_keys_refused(settings):
    from django.core.exceptions import ImproperlyConfigured

    settings.STRIPE_SECRET_KEY = "sk_live_abc"
    assert payments.stripe_enabled() is False
    with pytest.raises(ImproperlyConfigured):
        payments._stripe()
