from __future__ import annotations

import re

from django import forms

from head_manager.models import Carrier, OwnCompany


def _normalize_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


class AccountantOwnCompanyForm(forms.ModelForm):
    """Реквизиты юрлиц FullBox + режим НДС для биллинга."""

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
            "tax_mode",
            "vat_rate",
            "edo_operator",
            "edo_id",
            "is_default",
            "is_active",
            "comment",
        ]
        widgets = {
            "address": forms.Textarea(attrs={"rows": 2, "class": "field"}),
            "postal_address": forms.Textarea(attrs={"rows": 2, "class": "field"}),
            "bank_address": forms.Textarea(attrs={"rows": 2, "class": "field"}),
            "comment": forms.Textarea(attrs={"rows": 3, "class": "field"}),
            "tax_mode": forms.Select(attrs={"class": "field"}),
            "vat_rate": forms.Select(
                attrs={"class": "field"},
                choices=[("0", "0%"), ("5", "5%"), ("20", "20%")],
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name not in self.Meta.widgets:
                field.widget.attrs.setdefault("class", "field")
        self.fields["tax_mode"].label = "Формат НДС"
        self.fields["vat_rate"].label = "Ставка НДС"
        self.fields["name"].label = "Полное название"
        self.fields["is_default"].label = "Компания по умолчанию"
        # Select with fixed choices for vat_rate
        self.fields["vat_rate"].widget = forms.Select(
            attrs={"class": "field"},
            choices=[("0", "0% — без НДС"), ("5", "5%"), ("20", "20%")],
        )

    def clean(self):
        cleaned = super().clean()
        for key, value in list(cleaned.items()):
            if isinstance(value, str) and key not in {"comment", "address", "postal_address", "bank_address"}:
                cleaned[key] = _normalize_text(value)
        tax_mode = cleaned.get("tax_mode")
        vat_rate = str(cleaned.get("vat_rate") or "").strip()
        if tax_mode == OwnCompany.TAX_MODE_NO_VAT:
            cleaned["vat_rate"] = "0"
        elif tax_mode == OwnCompany.TAX_MODE_VAT and vat_rate in {"", "0"}:
            cleaned["vat_rate"] = "5"
        return cleaned


class AccountantCarrierForm(forms.ModelForm):
    """Справочник перевозчиков для логистики и документов."""

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
        widgets = {
            "address": forms.Textarea(attrs={"rows": 2, "class": "field"}),
            "postal_address": forms.Textarea(attrs={"rows": 2, "class": "field"}),
            "bank_address": forms.Textarea(attrs={"rows": 2, "class": "field"}),
            "comment": forms.Textarea(attrs={"rows": 3, "class": "field"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name not in self.Meta.widgets:
                field.widget.attrs.setdefault("class", "field")
        self.fields["name"].label = "Полное название"
        self.fields["contact_person"].label = "Контактное лицо"
        self.fields["is_active"].label = "Активен (показывается в рейсах логистики)"

    def clean(self):
        cleaned = super().clean()
        for key, value in list(cleaned.items()):
            if isinstance(value, str) and key not in {"comment", "address", "postal_address", "bank_address"}:
                cleaned[key] = _normalize_text(value)
        if not cleaned.get("name"):
            self.add_error("name", "Укажите название перевозчика.")
        return cleaned
