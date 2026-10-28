"""Raw database editing for the owner (at /django-admin/). Day-to-day work
happens in the studio screens; this is for fixing data and editing products."""
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from . import models


@admin.register(models.User)
class StudioUserAdmin(UserAdmin):
    list_display = ["username", "first_name", "last_name", "email", "role"]
    list_filter = ["role", "is_active"]
    fieldsets = UserAdmin.fieldsets + (("Studio", {"fields": ("role", "phone")}),)


@admin.register(models.Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ["name", "kind", "credit_type", "credits", "price_cents", "expires_days", "active"]
    list_editable = ["active"]


@admin.register(models.ClientPackage)
class ClientPackageAdmin(admin.ModelAdmin):
    list_display = ["client", "name", "credits_remaining", "credits_total", "expires_at", "source"]
    list_filter = ["credit_type", "source"]
    search_fields = ["client__user__first_name", "client__user__last_name"]


@admin.register(models.Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ["client", "event", "status", "waitlist_position"]
    list_filter = ["status"]


@admin.register(models.Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ["title", "trainer", "start", "kind", "capacity", "status"]
    list_filter = ["kind", "status", "trainer"]
    date_hierarchy = "start"


for m in [
    models.StudioSettings,
    models.TrainerProfile,
    models.ClientProfile,
    models.Availability,
    models.TimeOff,
    models.Membership,
    models.ClassTemplate,
    models.CreditTransaction,
    models.StripeEvent,
    models.Notification,
]:
    admin.site.register(m)
