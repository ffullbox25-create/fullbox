from __future__ import annotations

from django.conf import settings
from django.db import transaction

from sklad.models import WarehouseTemporaryNomenclature
from sku.models import Agency


def temporary_nomenclature_enabled() -> bool:
    return bool(getattr(settings, "WAREHOUSE_TEMP_NOMENCLATURE_ENABLED", False))


class WarehouseTemporaryNomenclatureService:
    DEFAULT_GOODS_TYPE = "op"

    @staticmethod
    def _clean(value) -> str:
        return str(value or "").strip()

    @classmethod
    def identity_key(
        cls,
        *,
        item_code: str | None,
        name: str | None,
        size: str | None,
        brand: str | None = None,
        color: str | None = None,
    ) -> str:
        return "|".join(
            (
                cls._clean(item_code).lower(),
                cls._clean(name).lower(),
                cls._clean(brand).lower(),
                cls._clean(color).lower(),
                cls._clean(size).lower(),
            )
        )

    @classmethod
    def generated_item_code(cls, *, agency_id: int, item_id: int) -> str:
        return f"OPT-{agency_id}-{item_id}"

    @classmethod
    @transaction.atomic
    def ensure_items(
        cls,
        *,
        agency: Agency | None,
        items: list[dict] | None,
        order_type: str = "receiving",
        order_id: str = "",
        goods_type: str = "",
    ) -> list[dict]:
        if not temporary_nomenclature_enabled():
            return items or []
        if not agency or not items:
            return items or []

        normalized_goods_type = cls._clean(goods_type).lower() or cls.DEFAULT_GOODS_TYPE
        indexed_rows: list[tuple[dict, str, str, str, str, str]] = []
        identity_keys: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_code = cls._clean(item.get("sku_code") or item.get("sku"))
            name = cls._clean(item.get("name"))
            brand = cls._clean(item.get("brand"))
            color = cls._clean(item.get("color"))
            size = cls._clean(item.get("size"))
            barcode = cls._clean(item.get("barcode"))
            if not any((item_code, name, size, barcode)):
                continue
            identity_key = cls.identity_key(
                item_code=item_code,
                name=name,
                size=size,
                brand=brand,
                color=color,
            )
            if not identity_key.replace("|", ""):
                continue
            indexed_rows.append((item, identity_key, item_code, name, brand, color, size, barcode))
            identity_keys.append(identity_key)

        if not indexed_rows:
            return items

        existing = {
            row.identity_key: row
            for row in WarehouseTemporaryNomenclature.objects.filter(
                agency=agency,
                identity_key__in=identity_keys,
            )
        }

        context_type = cls._clean(order_type)
        context_id = cls._clean(order_id)
        for item, identity_key, item_code, name, brand, color, size, barcode in indexed_rows:
            temp_item = existing.get(identity_key)
            if temp_item is None:
                temp_item = WarehouseTemporaryNomenclature.objects.create(
                    agency=agency,
                    identity_key=identity_key,
                    item_code=item_code,
                    name=name or item_code or "Временная позиция",
                    brand=brand,
                    color=color,
                    size=size,
                    barcode=barcode,
                    goods_type=normalized_goods_type,
                    first_context_type=context_type,
                    first_context_id=context_id,
                    last_context_type=context_type,
                    last_context_id=context_id,
                )
                if not temp_item.item_code:
                    temp_item.item_code = cls.generated_item_code(
                        agency_id=int(agency.id or 0),
                        item_id=int(temp_item.id or 0),
                    )
                    temp_item.save(update_fields=["item_code", "updated_at"])
                existing[identity_key] = temp_item
            else:
                update_fields: list[str] = []
                if item_code and temp_item.item_code != item_code:
                    temp_item.item_code = item_code
                    update_fields.append("item_code")
                if name and temp_item.name != name:
                    temp_item.name = name
                    update_fields.append("name")
                if brand and temp_item.brand != brand:
                    temp_item.brand = brand
                    update_fields.append("brand")
                if color and temp_item.color != color:
                    temp_item.color = color
                    update_fields.append("color")
                if size and temp_item.size != size:
                    temp_item.size = size
                    update_fields.append("size")
                if barcode and temp_item.barcode != barcode:
                    temp_item.barcode = barcode
                    update_fields.append("barcode")
                if normalized_goods_type and temp_item.goods_type != normalized_goods_type:
                    temp_item.goods_type = normalized_goods_type
                    update_fields.append("goods_type")
                if context_type and temp_item.last_context_type != context_type:
                    temp_item.last_context_type = context_type
                    update_fields.append("last_context_type")
                if context_id and temp_item.last_context_id != context_id:
                    temp_item.last_context_id = context_id
                    update_fields.append("last_context_id")
                if update_fields:
                    temp_item.save(update_fields=update_fields + ["updated_at"])

            resolved_code = cls._clean(temp_item.item_code)
            item["temporary_nomenclature_id"] = int(temp_item.id or 0)
            item["temporary_nomenclature_code"] = resolved_code
            item["nomenclature_kind"] = "temporary"
            if not item_code and resolved_code:
                item["sku_code"] = resolved_code
            item["sku"] = cls._clean(item.get("sku") or item.get("sku_code") or resolved_code)
            if not name and temp_item.name:
                item["name"] = temp_item.name
            if not brand and temp_item.brand:
                item["brand"] = temp_item.brand
            if not color and temp_item.color:
                item["color"] = temp_item.color
            if not size and temp_item.size:
                item["size"] = temp_item.size
            if not barcode and temp_item.barcode:
                item["barcode"] = temp_item.barcode

        return items

    @classmethod
    def list_catalog_items(
        cls,
        *,
        agency: Agency | None = None,
        agency_id: int | None = None,
        goods_type: str = "",
    ) -> list[dict]:
        if not temporary_nomenclature_enabled():
            return []
        target_agency_id = agency_id or getattr(agency, "id", None)
        if not target_agency_id:
            return []
        queryset = WarehouseTemporaryNomenclature.objects.filter(
            agency_id=target_agency_id,
            normalized_at__isnull=True,
        ).order_by("item_code", "name", "size", "id")
        normalized_goods_type = cls._clean(goods_type).lower()
        if normalized_goods_type:
            queryset = queryset.filter(goods_type__iexact=normalized_goods_type)
        return [
            {
                "id": int(row.id or 0),
                "code": cls._clean(row.item_code),
                "name": cls._clean(row.name),
                "brand": cls._clean(row.brand),
                "color": cls._clean(row.color),
                "size": cls._clean(row.size),
                "barcode": cls._clean(row.barcode),
                "goods_type": cls._clean(row.goods_type).lower(),
            }
            for row in queryset
        ]
