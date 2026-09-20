import re

from django.conf import settings
from django.db import models
from django.db.models import Q


_AGENCY_SHORT_NAME_PATTERNS = (
    (re.compile(r"\bобщество\s+с\s+ограниченной\s+ответственностью\b", re.IGNORECASE), "ООО"),
    (re.compile(r"\bиндивидуальный\s+предприниматель\b", re.IGNORECASE), "ИП"),
)


def abbreviate_agency_name(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    for pattern, replacement in _AGENCY_SHORT_NAME_PATTERNS:
        text = pattern.sub(replacement, text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


class Agency(models.Model):
    """Клиент/агент из fb_agns."""

    id = models.AutoField(primary_key=True)
    agn_name = models.CharField("Организация", max_length=255, blank=True, null=True)
    short_name = models.CharField("Сокращенное название", max_length=255, blank=True, null=True)
    inn = models.CharField("ИНН", max_length=32, blank=True, null=True)
    kpp = models.CharField("КПП", max_length=32, blank=True, null=True)
    adres = models.TextField("Адрес", blank=True, null=True)
    user_id = models.IntegerField("USER_ID", blank=True, null=True)
    phone = models.CharField("Телефон", max_length=32, blank=True, null=True)
    sign_oferta = models.BooleanField("Подписана оферта", default=False)
    pref = models.CharField("Префикс", max_length=32, blank=True, null=True)
    fio_agn = models.CharField("ФИО контактного", max_length=255, blank=True, null=True)
    use_nds = models.BooleanField("Использует НДС", default=False)
    email = models.EmailField("Email", blank=True, null=True)
    mened_user_id = models.IntegerField("MENED_USER_ID", blank=True, null=True)
    fakt_adres = models.TextField("Фактический адрес", blank=True, null=True)
    ogrn = models.CharField("ОГРН", max_length=32, blank=True, null=True)
    bank_bik = models.CharField("БИК", max_length=32, blank=True, null=True)
    bank_name = models.CharField("Банк", max_length=255, blank=True, null=True)
    bank_itog_account = models.CharField("Р/счет", max_length=64, blank=True, null=True)
    bank_koresp_account = models.CharField("Корр. счет", max_length=64, blank=True, null=True)
    contract_numb = models.CharField("Номер договора", max_length=64, blank=True, null=True)
    contract_link = models.CharField("Ссылка на договор", max_length=255, blank=True, null=True)
    archived = models.BooleanField("Архивирован", default=False)
    portal_user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agency_profile",
        verbose_name="Пользователь",
    )

    class Meta:
        verbose_name = "Клиент"
        verbose_name_plural = "Клиенты"
        ordering = ["agn_name"]

    def __str__(self) -> str:
        return self.agn_name or f"Клиент {self.id}"

    def save(self, *args, **kwargs):
        if not (self.short_name or "").strip():
            self.short_name = abbreviate_agency_name(self.agn_name)
        super().save(*args, **kwargs)


class Market(models.Model):
    """Маркетплейсы из fb_market_list."""

    id = models.PositiveIntegerField(primary_key=True)
    name = models.CharField("Маркетплейс", max_length=64)

    class Meta:
        verbose_name = "Маркетплейс"
        verbose_name_plural = "Маркетплейсы"
        ordering = ["id"]

    def __str__(self) -> str:
        return self.name


class Color(models.Model):
    """Цвета из fb_colors_list."""

    id = models.PositiveIntegerField(primary_key=True)
    name = models.CharField("Цвет", max_length=64)

    class Meta:
        verbose_name = "Цвет"
        verbose_name_plural = "Цвета"
        ordering = ["id"]

    def __str__(self) -> str:
        return self.name


class Store(models.Model):
    """Склады из fb_store_list."""

    id = models.PositiveIntegerField(primary_key=True)
    stor_name = models.CharField("Склад", max_length=128)
    agn_name = models.CharField("Организация (по имени)", max_length=255, blank=True, null=True)
    agency = models.ForeignKey(
        Agency, on_delete=models.SET_NULL, null=True, blank=True, related_name="stores", verbose_name="Клиент"
    )

    class Meta:
        verbose_name = "Склад"
        verbose_name_plural = "Склады"
        ordering = ["id"]

    def __str__(self) -> str:
        return self.stor_name or f"Склад {self.id}"


class MarketCredential(models.Model):
    """Ключи для маркетплейсов из fb_agns_market_artiku."""

    id = models.PositiveIntegerField(primary_key=True)
    agency = models.ForeignKey(
        Agency, on_delete=models.CASCADE, related_name="market_credentials", verbose_name="Клиент"
    )
    market = models.ForeignKey(
        Market, on_delete=models.CASCADE, related_name="credentials", verbose_name="Маркетплейс"
    )
    market_key = models.TextField("Ключ доступа", blank=True, null=True)
    client_id = models.CharField("Client ID", max_length=128, blank=True, null=True)

    class Meta:
        verbose_name = "Ключ маркетплейса"
        verbose_name_plural = "Ключи маркетплейсов"
        unique_together = ("agency", "market")

    def __str__(self) -> str:
        return f"{self.agency} / {self.market}"


class SKU(models.Model):
    SOURCE_CHOICES = [
        ("manual", "Создан вручную"),
        ("marketplace", "Синхронизация маркетплейс"),
    ]

    sku_code = models.CharField("Артикул", max_length=64)
    name = models.CharField("Наименование", max_length=255)
    brand = models.CharField("Бренд", max_length=255, blank=True, null=True)
    market = models.ForeignKey(
        Market, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Маркетплейс"
    )
    agency = models.ForeignKey(
        Agency, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Клиент"
    )
    color = models.CharField("Цвет (текст)", max_length=64, blank=True, null=True)
    color_ref = models.ForeignKey(
        Color, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Цвет из справочника"
    )
    size = models.CharField("Размер", max_length=64, blank=True, null=True)
    name_print = models.CharField("Название для печати", max_length=255, blank=True, null=True)
    code = models.CharField("Код", max_length=128, blank=True, null=True)
    img = models.URLField("Ссылка на фото", max_length=500, blank=True, null=True)
    img_comment = models.TextField("Комментарий к фото", blank=True, null=True)
    gender = models.CharField("Пол", max_length=64, blank=True, null=True)
    season = models.CharField("Сезон", max_length=64, blank=True, null=True)
    additional_name = models.CharField("Доп. наименование", max_length=255, blank=True, null=True)
    composition = models.CharField("Состав", max_length=255, blank=True, null=True)
    made_in = models.CharField("Страна производства", max_length=128, blank=True, null=True)
    cr_product_date = models.DateField("Дата производства", blank=True, null=True)
    end_product_date = models.DateField("Срок годности", blank=True, null=True)
    sign_akciz = models.BooleanField("Признак акциза", default=False)
    tovar_category = models.CharField("Категория товара", max_length=128, blank=True, null=True)
    use_nds = models.BooleanField("Использует НДС", default=False)
    vid_tovar = models.CharField("Вид товара", max_length=128, blank=True, null=True)
    type_tovar = models.CharField("Тип товара", max_length=128, blank=True, null=True)
    stor_unit = models.ForeignKey(
        Store, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Склад хранения"
    )
    weight_kg = models.DecimalField(
        "Вес, кг", max_digits=10, decimal_places=3, blank=True, null=True
    )
    weight_net_kg = models.DecimalField(
        "Вес нетто, кг", max_digits=10, decimal_places=3, blank=True, null=True
    )
    weight_gross_kg = models.DecimalField(
        "Вес брутто, кг", max_digits=10, decimal_places=3, blank=True, null=True
    )
    volume = models.DecimalField(
        "Объем", max_digits=12, decimal_places=3, blank=True, null=True
    )
    length_mm = models.DecimalField(
        "Длина, мм", max_digits=10, decimal_places=1, blank=True, null=True
    )
    width_mm = models.DecimalField(
        "Ширина, мм", max_digits=10, decimal_places=1, blank=True, null=True
    )
    height_mm = models.DecimalField(
        "Высота, мм", max_digits=10, decimal_places=1, blank=True, null=True
    )
    honest_sign = models.BooleanField("Честный знак", default=False)
    deleted = models.BooleanField("Удален", default=False)
    description = models.TextField("Описание", blank=True, null=True)
    source = models.CharField(
        "Источник",
        max_length=32,
        choices=SOURCE_CHOICES,
        default="manual",
    )
    source_reference = models.CharField(
        "Внешний идентификатор", max_length=128, blank=True, null=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "SKU"
        verbose_name_plural = "Номенклатура"
        ordering = ["sku_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["agency", "sku_code"],
                condition=Q(deleted=False),
                name="uniq_sku_code_per_agency_active",
            )
        ]

    def __str__(self) -> str:
        return f"{self.sku_code} - {self.name}"


class SKUBarcode(models.Model):
    agency = models.ForeignKey(
        Agency,
        on_delete=models.CASCADE,
        related_name="sku_barcodes",
        verbose_name="Клиент",
        editable=False,
    )
    sku = models.ForeignKey(
        SKU, on_delete=models.CASCADE, related_name="barcodes", verbose_name="SKU"
    )
    value = models.CharField("Штрихкод", max_length=64)
    size = models.CharField("Размер", max_length=64, blank=True, null=True)
    is_primary = models.BooleanField("Основной", default=False)

    class Meta:
        verbose_name = "Штрихкод"
        verbose_name_plural = "Штрихкоды"
        ordering = ["-is_primary", "value"]
        constraints = [
            models.UniqueConstraint(
                fields=["agency", "value"],
                name="uniq_sku_barcode_per_agency",
            )
        ]

    def save(self, *args, **kwargs):
        if not self.sku_id:
            raise ValueError("Для штрихкода не указан SKU.")
        sku_agency_id = getattr(self.sku, "agency_id", None)
        if sku_agency_id is None:
            sku_agency_id = SKU.objects.only("agency_id").get(pk=self.sku_id).agency_id
        if not sku_agency_id:
            raise ValueError("Для SKU не указан клиент.")
        if self.agency_id and self.agency_id != sku_agency_id:
            raise ValueError("Клиент штрихкода не совпадает с клиентом SKU.")
        if not self.agency_id:
            self.agency_id = sku_agency_id
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = {*update_fields, "agency"}
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        size = f" [{self.size}]" if self.size else ""
        return f"{self.value}{size} ({'основной' if self.is_primary else 'доп.'})"


class SKUPhoto(models.Model):
    sku = models.ForeignKey(
        SKU, on_delete=models.CASCADE, related_name="photos", verbose_name="SKU"
    )
    url = models.URLField("Ссылка на фото")
    sort_order = models.PositiveIntegerField("Порядок", default=0)

    class Meta:
        verbose_name = "Фото товара"
        verbose_name_plural = "Фото товара"
        ordering = ["sort_order", "id"]

    def __str__(self) -> str:
        return self.url


class MarketplaceBinding(models.Model):
    MARKETPLACE_WB = "WB"
    MARKETPLACE_OZON = "OZON"
    MARKETPLACE_OZON_SKU = "OZON_SKU"

    SYNC_MODE_CHOICES = [
        ("readonly", "Только чтение"),
        ("overwrite", "Перезаписывать при синхронизации"),
    ]

    sku = models.ForeignKey(
        SKU,
        on_delete=models.CASCADE,
        related_name="marketplace_bindings",
        verbose_name="SKU",
    )
    marketplace = models.CharField("Маркетплейс", max_length=64)
    external_id = models.CharField("Внешний ID", max_length=128)
    sync_mode = models.CharField(
        "Режим синхронизации", max_length=32, choices=SYNC_MODE_CHOICES, default="readonly"
    )
    last_synced_at = models.DateTimeField("Последняя синхронизация", blank=True, null=True)

    class Meta:
        verbose_name = "Привязка к маркетплейсу"
        verbose_name_plural = "Привязки к маркетплейсам"
        unique_together = ("marketplace", "external_id")

    def __str__(self) -> str:
        return f"{self.marketplace}: {self.external_id}"
