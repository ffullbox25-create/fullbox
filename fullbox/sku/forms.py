from django import forms

from django.forms import BaseInlineFormSet, inlineformset_factory

from .models import SKU, SKUBarcode


class SKUForm(forms.ModelForm):
    def clean(self):
        cleaned = super().clean()
        sku_code = (cleaned.get("sku_code") or "").strip()
        agency = cleaned.get("agency")

        if not sku_code:
            self.add_error("sku_code", "Артикул обязателен.")

        if sku_code:
            qs = SKU.objects.filter(deleted=False, sku_code=sku_code)
            if agency is None:
                qs = qs.filter(agency__isnull=True)
            else:
                qs = qs.filter(agency=agency)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                self.add_error("sku_code", "Такой артикул уже есть у этого клиента.")

        numeric_fields = {
            "weight_kg": "Вес",
            "weight_net_kg": "Вес нетто",
            "weight_gross_kg": "Вес брутто",
            "volume": "Объем",
            "length_mm": "Длина",
            "width_mm": "Ширина",
            "height_mm": "Высота",
        }
        for field, label in numeric_fields.items():
            value = cleaned.get(field)
            if value is not None and value < 0:
                self.add_error(field, f"{label} не может быть отрицательным.")

        cleaned["sku_code"] = sku_code
        return cleaned

    class Meta:
        model = SKU
        fields = [
            "sku_code",
            "name",
            "brand",
            "agency",
            "market",
            "color",
            "color_ref",
            "size",
            "name_print",
            "img",
            "img_comment",
            "gender",
            "season",
            "additional_name",
            "composition",
            "made_in",
            "cr_product_date",
            "end_product_date",
            "sign_akciz",
            "tovar_category",
            "use_nds",
            "vid_tovar",
            "type_tovar",
            "weight_kg",
            "weight_net_kg",
            "weight_gross_kg",
            "volume",
            "length_mm",
            "width_mm",
            "height_mm",
            "honest_sign",
            "description",
            "source",
            "source_reference",
        ]
        widgets = {
            "cr_product_date": forms.DateInput(attrs={"type": "date"}),
            "end_product_date": forms.DateInput(attrs={"type": "date"}),
            "description": forms.Textarea(attrs={"rows": 3}),
            "img_comment": forms.Textarea(attrs={"rows": 2}),
        }


class SKUBarcodeForm(forms.ModelForm):
    def clean_value(self):
        return (self.cleaned_data.get("value") or "").strip()

    def clean_size(self):
        return (self.cleaned_data.get("size") or "").strip() or None

    class Meta:
        model = SKUBarcode
        fields = ["value", "size", "is_primary"]
        widgets = {
            "value": forms.TextInput(attrs={"placeholder": "Штрихкод"}),
            "size": forms.TextInput(attrs={"placeholder": "Размер"}),
            "is_primary": forms.HiddenInput(),
        }


class BaseSKUBarcodeFormSet(BaseInlineFormSet):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound and not self.instance.pk and self.forms:
            self.forms[0].initial["is_primary"] = True

    def clean(self):
        super().clean()
        if any(self.errors):
            return

        active_forms = []
        primary_forms = []
        active_values = {}
        for form in self.forms:
            cleaned = getattr(form, "cleaned_data", None) or {}
            if cleaned.get("DELETE") or not cleaned.get("value"):
                continue
            active_forms.append(form)
            value = str(cleaned.get("value") or "").strip()
            if value in active_values:
                raise forms.ValidationError(
                    f"Штрихкод {value} указан несколько раз у одного клиента."
                )
            active_values[value] = form
            if cleaned.get("is_primary"):
                primary_forms.append(form)

        if not active_forms:
            raise forms.ValidationError("Добавьте хотя бы один штрихкод.")

        if not primary_forms and len(active_forms) == 1:
            only_form = active_forms[0]
            only_form.cleaned_data["is_primary"] = True
            only_form.instance.is_primary = True
            primary_forms = [only_form]

        if len(primary_forms) != 1:
            raise forms.ValidationError("Выберите один основной штрихкод.")

        agency_id = getattr(self.instance, "agency_id", None)
        if agency_id and active_values:
            current_ids = {
                int(form.instance.pk)
                for form in active_forms
                if getattr(form.instance, "pk", None)
            }
            conflicts = SKUBarcode.objects.filter(
                agency_id=agency_id,
                value__in=active_values,
            )
            if current_ids:
                conflicts = conflicts.exclude(pk__in=current_ids)
            conflict = conflicts.select_related("sku").order_by("value").first()
            if conflict:
                raise forms.ValidationError(
                    f"Штрихкод {conflict.value} уже используется у артикула "
                    f"{conflict.sku.sku_code} этого клиента."
                )


SKUBarcodeFormSet = inlineformset_factory(
    SKU,
    SKUBarcode,
    form=SKUBarcodeForm,
    formset=BaseSKUBarcodeFormSet,
    extra=1,
    can_delete=True,
)
