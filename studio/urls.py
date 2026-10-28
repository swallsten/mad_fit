from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
from django.urls import path

from .views import client, office, staff


@login_required
def home(request):
    if request.user.is_client:
        return redirect("client_dashboard")
    return redirect("schedule")


urlpatterns = [
    path("", home, name="home"),
    # client booking page
    path("me/", client.dashboard, name="client_dashboard"),
    path("me/book/", client.book, name="client_book"),
    path("me/book/confirm/", client.book_confirm, name="client_book_confirm"),
    path("me/classes/", client.classes, name="client_classes"),
    path("me/classes/<int:event_id>/join/", client.class_join, name="client_class_join"),
    path("me/buy/", client.buy, name="client_buy"),
    path("me/buy/<int:product_id>/", client.buy_product, name="client_buy_product"),
    path("me/checkout/success/", client.checkout_success, name="checkout_success"),
    path("me/billing/", client.billing_portal, name="client_billing"),
    path("me/history/", client.history, name="client_history"),
    path("bookings/<int:booking_id>/cancel/", staff.cancel_view, name="booking_cancel"),
    # trainers + admin
    path("schedule/", staff.schedule, name="schedule"),
    path("events/<int:event_id>/", staff.event_detail, name="event_detail"),
    path("events/<int:event_id>/add/", staff.event_add_client, name="event_add_client"),
    path("events/<int:event_id>/cancel/", staff.event_cancel, name="event_cancel"),
    path("bookings/<int:booking_id>/attendance/", staff.booking_attendance, name="booking_attendance"),
    path("clients/", staff.clients, name="clients"),
    path("clients/<int:client_id>/", staff.client_detail, name="client_detail"),
    path("book/", staff.staff_book, name="staff_book"),
    path("group/new/", staff.group_new, name="group_new"),
    path("availability/", staff.availability, name="availability"),
    path("availability/<int:pk>/delete/", staff.availability_delete, name="availability_delete"),
    path("timeoff/<int:pk>/delete/", staff.timeoff_delete, name="timeoff_delete"),
    # admin only
    path("office/classes/", office.classes, name="office_classes"),
    path("office/classes/<int:pk>/toggle/", office.class_toggle, name="office_class_toggle"),
    path("office/products/", office.products, name="office_products"),
    path("office/settings/", office.studio_settings, name="office_settings"),
    path("office/report/", office.report, name="office_report"),
    path("clients/<int:client_id>/adjust/", office.client_adjust, name="client_adjust"),
    path("clients/<int:client_id>/grant/", office.client_grant, name="client_grant"),
    path("clients/<int:client_id>/flag/", office.client_flag, name="client_flag"),
    path("stripe/webhook/", office.stripe_webhook, name="stripe_webhook"),
]
