from datetime import timedelta

from django import forms
from django.utils import timezone

from .models import Availability, ClassTemplate, ClientPackage, Product, StudioSettings, TimeOff, TrainerProfile


class DateInput(forms.DateInput):
    input_type = "date"


class TimeInput(forms.TimeInput):
    input_type = "time"


class DateTimeInput(forms.DateTimeInput):
    input_type = "datetime-local"

    def format_value(self, value):
        if hasattr(value, "strftime"):
            return timezone.localtime(value).strftime("%Y-%m-%dT%H:%M") if timezone.is_aware(value) else value.strftime("%Y-%m-%dT%H:%M")
        return value


class AvailabilityForm(forms.ModelForm):
    class Meta:
        model = Availability
        fields = ["weekday", "start_time", "end_time"]
        widgets = {"start_time": TimeInput(), "end_time": TimeInput()}

    def clean(self):
        data = super().clean()
        if data.get("start_time") and data.get("end_time") and data["end_time"] <= data["start_time"]:
            raise forms.ValidationError("End must be after start.")
        return data


class TimeOffForm(forms.ModelForm):
    class Meta:
        model = TimeOff
        fields = ["start", "end", "reason"]
        widgets = {"start": DateTimeInput(), "end": DateTimeInput()}

    def clean(self):
        data = super().clean()
        if data.get("start") and data.get("end") and data["end"] <= data["start"]:
            raise forms.ValidationError("End must be after start.")
        return data


class SettingsForm(forms.ModelForm):
    class Meta:
        model = StudioSettings
        fields = [
            "studio_name",
            "late_cancel_hours",
            "waitlist_cutoff_hours",
            "min_booking_notice_hours",
            "booking_window_days",
        ]


class ClassTemplateForm(forms.ModelForm):
    class Meta:
        model = ClassTemplate
        fields = ["title", "trainer", "weekday", "start_time", "duration_minutes", "capacity"]
        widgets = {"start_time": TimeInput()}


class GroupSessionForm(forms.Form):
    trainer = forms.ModelChoiceField(queryset=TrainerProfile.objects.filter(active=True))
    title = forms.CharField(initial="Small-group session", max_length=100)
    start = forms.DateTimeField(widget=DateTimeInput())
    duration_minutes = forms.TypedChoiceField(choices=[(30, "30 min"), (60, "60 min")], coerce=int, initial=60)
    capacity = forms.IntegerField(min_value=2, max_value=6, initial=3)


class AdjustCreditsForm(forms.Form):
    package = forms.ModelChoiceField(queryset=ClientPackage.objects.none())
    delta = forms.IntegerField(help_text="Positive to add, negative to remove.")
    note = forms.CharField(max_length=255)

    def __init__(self, *args, client=None, **kwargs):
        super().__init__(*args, **kwargs)
        if client:
            self.fields["package"].queryset = client.packages.all()

    def clean_delta(self):
        if self.cleaned_data["delta"] == 0:
            raise forms.ValidationError("Enter a non-zero number.")
        return self.cleaned_data["delta"]


class GrantProductForm(forms.Form):
    product = forms.ModelChoiceField(queryset=Product.objects.filter(active=True).exclude(kind=Product.Kind.MEMBERSHIP))
    note = forms.CharField(max_length=255, initial="Paid in studio")


class ReportForm(forms.Form):
    start = forms.DateField(widget=DateInput())
    end = forms.DateField(widget=DateInput())

    @classmethod
    def default_range(cls):
        today = timezone.localdate()
        first = today.replace(day=1) if today.day > 1 else (today - timedelta(days=1)).replace(day=1)
        return first, today

    def clean(self):
        data = super().clean()
        if data.get("start") and data.get("end") and data["end"] < data["start"]:
            raise forms.ValidationError("End date is before start date.")
        return data
