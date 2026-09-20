from django import forms
from sklad.models import WarehouseLocation
from sku.models import Agency, SKU

from .models import Inventory, InventoryLocation, inventory_location_label


class LocationChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, location: WarehouseLocation) -> str:
        return f"{inventory_location_label(location)} · {location.get_zone_kind_display()}"


class SkuChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, sku: SKU) -> str:
        agency = getattr(sku, "agency", None)
        agency_name = str(getattr(agency, "short_name", "") or getattr(agency, "agn_name", "") or "-").strip()
        size = f" · {sku.size}" if str(sku.size or "").strip() else ""
        return f"{sku.sku_code}{size} · {sku.name} · {agency_name}"


class InventoryCreateForm(forms.ModelForm):
    locations = LocationChoiceField(
        queryset=WarehouseLocation.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Места",
    )
    sku = SkuChoiceField(
        queryset=SKU.objects.none(),
        required=False,
        label="Товар",
        widget=forms.HiddenInput,
    )

    class Meta:
        model = Inventory
        fields = ("inventory_type", "agency", "sku", "comment")
        labels = {
            "inventory_type": "Тип инвентаризации",
            "agency": "Партнер",
            "comment": "Комментарий",
        }
        widgets = {"comment": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["inventory_type"].choices = [
            choice
            for choice in self.fields["inventory_type"].choices
            if choice[0] != Inventory.TYPE_FULL
        ]
        self.fields["agency"].queryset = Agency.objects.filter(archived=False).order_by("agn_name")
        self.fields["agency"].required = False
        self.fields["sku"].queryset = (
            SKU.objects.filter(deleted=False)
            .select_related("agency")
            .order_by("sku_code", "size", "id")
        )
        self.fields["locations"].queryset = WarehouseLocation.objects.filter(is_active=True).order_by(
            "zone_code", "row_no", "section_no", "tier_no", "cell_no", "id"
        )
        self.selected_sku_label = ""
        raw_sku_id = self.data.get(self.add_prefix("sku")) if self.is_bound else self.initial.get("sku")
        if isinstance(raw_sku_id, SKU):
            selected_sku = raw_sku_id
        else:
            try:
                selected_sku = self.fields["sku"].queryset.filter(pk=int(raw_sku_id or 0)).first()
            except (TypeError, ValueError):
                selected_sku = None
        if selected_sku is not None:
            self.selected_sku_label = self.fields["sku"].label_from_instance(selected_sku)

    def clean(self):
        cleaned = super().clean()
        inventory_type = cleaned.get("inventory_type")
        agency = cleaned.get("agency")
        sku = cleaned.get("sku")
        locations = cleaned.get("locations")
        if inventory_type == Inventory.TYPE_PARTNER and agency is None:
            self.add_error("agency", "Выберите партнера.")
        if inventory_type == Inventory.TYPE_GOODS and sku is None:
            self.add_error("sku", "Выберите товар.")
        if inventory_type == Inventory.TYPE_PLACES and not locations:
            self.add_error("locations", "Выберите хотя бы одно складское место.")
        if inventory_type != Inventory.TYPE_PARTNER:
            cleaned["agency"] = None
        if inventory_type != Inventory.TYPE_GOODS:
            cleaned["sku"] = None
        return cleaned

    def save(self, commit=True):
        locations = list(self.cleaned_data.get("locations") or [])
        inventory = super().save(commit=commit)
        if commit and locations:
            InventoryLocation.objects.bulk_create(
                [InventoryLocation(inventory=inventory, location=location) for location in locations],
                ignore_conflicts=True,
            )
        return inventory
