from django import forms
from django.utils.text import slugify

from billing.models import BillingService
from sku.forms import SKUForm
from sku.models import MarketplaceBinding, SKUBarcode

from .models import GoodsDefaultService, GoodsExtraFieldDefinition, GoodsFile, GoodsProfile


class GoodsSKUForm(SKUForm):
    class Meta(SKUForm.Meta):
        pass


class GoodsProfileForm(forms.ModelForm):
    class Meta:
        model = GoodsProfile
        fields = [
            "internal_notes",
            "fbs_shelf_life_required",
            "receiving_task_template",
            "shipping_task_template",
        ]
        widgets = {
            "internal_notes": forms.Textarea(attrs={"rows": 4}),
            "receiving_task_template": forms.Textarea(attrs={"rows": 5}),
            "shipping_task_template": forms.Textarea(attrs={"rows": 5}),
        }


class GoodsBarcodeForm(forms.ModelForm):
    class Meta:
        model = SKUBarcode
        fields = ["value", "size", "is_primary"]

    def clean_value(self):
        return (self.cleaned_data.get("value") or "").strip()


class GoodsMarketplaceBindingForm(forms.ModelForm):
    class Meta:
        model = MarketplaceBinding
        fields = ["marketplace", "external_id", "sync_mode"]

    def clean_marketplace(self):
        return (self.cleaned_data.get("marketplace") or "").strip()

    def clean_external_id(self):
        return (self.cleaned_data.get("external_id") or "").strip()


class GoodsFileForm(forms.ModelForm):
    class Meta:
        model = GoodsFile
        fields = ["file"]


class GoodsDefaultServiceForm(forms.ModelForm):
    class Meta:
        model = GoodsDefaultService
        fields = ["stage", "service", "unit_price", "quantity", "auto_add"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["service"].queryset = BillingService.objects.filter(is_active=True).order_by(
            "sort_order", "name"
        )
        self.fields["unit_price"].required = False

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("unit_price") is None and cleaned.get("service"):
            cleaned["unit_price"] = cleaned["service"].default_price
        return cleaned


class GoodsExtraDefinitionForm(forms.ModelForm):
    class Meta:
        model = GoodsExtraFieldDefinition
        fields = ["name", "field_type"]

    def save(self, commit=True):
        obj = super().save(commit=False)
        base = slugify(obj.name, allow_unicode=False) or "field"
        candidate = base
        index = 2
        while GoodsExtraFieldDefinition.objects.filter(slug=candidate).exists():
            candidate = f"{base}-{index}"
            index += 1
        obj.slug = candidate
        if commit:
            obj.save()
        return obj
