from django.contrib import admin

from .models import Carrier, OwnCompany


@admin.register(OwnCompany)
class OwnCompanyAdmin(admin.ModelAdmin):
    list_display = ("name", "short_name", "inn", "phone", "director_name", "is_default", "is_active")
    search_fields = ("name", "short_name", "inn", "phone", "email", "director_name", "edo_id")
    list_filter = ("is_default", "is_active")


@admin.register(Carrier)
class CarrierAdmin(admin.ModelAdmin):
    list_display = ("name", "short_name", "inn", "phone", "contact_person", "bank_name", "is_active")
    search_fields = ("name", "short_name", "inn", "phone", "email", "contact_person", "bank_name", "settlement_account")
    list_filter = ("is_active",)
