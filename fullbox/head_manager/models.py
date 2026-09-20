from django.db import models

from sku.models import abbreviate_agency_name


class OwnCompany(models.Model):
    TAX_MODE_VAT = "vat"
    TAX_MODE_NO_VAT = "no_vat"
    TAX_MODE_CHOICES = [
        (TAX_MODE_VAT, "С НДС"),
        (TAX_MODE_NO_VAT, "Без НДС"),
    ]

    name = models.CharField("Полное название", max_length=255)
    short_name = models.CharField("Сокращенное название", max_length=255, blank=True)
    inn = models.CharField("ИНН", max_length=32, blank=True)
    kpp = models.CharField("КПП", max_length=32, blank=True)
    ogrn = models.CharField("ОГРН / ОГРНИП", max_length=32, blank=True)
    address = models.TextField("Адрес", blank=True)
    postal_address = models.TextField("Почтовый адрес", blank=True)
    phone = models.CharField("Телефон", max_length=32, blank=True)
    email = models.EmailField("Email", blank=True)
    director_name = models.CharField("Руководитель", max_length=255, blank=True)
    director_basis = models.CharField("Основание полномочий", max_length=255, blank=True)
    bank_name = models.CharField("Банк", max_length=255, blank=True)
    bank_bik = models.CharField("БИК", max_length=32, blank=True)
    settlement_account = models.CharField("Расчетный счет", max_length=64, blank=True)
    correspondent_account = models.CharField("Корреспондентский счет", max_length=64, blank=True)
    bank_address = models.TextField("Адрес банка", blank=True)
    edo_operator = models.CharField("Оператор ЭДО", max_length=255, blank=True)
    edo_id = models.CharField("ID в ЭДО", max_length=255, blank=True)
    tax_mode = models.CharField("Налоговый режим", max_length=16, choices=TAX_MODE_CHOICES, default=TAX_MODE_VAT)
    vat_rate = models.CharField("Ставка НДС", max_length=16, default="20")
    is_default = models.BooleanField("Компания по умолчанию", default=False)
    is_active = models.BooleanField("Активна", default=True)
    comment = models.TextField("Комментарий", blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        verbose_name = "Наша компания"
        verbose_name_plural = "Наши компании"
        ordering = ["-is_default", "name"]

    def __str__(self) -> str:
        return self.short_name or self.name

    def save(self, *args, **kwargs):
        self.short_name = abbreviate_agency_name(self.name) or ""
        super().save(*args, **kwargs)
        if self.is_default:
            type(self).objects.exclude(pk=self.pk).filter(is_default=True).update(is_default=False)


class Carrier(models.Model):
    name = models.CharField("Полное название", max_length=255)
    short_name = models.CharField("Сокращенное название", max_length=255, blank=True)
    inn = models.CharField("ИНН", max_length=32, blank=True)
    kpp = models.CharField("КПП", max_length=32, blank=True)
    ogrn = models.CharField("ОГРН / ОГРНИП", max_length=32, blank=True)
    address = models.TextField("Адрес", blank=True)
    postal_address = models.TextField("Почтовый адрес", blank=True)
    phone = models.CharField("Телефон", max_length=32, blank=True)
    email = models.EmailField("Email", blank=True)
    contact_person = models.CharField("Контактное лицо", max_length=255, blank=True)
    bank_name = models.CharField("Банк", max_length=255, blank=True)
    bank_bik = models.CharField("БИК", max_length=32, blank=True)
    settlement_account = models.CharField("Расчетный счет", max_length=64, blank=True)
    correspondent_account = models.CharField("Корреспондентский счет", max_length=64, blank=True)
    bank_address = models.TextField("Адрес банка", blank=True)
    is_active = models.BooleanField("Активен", default=True)
    comment = models.TextField("Комментарий", blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        verbose_name = "Перевозчик"
        verbose_name_plural = "Перевозчики"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.short_name or self.name

    def save(self, *args, **kwargs):
        self.short_name = abbreviate_agency_name(self.name) or ""
        super().save(*args, **kwargs)
