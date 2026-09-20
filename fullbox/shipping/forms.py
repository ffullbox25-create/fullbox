from __future__ import annotations

from datetime import timedelta

from django import forms
from django.utils import timezone

from sku.models import Agency
from sku.models import Market

from .models import ShippingOrder, ShippingOrderItem, ShippingTransportNote

WORKDAY_START_HOUR = 8
WORKDAY_END_HOUR = 19
NEXT_DAY_DEADLINE_HOUR = 11
WORKDAY_HOURS_ERROR = "Плановое время отгрузки должно быть в рабочем окне с 08:00 до 19:00."
NEXT_DAY_DEADLINE_ERROR = (
    "Заявки на маркетплейсы до 11:00 принимаются на отгрузку на завтра, "
    "с 11:00 — на послезавтра."
)


def _marketplace_key(market: Market | None) -> str:
    text = str(getattr(market, "name", "") or "").strip().lower()
    if text in {"ozon"}:
        return "ozon"
    if text in {"wb", "wildberries", "wildberries (wb)"}:
        return "wb"
    if text in {"yandex", "yandex market", "yandex.market", "яндекс", "яндекс маркет"}:
        return "yandex"
    if text in {"sber", "сбер", "сбермегамаркет", "sbermegamarket"}:
        return "sber"
    return ""


def parse_items_raw(raw_text: str) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    errors: list[str] = []
    lines = [line.strip() for line in str(raw_text or "").splitlines() if line.strip()]
    for index, line in enumerate(lines, start=1):
        parts = [part.strip() for part in line.split(";")]
        if len(parts) < 6:
            errors.append(
                f"Строка {index}: ожидается формат "
                "`артикул;наименование;размер;штрихкод;тип товара;кол-во`."
            )
            continue
        sku_code, name, size, barcode, goods_type, qty_text = parts[:6]
        if not sku_code:
            errors.append(f"Строка {index}: артикул обязателен.")
            continue
        if not name:
            errors.append(f"Строка {index}: наименование обязательно.")
            continue
        try:
            qty = int(qty_text)
        except ValueError:
            errors.append(f"Строка {index}: количество должно быть целым числом.")
            continue
        if qty <= 0:
            errors.append(f"Строка {index}: количество должно быть больше нуля.")
            continue
        rows.append(
            {
                "sku_code": sku_code,
                "name": name,
                "size": size,
                "barcode": barcode,
                "goods_type": goods_type,
                "qty_requested": qty,
            }
        )
    return rows, errors


class ShippingOrderForm(forms.ModelForm):
    wb_transit_warehouse = forms.BooleanField(required=False)
    supply_number = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={
                "placeholder": "Номер поставки Ozon",
                "data-show-for-marketplace": "ozon",
                "autocomplete": "off",
                "autocapitalize": "off",
                "spellcheck": "false",
                "data-no-browser-autofill": "1",
            }
        ),
    )

    class Meta:
        model = ShippingOrder
        fields = [
            "agency",
            "delivery_type",
            "requires_marking_scan",
            "slot_date",
            "slot_time",
            "eta_at",
            "destination_address",
            "shipping_barcode",
            "marketplace",
            "wb_supply_barcode",
            "supply_number",
            "wb_transit_warehouse",
            "transit_address",
            "destination_warehouse",
            "supply_type",
            "vehicle_type",
            "vehicle_number",
            "driver_phone",
            "comment",
        ]
        widgets = {
            "slot_date": forms.DateInput(attrs={"type": "date", "id": "slot_date"}),
            "slot_time": forms.TimeInput(attrs={"type": "time", "id": "slot_time", "step": 300}, format="%H:%M"),
            "eta_at": forms.HiddenInput(attrs={"id": "eta_at"}),
            "destination_address": forms.TextInput(attrs={"placeholder": "Например: склад клиента, другой склад, адрес или город"}),
            "shipping_barcode": forms.TextInput(attrs={"placeholder": "Введите ШК поставки"}),
            "wb_supply_barcode": forms.TextInput(attrs={"placeholder": "Введите номер поставки"}),
            "supply_number": forms.TextInput(
                attrs={
                    "placeholder": "Номер поставки Ozon",
                    "data-show-for-marketplace": "ozon",
                    "autocomplete": "off",
                    "autocapitalize": "off",
                    "spellcheck": "false",
                    "data-no-browser-autofill": "1",
                }
            ),
            "transit_address": forms.TextInput(attrs={"placeholder": "Введите транзитный адрес"}),
            "destination_warehouse": forms.TextInput(attrs={"placeholder": "Введите склад назначения"}),
            "vehicle_number": forms.TextInput(attrs={"placeholder": "A123BC77"}),
            "driver_phone": forms.TextInput(attrs={"inputmode": "tel", "placeholder": "+7 900 000-00-00"}),
            "comment": forms.Textarea(
                attrs={
                    "rows": 5,
                    "placeholder": "Комментарий к заявке для менеджера или склада",
                }
            ),
        }

    def __init__(
        self,
        *args,
        agency_queryset=None,
        locked_agency: Agency | None = None,
        relax_for_autosave: bool = False,
        ozon_multi_warehouse: bool = False,
        allow_marketplace_date_override: bool = False,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.relax_for_autosave = bool(relax_for_autosave)
        self.ozon_multi_warehouse = bool(ozon_multi_warehouse)
        self.allow_marketplace_date_override = bool(allow_marketplace_date_override)
        qs = agency_queryset if agency_queryset is not None else Agency.objects.filter(archived=False)
        self.fields["agency"].queryset = qs.order_by("agn_name")
        if locked_agency is not None:
            self.fields["agency"].initial = locked_agency
            self.fields["agency"].disabled = True
        self.fields["eta_at"].required = False
        self.fields["slot_date"].required = False
        self.fields["slot_time"].required = False
        # Defaulted to marketplace in clean(); keep optional so client/UI
        # posts without the field still create a valid order.
        self.fields["delivery_type"].required = False
        self.fields["delivery_type"].initial = ShippingOrder.DELIVERY_MARKETPLACE
        self.fields["requires_marking_scan"].required = False
        self.fields["marketplace"].queryset = Market.objects.order_by("name")
        self.fields["marketplace"].required = False
        self.fields["shipping_barcode"].required = False
        self.fields["wb_supply_barcode"].required = False
        self.fields["supply_number"].required = False
        self.fields["transit_address"].required = False
        self.fields["destination_warehouse"].required = False
        self.fields["supply_type"].required = False
        self.fields["vehicle_type"].required = False
        self.fields["vehicle_number"].required = False
        self.fields["driver_phone"].required = False
        self.fields["comment"].required = False
        self.fields["destination_warehouse"].widget.attrs["list"] = "destination-warehouse-options"
        self.fields["transit_address"].widget.attrs["list"] = "transit-address-options"
        if self.relax_for_autosave:
            for field in self.fields.values():
                field.required = False

    def clean(self):
        cleaned_data = super().clean()
        if getattr(self, "relax_for_autosave", False):
            delivery_type = str(cleaned_data.get("delivery_type") or "").strip() or ShippingOrder.DELIVERY_MARKETPLACE
            cleaned_data["delivery_type"] = delivery_type
            if delivery_type != ShippingOrder.DELIVERY_MARKETPLACE:
                cleaned_data["requires_marking_scan"] = False
            if delivery_type in {
                ShippingOrder.DELIVERY_PICKUP,
                ShippingOrder.DELIVERY_COURIER,
                ShippingOrder.DELIVERY_TRANSFER,
            }:
                cleaned_data["marketplace"] = None
                cleaned_data["shipping_barcode"] = ""
                cleaned_data["wb_supply_barcode"] = ""
                cleaned_data["wb_transit_warehouse"] = False
                cleaned_data["transit_address"] = ""
                cleaned_data["destination_warehouse"] = ""
                cleaned_data["supply_type"] = ""
                cleaned_data["supply_number"] = ""
                cleaned_data["slot_date"] = None
                cleaned_data["slot_time"] = None
            if delivery_type in {
                ShippingOrder.DELIVERY_PICKUP,
                ShippingOrder.DELIVERY_TRANSFER,
            }:
                cleaned_data["vehicle_type"] = ""
                cleaned_data["vehicle_number"] = ""
                cleaned_data["driver_phone"] = ""
            if not cleaned_data.get("agency"):
                self.add_error("agency", "Выберите клиента.")
            return cleaned_data
        eta_at = cleaned_data.get("eta_at")
        cleaned_data["shipping_barcode"] = str(cleaned_data.get("shipping_barcode") or "").strip()
        cleaned_data["destination_address"] = str(cleaned_data.get("destination_address") or "").strip()
        cleaned_data["wb_supply_barcode"] = str(cleaned_data.get("wb_supply_barcode") or "").strip()
        cleaned_data["transit_address"] = str(cleaned_data.get("transit_address") or "").strip()
        cleaned_data["destination_warehouse"] = str(cleaned_data.get("destination_warehouse") or "").strip()
        slot_date = cleaned_data.get("slot_date")
        slot_time = cleaned_data.get("slot_time")
        delivery_type = str(cleaned_data.get("delivery_type") or "").strip() or ShippingOrder.DELIVERY_MARKETPLACE
        vehicle_type = str(cleaned_data.get("vehicle_type") or "").strip()
        vehicle_number = str(cleaned_data.get("vehicle_number") or "").strip()
        driver_phone = str(cleaned_data.get("driver_phone") or "").strip()
        has_transit = bool(cleaned_data.get("wb_transit_warehouse"))
        marketplace = cleaned_data.get("marketplace")
        marketplace_key = _marketplace_key(marketplace)
        is_pickup = delivery_type == ShippingOrder.DELIVERY_PICKUP
        is_courier = delivery_type == ShippingOrder.DELIVERY_COURIER
        is_transfer = delivery_type == ShippingOrder.DELIVERY_TRANSFER
        is_ozon = marketplace_key == "ozon"
        skip_marketplace_fields = is_pickup or is_courier or is_transfer
        requires_shipping_barcode = not skip_marketplace_fields
        requires_wb_supply_number = not skip_marketplace_fields and not is_ozon
        now_local = timezone.localtime()
        today_local = now_local.date()

        cleaned_data["delivery_type"] = delivery_type
        if delivery_type != ShippingOrder.DELIVERY_MARKETPLACE:
            cleaned_data["requires_marking_scan"] = False
        cleaned_data["vehicle_type"] = vehicle_type
        cleaned_data["vehicle_number"] = vehicle_number
        cleaned_data["driver_phone"] = driver_phone
        cleaned_data["wb_transit_warehouse"] = has_transit

        if skip_marketplace_fields:
            cleaned_data["marketplace"] = None
            cleaned_data["shipping_barcode"] = ""
            cleaned_data["wb_supply_barcode"] = ""
            cleaned_data["wb_transit_warehouse"] = False
            cleaned_data["transit_address"] = ""
            cleaned_data["destination_warehouse"] = ""
            cleaned_data["supply_type"] = ""
            cleaned_data["supply_number"] = ""
            cleaned_data["slot_date"] = None
            cleaned_data["slot_time"] = None
        if is_transfer and not cleaned_data["destination_address"]:
            self.add_error("destination_address", "Укажите, куда нужно переместить товар.")
        if is_pickup or is_transfer:
            cleaned_data["vehicle_type"] = ""
            cleaned_data["vehicle_number"] = ""
            cleaned_data["driver_phone"] = ""
        elif is_ozon:
            cleaned_data["wb_supply_barcode"] = ""
            cleaned_data["supply_number"] = str(cleaned_data.get("supply_number") or "").strip()
            cleaned_data["slot_time"] = slot_time
        elif not skip_marketplace_fields:
            cleaned_data["slot_time"] = None
            cleaned_data["supply_number"] = ""

        if requires_shipping_barcode and not cleaned_data["shipping_barcode"]:
            self.add_error("shipping_barcode", "Укажите ШК поставки.")

        if not skip_marketplace_fields and marketplace is None:
            self.add_error("marketplace", "Выберите маркетплейс.")

        if requires_wb_supply_number and not cleaned_data["wb_supply_barcode"]:
            self.add_error("wb_supply_barcode", "Укажите номер поставки.")

        ozon_distribution = bool(getattr(self, "ozon_multi_warehouse", False)) and is_ozon
        # For an Ozon route through a transit hub the final warehouse may be
        # absent in the marketplace payload. The transit address remains
        # mandatory below, so an empty destination does not make the route
        # ambiguous and does not affect WB or other marketplaces.
        ozon_transit_route = is_ozon and has_transit
        if not skip_marketplace_fields and not cleaned_data["destination_warehouse"]:
            if not ozon_distribution and not ozon_transit_route:
                self.add_error("destination_warehouse", "Укажите склад назначения.")
            else:
                # Final warehouses either live in ShippingDestination rows or
                # were not returned by Ozon for the selected transit route.
                cleaned_data["destination_warehouse"] = ""

        if not skip_marketplace_fields and not str(cleaned_data.get("supply_type") or "").strip():
            self.add_error("supply_type", "Выберите тип поставки.")

        if not skip_marketplace_fields and is_ozon and not cleaned_data.get("supply_number"):
            self.add_error("supply_number", "Для Ozon укажите номер поставки.")

        if ozon_distribution and not skip_marketplace_fields:
            # Ozon multi-destination: transit hub is required (not WB transit-only path).
            if not cleaned_data["transit_address"]:
                self.add_error("transit_address", "Для Ozon укажите транзитный склад.")
            else:
                cleaned_data["wb_transit_warehouse"] = True
        else:
            if not skip_marketplace_fields and has_transit and not cleaned_data["transit_address"]:
                self.add_error("transit_address", "Укажите транзитный адрес.")
            if skip_marketplace_fields or not has_transit:
                cleaned_data["transit_address"] = ""

        if not is_pickup and not is_transfer and not vehicle_type:
            self.add_error("vehicle_type", "Выберите транспорт.")
        if is_courier and vehicle_type and vehicle_type not in {
            ShippingOrder.VEHICLE_CDEK,
            ShippingOrder.VEHICLE_YANDEX,
            ShippingOrder.VEHICLE_FULFILLMENT,
            ShippingOrder.VEHICLE_CLIENT,
        }:
            self.add_error("vehicle_type", "Для курьера выберите СДЭК, Яндекс или другой транспорт.")

        if not is_pickup and not is_transfer and driver_phone:
            digits = "".join(ch for ch in driver_phone if ch.isdigit())
            if len(digits) != 11 or digits[0] not in {"7", "8"}:
                self.add_error("driver_phone", "Введите корректный телефон водителя.")

        if not skip_marketplace_fields and slot_date and slot_date < today_local:
            self.add_error("slot_date", "Дата слота не может быть задним числом.")

        if not skip_marketplace_fields and is_ozon:
            if not slot_date:
                self.add_error("slot_date", "Для Ozon укажите дату слота.")

        if eta_at is None:
            self.add_error("eta_at", "Заполните плановую дату отгрузки.")
        else:
            if timezone.is_naive(eta_at):
                eta_at = timezone.make_aware(eta_at, timezone.get_current_timezone())
                cleaned_data["eta_at"] = eta_at
            eta_local = eta_at.astimezone(timezone.get_current_timezone())
            eta_date = eta_local.date()
            if eta_date < today_local:
                self.add_error("eta_at", "Плановая дата отгрузки не может быть задним числом.")
            elif not skip_marketplace_fields and not self.allow_marketplace_date_override:
                lead_days = 1 if now_local.hour < NEXT_DAY_DEADLINE_HOUR else 2
                minimum_ship_date = today_local + timedelta(days=lead_days)
                if eta_date < minimum_ship_date:
                    day_label = "завтра" if lead_days == 1 else "послезавтра"
                    self.add_error(
                        "eta_at",
                        (
                            "Для клиентской заявки на маркетплейс дата отгрузки должна быть "
                            f"не раньше чем {day_label}, {minimum_ship_date:%d.%m.%Y}. "
                            f"{NEXT_DAY_DEADLINE_ERROR}"
                        ),
                    )
        return cleaned_data


class ShippingOrderItemForm(forms.ModelForm):
    class Meta:
        model = ShippingOrderItem
        fields = [
            "sku_code",
            "name",
            "size",
            "barcode",
            "goods_type",
            "qty_requested",
            "comment",
        ]

    def clean_qty_requested(self):
        qty = int(self.cleaned_data.get("qty_requested") or 0)
        if qty <= 0:
            raise forms.ValidationError("Количество должно быть больше нуля.")
        return qty


class ShippingTransportNoteForm(forms.ModelForm):
    class Meta:
        model = ShippingTransportNote
        fields = [
            "document_number",
            "document_date",
            "shipper_name",
            "shipper_inn",
            "shipper_address",
            "shipper_phone",
            "consignee_name",
            "consignee_inn",
            "consignee_address",
            "consignee_phone",
            "carrier_name",
            "carrier_inn",
            "carrier_address",
            "carrier_phone",
            "loading_address",
            "unloading_address",
            "cargo_name",
            "cargo_package_count",
            "cargo_package_type",
            "cargo_weight_kg",
            "cargo_declared_value",
            "accompanying_documents",
            "special_instructions",
            "transportation_conditions",
            "delivery_notes",
            "driver_name",
            "driver_phone",
            "vehicle_number",
            "trailer_number",
            "service_cost",
        ]
        widgets = {
            "document_date": forms.DateInput(attrs={"type": "date"}),
            "shipper_address": forms.Textarea(attrs={"rows": 3}),
            "consignee_address": forms.Textarea(attrs={"rows": 3}),
            "carrier_address": forms.Textarea(attrs={"rows": 3}),
            "loading_address": forms.Textarea(attrs={"rows": 3}),
            "unloading_address": forms.Textarea(attrs={"rows": 3}),
            "cargo_name": forms.Textarea(attrs={"rows": 4}),
            "accompanying_documents": forms.Textarea(attrs={"rows": 3}),
            "special_instructions": forms.Textarea(attrs={"rows": 3}),
            "transportation_conditions": forms.Textarea(attrs={"rows": 3}),
            "delivery_notes": forms.Textarea(attrs={"rows": 3}),
            "cargo_weight_kg": forms.NumberInput(attrs={"step": "0.001", "min": "0"}),
            "cargo_declared_value": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "service_cost": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        placeholders = {
            "document_number": "Например, ТН-0001",
            "shipper_name": "ООО Ромашка",
            "shipper_inn": "7700000000",
            "shipper_phone": "+7 900 000-00-00",
            "consignee_name": "Склад назначения / маркетплейс",
            "consignee_inn": "7700000000",
            "consignee_phone": "+7 900 000-00-00",
            "carrier_name": "FullBox",
            "carrier_inn": "7700000000",
            "carrier_phone": "+7 499 450-35-55",
            "cargo_package_type": "Короб / паллета",
            "driver_name": "ФИО водителя",
            "driver_phone": "+7 900 000-00-00",
            "vehicle_number": "A123BC77",
            "trailer_number": "При наличии",
        }
        for name, placeholder in placeholders.items():
            if name in self.fields:
                self.fields[name].widget.attrs.setdefault("placeholder", placeholder)
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.Textarea):
                field.widget.attrs.setdefault("placeholder", "Заполните поле")

    def clean(self):
        cleaned_data = super().clean()
        for name, value in list(cleaned_data.items()):
            if isinstance(value, str):
                cleaned_data[name] = value.strip()
        return cleaned_data
