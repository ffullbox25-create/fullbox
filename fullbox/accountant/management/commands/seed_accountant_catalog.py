from __future__ import annotations

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from billing.models import BillingService, TariffCategory
from billing.standard_price_catalog import seed_standard_prices
from head_manager.models import OwnCompany
from sku.models import Agency

from accountant.models import ClientLifecycle


CATEGORIES = [
    ("receiving", "Приемка", 10),
    ("processing", "Обработка", 20),
    ("marking", "Маркировка", 30),
    ("packing", "Упаковка", 40),
    ("storage", "Хранение", 50),
    ("shipping", "Отгрузка", 60),
    ("logistics", "Логистика", 70),
    ("extra", "Дополнительные услуги", 80),
    ("honest_sign", "Честный знак", 90),
    ("other", "Прочее", 100),
]

SERVICES = [
    ("receiving_goods", "Приемка товара", "receiving", "короб", "10"),
    ("processing_unit", "Обработка единицы", "processing", "шт", "5"),
    ("marking_goods", "Маркировка", "marking", "шт", "5"),
    ("label_print", "Печать этикетки", "marking", "шт", "2"),
    ("packing_goods", "Упаковка", "packing", "шт", "8"),
    ("box_assembly", "Сборка короба", "packing", "короб", "15"),
    ("storage_pallet", "Хранение паллеты", "storage", "паллета/день", "50"),
    ("storage_box", "Хранение короба", "storage", "короб/день", "10"),
    ("ship_wb", "Отгрузка на WB", "shipping", "короб", "20"),
    ("ship_ozon", "Отгрузка на Ozon", "shipping", "короб", "20"),
    ("ship_ym", "Отгрузка на Яндекс Маркет", "shipping", "короб", "20"),
    ("pallet_move", "Перемещение паллет", "logistics", "паллета", "30"),
    ("extra_work", "Дополнительные работы", "extra", "час", "500"),
    ("photo_goods", "Фото товара", "extra", "шт", "15"),
    ("defect_check", "Проверка брака", "extra", "шт", "3"),
    ("repack", "Переупаковка", "packing", "шт", "10"),
    ("sticker", "Стикеровка", "marking", "шт", "3"),
    ("honest_sign", "Честный знак", "honest_sign", "шт", "4"),
    ("palletizing", "Паллетирование", "logistics", "паллета", "40"),
    ("unload", "Разгрузка", "receiving", "паллета", "100"),
    ("load", "Погрузка", "shipping", "паллета", "100"),
    ("delivery", "Доставка", "logistics", "рейс", "0"),
    ("other_service", "Прочие услуги", "other", "усл", "0"),
]


class Command(BaseCommand):
    help = "Seed service catalog, OwnCompany VAT presets, and ClientLifecycle for existing agencies"

    @transaction.atomic
    def handle(self, *args, **options):
        cats = {}
        for code, name, order in CATEGORIES:
            cat, _ = TariffCategory.objects.get_or_create(
                code=code,
                defaults={"name": name, "sort_order": order, "is_active": True},
            )
            if cat.name != name or cat.sort_order != order:
                cat.name = name
                cat.sort_order = order
                cat.is_active = True
                cat.save(update_fields=["name", "sort_order", "is_active", "updated_at"])
            cats[code] = cat
        self.stdout.write(f"categories={len(cats)}")

        # Полный прайс FullBox → BillingService.default_price + StandardServicePrice
        price_seed = seed_standard_prices()
        self.stdout.write(f"standard_prices={price_seed}")

        created_services = 0
        for code, name, cat_code, unit, price in SERVICES:
            svc, was_created = BillingService.objects.get_or_create(
                code=code,
                defaults={
                    "name": name,
                    "unit": unit,
                    "category": cats[cat_code],
                    "default_price": Decimal(price),
                    "used_in_billing": True,
                    "is_active": True,
                    "sort_order": created_services * 10 + 10,
                },
            )
            if was_created:
                created_services += 1
            else:
                updated = False
                if not svc.category_id:
                    svc.category = cats[cat_code]
                    updated = True
                if svc.default_price is None:
                    svc.default_price = Decimal(price)
                    updated = True
                if not svc.used_in_billing:
                    svc.used_in_billing = True
                    updated = True
                if updated:
                    svc.save()
        self.stdout.write(f"services_created={created_services}")

        ip_co, _ = OwnCompany.objects.get_or_create(
            inn="000000000001",
            defaults={
                "name": "ИП FullBox без НДС",
                "short_name": "ИП без НДС",
                "tax_mode": "no_vat",
                "vat_rate": "0",
                "is_active": True,
                "is_default": False,
            },
        )
        if ip_co.tax_mode != "no_vat" or str(ip_co.vat_rate) != "0":
            ip_co.tax_mode = "no_vat"
            ip_co.vat_rate = "0"
            ip_co.is_active = True
            ip_co.save(update_fields=["tax_mode", "vat_rate", "is_active", "updated_at"])

        ooo_co, _ = OwnCompany.objects.get_or_create(
            inn="000000000002",
            defaults={
                "name": "ООО FullBox НДС 5%",
                "short_name": "ООО НДС 5%",
                "tax_mode": "vat",
                "vat_rate": "5",
                "is_active": True,
                "is_default": False,
            },
        )
        if ooo_co.tax_mode != "vat" or str(ooo_co.vat_rate) != "5":
            ooo_co.tax_mode = "vat"
            ooo_co.vat_rate = "5"
            ooo_co.is_active = True
            ooo_co.save(update_fields=["tax_mode", "vat_rate", "is_active", "updated_at"])
        self.stdout.write(f"own_companies ip={ip_co.id} ooo={ooo_co.id}")

        # Existing agencies → active lifecycle (бухгалтер дополнит позже)
        made = 0
        for agency in Agency.objects.all().iterator():
            status = ClientLifecycle.STATUS_ARCHIVED if agency.archived else ClientLifecycle.STATUS_ACTIVE
            lifecycle, created = ClientLifecycle.objects.get_or_create(
                agency=agency,
                defaults={
                    "status": status,
                    "email_documents": agency.email or "",
                    "email_notifications": agency.email or "",
                },
            )
            if created:
                made += 1
            elif agency.archived and lifecycle.status != ClientLifecycle.STATUS_ARCHIVED:
                lifecycle.status = ClientLifecycle.STATUS_ARCHIVED
                lifecycle.save(update_fields=["status", "updated_at"])
            elif (not agency.archived) and lifecycle.status == ClientLifecycle.STATUS_DRAFT and not lifecycle.created_by_id:
                # только автосозданные без явного черновика менеджера — уже active из get_or_create
                pass
        self.stdout.write(self.style.SUCCESS(f"lifecycles_created={made}"))
