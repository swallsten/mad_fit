from django.conf import settings

from .models import StudioSettings

# Seeded accounts offered as one-click logins when DEMO_MODE is on.
DEMO_ACCOUNTS = [
    ("Owner / admin", "Jordan Reyes", "owner@studio.test", "All seven calendars, clients, payroll report, settings"),
    ("Trainer", "Alex Kim", "alex@studio.test", "Alex's own schedule, clients and availability"),
    ("Client", "Emily Carter", "emily.carter@example.com", "Has credits and a membership; on a class waitlist"),
    ("Client", "Olivia Brooks", "olivia.brooks@example.com", "Booked in a full class — cancel it to promote Emily"),
    ("Client", "Dana Whitfield", "dana.whitfield@example.com", "Package has expired, so booking is refused"),
    ("Client", "Marcus Bell", "marcus.bell@example.com", "Failed payment — new bookings are blocked"),
]
DEMO_PASSWORD = "demo1234"


def studio_context(request):
    ctx = {"studio": StudioSettings.load(), "demo_mode": settings.DEMO_MODE}
    if settings.DEMO_MODE:
        ctx.update(demo_accounts=DEMO_ACCOUNTS, demo_password=DEMO_PASSWORD)
    return ctx
