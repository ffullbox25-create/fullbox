import math
from decimal import Decimal
from pathlib import Path

import pandas as pd
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from sku.models import (
    Agency,
    Color,
    Market,
    MarketCredential,
    SKU,
    SKUBarcode,
    SKUPhoto,
    Store,
)


def as_bool(value) -> bool:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "да", "y", "д"}
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return bool(value)


def as_int(value):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_str(value):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    value = str(value).strip()
    return value or None


def as_decimal(value):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def as_date(value):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if hasattr(value, "date"):
        return value.date()
    return None


class Command(BaseCommand):
    help = "Загружает исходные таблицы fb_* из папки «Начальные таблицы» в БД."

    def add_arguments(self, parser):
        parser.add_argument(
            "--base-path",
            type=str,
            help="Путь к каталогу с файлами fb_*.xlsx (по умолчанию ../Начальные таблицы)",
        )

    def handle(self, *args, **options):
        base_dir = options.get("base_path")
        if base_dir:
            base_path = Path(base_dir)
        else:
            base_path = Path(settings.BASE_DIR).parent / "Начальные таблицы"

        if not base_path.exists():
            raise CommandError(f"Каталог с таблицами не найден: {base_path}")

        self.stdout.write(f"Используем каталог: {base_path}")

        self.load_agencies(base_path / "fb_agns.xlsx")
        self.load_markets(base_path / "fb_market_list.xlsx")
        self.load_colors(base_path / "fb_colors_list.xlsx")
        self.load_stores(base_path / "fb_store_list.xlsx")
        self.load_market_credentials(base_path / "fb_agns_market_artiku.xlsx")
        self.load_skus(base_path / "fb_tovar_list.xlsx")

        self.stdout.write(self.style.SUCCESS("Готово. Данные загружены."))

    def load_agencies(self, path: Path):
        df = pd.read_excel(path)
        created = 0
        for row in df.to_dict(orient="records"):
            agency, is_created = Agency.objects.update_or_create(
                id=row["ID"],
                defaults={
                    "agn_name": as_str(row.get("AGN_NAME")),
                    "inn": as_str(row.get("INN")),
                    "kpp": as_str(row.get("KPP")),
                    "adres": as_str(row.get("ADRES")),
                    "user_id": as_int(row.get("USER_ID")),
                    "phone": as_str(row.get("PHONE")),
                    "sign_oferta": as_bool(row.get("SIGN_OFERTA")),
                    "pref": as_str(row.get("PREF")),
                    "fio_agn": as_str(row.get("FIO_AGN")),
                    "use_nds": as_bool(row.get("USE_NDS")),
                    "email": as_str(row.get("EMAIL")),
                    "mened_user_id": as_int(row.get("MENED_USER_ID")),
                    "fakt_adres": as_str(row.get("FAKT_ADRES")),
                    "ogrn": as_str(row.get("OGRN")),
                    "bank_bik": as_str(row.get("BANK_BIK")),
                    "bank_name": as_str(row.get("BANK_NAME")),
                    "bank_itog_account": as_str(row.get("BANK_ITOG_ACCOUNT")),
                    "bank_koresp_account": as_str(row.get("BANK_KORESP_ACCOUNT")),
                    "contract_numb": as_str(row.get("CONTRACT_NUMB")),
                    "contract_link": as_str(row.get("CONTRACT_LINK")),
                },
            )
            created += int(is_created)
        self.stdout.write(f"Агенты: {len(df)} записей, создано {created}")

    def load_markets(self, path: Path):
        df = pd.read_excel(path)
        created = 0
        for row in df.to_dict(orient="records"):
            _, is_created = Market.objects.update_or_create(
                id=row["ID"],
                defaults={"name": as_str(row.get("MARKET_NAME"))},
            )
            created += int(is_created)
        self.stdout.write(f"Маркетплейсы: {len(df)} записей, создано {created}")

    def load_colors(self, path: Path):
        df = pd.read_excel(path)
        created = 0
        for row in df.to_dict(orient="records"):
            _, is_created = Color.objects.update_or_create(
                id=row["ID"], defaults={"name": as_str(row.get("COLOR_NAME"))}
            )
            created += int(is_created)
        self.stdout.write(f"Цвета: {len(df)} записей, создано {created}")

    def load_stores(self, path: Path):
        df = pd.read_excel(path)
        created = 0
        # сопоставление по имени клиента
        agencies_by_name = {a.agn_name: a for a in Agency.objects.all()}
        for row in df.to_dict(orient="records"):
            agn_name = as_str(row.get("AGN_NAME"))
            agency = agencies_by_name.get(agn_name)
            _, is_created = Store.objects.update_or_create(
                id=row["ID"],
                defaults={
                    "stor_name": as_str(row.get("STOR_NAME")),
                    "agn_name": agn_name,
                    "agency": agency,
                },
            )
            created += int(is_created)
        self.stdout.write(f"Склады: {len(df)} записей, создано {created}")

    def load_market_credentials(self, path: Path):
        df = pd.read_excel(path)
        created = 0
        agencies = {a.id: a for a in Agency.objects.all()}
        markets = {m.id: m for m in Market.objects.all()}
        for row in df.to_dict(orient="records"):
            agn_id = as_int(row.get("AGN_ID"))
            market_id = as_int(row.get("MARKET_ID"))
            agency = agencies.get(agn_id)
            market = markets.get(market_id)
            if not agency or not market:
                continue
            _, is_created = MarketCredential.objects.update_or_create(
                id=row["ID"],
                defaults={
                    "agency": agency,
                    "market": market,
                    "market_key": as_str(row.get("MARCET_KEY")),
                    "client_id": as_str(row.get("CLIENT_ID")),
                },
            )
            created += int(is_created)
        self.stdout.write(f"Ключи маркетплейсов: {len(df)} записей, создано {created}")

    def load_skus(self, path: Path):
        df = pd.read_excel(path)
        agencies = {a.id: a for a in Agency.objects.all()}
        markets = {m.id: m for m in Market.objects.all()}
        colors = {c.id: c for c in Color.objects.all()}
        stores = {s.id: s for s in Store.objects.all()}

        created = 0
        for row in df.to_dict(orient="records"):
            agency = agencies.get(as_int(row.get("AGN_ID")))
            market = markets.get(as_int(row.get("MARKET_TYPE_ID")))
            color_ref = colors.get(as_int(row.get("COLORS_ID")))
            store_unit = stores.get(as_int(row.get("STOR_UNIT_ID")))
            sku_code = as_str(row.get("ARTIKUL"))
            if not sku_code:
                continue

            defaults = {
                "name": as_str(row.get("NAME")) or sku_code,
                "brand": as_str(row.get("BRAND")),
                "market": market,
                "agency": agency,
                "color": color_ref.name if color_ref else as_str(row.get("COLORS_ID")),
                "color_ref": color_ref,
                "size": as_str(row.get("TSIZE")),
                "name_print": as_str(row.get("NAME_PRINT")),
                "code": as_str(row.get("CODE")),
                "img": as_str(row.get("IMG")),
                "img_comment": as_str(row.get("IMG_COMMENT")),
                "gender": as_str(row.get("GENDER")),
                "season": as_str(row.get("SEASON")),
                "additional_name": as_str(row.get("DOP_ITEM_NAME")),
                "composition": as_str(row.get("COMPOSITION")),
                "made_in": as_str(row.get("MADE_IN")),
                "cr_product_date": as_date(row.get("CR_PRODUCT_DATE")),
                "end_product_date": as_date(row.get("END_PRODUCT_DATE")),
                "sign_akciz": as_bool(row.get("SIGN_AKTCIZ")),
                "tovar_category": as_str(row.get("TOVAR_CATEGORY")),
                "use_nds": as_bool(row.get("USE_NDS")),
                "vid_tovar": as_str(row.get("VID_TOVAR")),
                "type_tovar": as_str(row.get("TYPE_TOVAR")),
                "stor_unit": store_unit,
                "weight_kg": as_decimal(row.get("WEIGHT")),
                "volume": as_decimal(row.get("VOLUME")),
                "length_mm": as_decimal(row.get("LENGTH")),
                "width_mm": as_decimal(row.get("WIDTH")),
                "honest_sign": as_bool(row.get("SIGN_CH_ZNAK")),
                "description": as_str(row.get("IMG_COMMENT")) or as_str(row.get("DOP_ITEM_NAME")),
                "source": "marketplace" if market else "manual",
            }

            sku, is_created = SKU.objects.update_or_create(
                sku_code=sku_code,
                defaults=defaults,
            )
            created += int(is_created)

            # Штрихкод
            code_val = as_str(row.get("CODE"))
            if code_val:
                SKUBarcode.objects.get_or_create(
                    sku=sku,
                    value=code_val,
                    defaults={"is_primary": True},
                )

            # Фото
            img_url = as_str(row.get("IMG"))
            if img_url:
                SKUPhoto.objects.get_or_create(
                    sku=sku,
                    url=img_url,
                    defaults={"sort_order": 0},
                )

        self.stdout.write(f"Номенклатура: {len(df)} записей, создано {created}")
