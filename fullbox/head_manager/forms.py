import re

from django import forms

from agent.models import DeviceAgent
from fbs.models import FbsPickingCart, FbsWorkstation

from .models import Carrier, OwnCompany


def _normalize_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


class _ReferenceBaseForm(forms.ModelForm):
    textareas = ("address", "postal_address", "bank_address", "comment")

    def clean(self):
        cleaned = super().clean()
        for key, value in list(cleaned.items()):
            if isinstance(value, str):
                cleaned[key] = _normalize_text(value)
        return cleaned

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name in self.textareas:
            field = self.fields.get(field_name)
            if field:
                field.widget = forms.Textarea(attrs={"rows": 3})


class OwnCompanyForm(_ReferenceBaseForm):
    class Meta:
        model = OwnCompany
        fields = [
            "name",
            "inn",
            "kpp",
            "ogrn",
            "address",
            "postal_address",
            "phone",
            "email",
            "director_name",
            "director_basis",
            "bank_name",
            "bank_bik",
            "settlement_account",
            "correspondent_account",
            "bank_address",
            "edo_operator",
            "edo_id",
            "is_default",
            "is_active",
            "comment",
        ]


class CarrierForm(_ReferenceBaseForm):
    class Meta:
        model = Carrier
        fields = [
            "name",
            "inn",
            "kpp",
            "ogrn",
            "address",
            "postal_address",
            "phone",
            "email",
            "contact_person",
            "bank_name",
            "bank_bik",
            "settlement_account",
            "correspondent_account",
            "bank_address",
            "is_active",
            "comment",
        ]


class FbsWorkstationBatchCreateForm(forms.Form):
    count = forms.IntegerField(label="Количество", min_value=1, max_value=50, initial=1)
    name_prefix = forms.CharField(label="Название", max_length=110, initial="Рабочее место")
    device_agent = forms.ModelChoiceField(
        label="Агент",
        queryset=DeviceAgent.objects.none(),
        required=False,
        empty_label="Не назначен",
    )
    printer_name = forms.CharField(label="Принтер", max_length=255, required=False)
    max_parallel_waves = forms.TypedChoiceField(
        label="Тележек у стола",
        choices=(
            (1, "1 тележка"),
            (2, "2 тележки"),
            (3, "3 тележки"),
            (4, "4 тележки"),
            (5, "5 тележек"),
            (6, "6 тележек"),
            (7, "7 тележек"),
        ),
        coerce=int,
        initial=7,
    )
    notes = forms.CharField(label="Примечание", required=False, widget=forms.Textarea)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["device_agent"].queryset = DeviceAgent.objects.order_by("name", "host", "agent_id")

    def clean(self):
        cleaned = super().clean()
        for field_name in ("name_prefix", "printer_name", "notes"):
            if field_name in cleaned:
                cleaned[field_name] = _normalize_text(cleaned[field_name])
        return cleaned


class FbsPickingCartBatchCreateForm(forms.Form):
    count = forms.IntegerField(label="Количество", min_value=1, max_value=100, initial=1)
    name_prefix = forms.CharField(label="Название", max_length=110, initial="Тележка")
    notes = forms.CharField(label="Примечание", required=False, widget=forms.Textarea)

    def clean(self):
        cleaned = super().clean()
        for field_name in ("name_prefix", "notes"):
            if field_name in cleaned:
                cleaned[field_name] = _normalize_text(cleaned[field_name])
        return cleaned


class FbsWorkstationUpdateForm(forms.ModelForm):
    class Meta:
        model = FbsWorkstation
        fields = [
            "name",
            "device_agent",
            "printer_name",
            "max_parallel_waves",
            "is_active",
            "notes",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["device_agent"].queryset = DeviceAgent.objects.order_by("name", "host", "agent_id")
        self.fields["device_agent"].required = False
        self.fields["max_parallel_waves"].widget = forms.Select(
            choices=(
                (1, "1 тележка"),
                (2, "2 тележки"),
                (3, "3 тележки"),
                (4, "4 тележки"),
                (5, "5 тележек"),
                (6, "6 тележек"),
                (7, "7 тележек"),
            )
        )

    def clean(self):
        cleaned = super().clean()
        for field_name in ("name", "printer_name", "notes"):
            if field_name in cleaned:
                cleaned[field_name] = _normalize_text(cleaned[field_name])
        return cleaned


class FbsPickingCartUpdateForm(forms.ModelForm):
    class Meta:
        model = FbsPickingCart
        fields = ["name", "is_active", "notes"]

    def clean(self):
        cleaned = super().clean()
        for field_name in ("name", "notes"):
            if field_name in cleaned:
                cleaned[field_name] = _normalize_text(cleaned[field_name])
        return cleaned
