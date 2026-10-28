"""Admin-only screens: classes, products, settings, payroll report, client
account actions, and the Stripe webhook endpoint."""
import csv
import json

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from studio.forms import AdjustCreditsForm, ClassTemplateForm, GrantProductForm, ReportForm, SettingsForm
from studio.models import ClassTemplate, ClientPackage, ClientProfile, Product, StudioSettings
from studio.permissions import admin_required
from studio.services import payments
from studio.services.booking import generate_classes
from studio.services.credits import BookingError, adjust_credits, grant_package
from studio.services.reports import REPORT_COLUMNS, trainer_report


@admin_required
def classes(request):
    form = ClassTemplateForm(request.POST or None)
    if request.method == "POST":
        if "generate" in request.POST:
            created, skipped = generate_classes(timezone.localdate(), weeks=int(request.POST.get("weeks", 3)))
            msg = f"Created {len(created)} classes."
            if skipped:
                msg += f" Skipped {len(skipped)} that clash with the trainer's calendar or time off."
            messages.success(request, msg)
            return redirect("office_classes")
        if form.is_valid():
            form.save()
            created, _ = generate_classes(timezone.localdate())
            messages.success(request, f"Class added and {len(created)} upcoming dates scheduled.")
            return redirect("office_classes")
    return render(
        request,
        "studio/office/classes.html",
        {"templates": ClassTemplate.objects.select_related("trainer__user"), "form": form},
    )


@admin_required
@require_POST
def class_toggle(request, pk):
    tpl = get_object_or_404(ClassTemplate, pk=pk)
    tpl.active = not tpl.active
    tpl.save(update_fields=["active"])
    messages.success(
        request,
        f"{tpl.title} {'re-activated' if tpl.active else 'paused'}. Already-scheduled dates are unchanged; cancel them from the calendar if needed.",
    )
    return redirect("office_classes")


@admin_required
def products(request):
    return render(
        request,
        "studio/office/products.html",
        {"products": Product.objects.all(), "stripe_enabled": payments.stripe_enabled()},
    )


@admin_required
def studio_settings(request):
    form = SettingsForm(request.POST or None, instance=StudioSettings.load())
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Settings saved.")
        return redirect("office_settings")
    return render(
        request,
        "studio/staff/form_page.html",
        {"form": form, "title": "Studio settings", "submit": "Save", "tz": settings.TIME_ZONE},
    )


@admin_required
def report(request):
    start, end = ReportForm.default_range()
    form = ReportForm(request.GET or None, initial={"start": start, "end": end})
    if request.GET and form.is_valid():
        start, end = form.cleaned_data["start"], form.cleaned_data["end"]
    rows = trainer_report(start, end)
    totals = {k: sum(r[k] for r in rows) for k, _ in REPORT_COLUMNS if k != "trainer"}
    if request.GET.get("format") == "csv":
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="trainer-report-{start}-to-{end}.csv"'
        w = csv.writer(resp)
        w.writerow([label for _, label in REPORT_COLUMNS])
        for r in rows:
            w.writerow([r["trainer"].name if k == "trainer" else r[k] for k, _ in REPORT_COLUMNS])
        return resp
    return render(
        request,
        "studio/office/report.html",
        {"form": form, "rows": rows, "columns": REPORT_COLUMNS, "totals": totals, "start": start, "end": end},
    )


@admin_required
@require_POST
def client_adjust(request, client_id):
    client = get_object_or_404(ClientProfile, pk=client_id)
    form = AdjustCreditsForm(request.POST, client=client)
    if form.is_valid():
        try:
            with transaction.atomic():
                adjust_credits(form.cleaned_data["package"], form.cleaned_data["delta"], form.cleaned_data["note"], request.user)
            messages.success(request, "Balance adjusted.")
        except BookingError as exc:
            messages.error(request, str(exc))
    else:
        messages.error(request, "Fix the adjustment form: " + "; ".join(e for errs in form.errors.values() for e in errs))
    return redirect("client_detail", client.pk)


@admin_required
@require_POST
def client_grant(request, client_id):
    """Record an in-studio (cash/card terminal) sale or a comp."""
    client = get_object_or_404(ClientProfile, pk=client_id)
    form = GrantProductForm(request.POST)
    if form.is_valid():
        with transaction.atomic():
            grant_package(
                client,
                form.cleaned_data["product"],
                source=ClientPackage.Source.MANUAL,
                by_user=request.user,
                note=form.cleaned_data["note"],
            )
        messages.success(request, f"Added {form.cleaned_data['product']} to {client}.")
    return redirect("client_detail", client.pk)


@admin_required
@require_POST
def client_flag(request, client_id):
    client = get_object_or_404(ClientProfile, pk=client_id)
    if request.POST.get("action") == "clear":
        payments.clear_flag(client)
        messages.success(request, f"Payment flag cleared for {client}. They can book again.")
    else:
        payments.flag_client(client, request.POST.get("reason") or "Flagged by admin")
        messages.warning(request, f"{client} is flagged and can't make new bookings.")
    return redirect("client_detail", client.pk)


@csrf_exempt
@require_POST
def stripe_webhook(request):
    secret = settings.STRIPE_WEBHOOK_SECRET
    if not secret:
        return HttpResponseBadRequest("STRIPE_WEBHOOK_SECRET is not configured.")
    import stripe

    try:
        stripe.Webhook.construct_event(request.body, request.headers.get("Stripe-Signature", ""), secret)
    except (ValueError, stripe.SignatureVerificationError):
        return HttpResponseBadRequest("Invalid signature")
    payments.process_event(json.loads(request.body))
    return HttpResponse("ok")
