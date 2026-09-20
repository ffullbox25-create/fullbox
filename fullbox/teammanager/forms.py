from __future__ import annotations

from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Q

from head_manager.models import Carrier
from logistics.models import CarrierVehicle
from logistics.services import _normalize_driver_phone, _normalize_vehicle_number


class CarrierVehicleForm(forms.ModelForm):
    class Meta:
        model = CarrierVehicle
        fields = (
            "carrier",
            "vehicle_name",
            "vehicle_number",
            "driver_name",
            "driver_phone",
            "max_weight_kg",
            "max_volume_m3",
            "max_pallets",
            "is_default",
            "is_active",
            "comment",
        )
        widgets = {
            "comment": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        carrier_filter = Q(is_active=True)
        if self.instance and self.instance.carrier_id:
            carrier_filter |= Q(pk=self.instance.carrier_id)
        self.fields["carrier"].queryset = Carrier.objects.filter(carrier_filter).order_by(
            "short_name", "name", "id"
        )
        for field in self.fields.values():
            if not isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "field")
        self.fields["vehicle_number"].widget.attrs.update(
            {"placeholder": "А123ВС777", "autocomplete": "off"}
        )
        self.fields["driver_name"].widget.attrs.setdefault("placeholder", "Иванов Иван Иванович")
        self.fields["driver_phone"].widget.attrs.update(
            {"placeholder": "+7 900 000-00-00", "inputmode": "tel"}
        )
        if self.instance and self.instance.pk:
            self.fields["carrier"].disabled = True

    def clean_vehicle_number(self):
        try:
            return _normalize_vehicle_number(self.cleaned_data.get("vehicle_number"))
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    def clean_driver_phone(self):
        try:
            return _normalize_driver_phone(self.cleaned_data.get("driver_phone"))
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("is_active"):
            cleaned["is_default"] = False
        return cleaned

    def _get_validation_exclusions(self):
        exclusions = super()._get_validation_exclusions()
        if self.cleaned_data.get("is_default"):
            exclusions.add("is_default")
        return exclusions
