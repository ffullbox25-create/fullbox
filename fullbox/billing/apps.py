from django.apps import AppConfig


class BillingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "billing"
    verbose_name = "Биллинг и счета"

    def ready(self):
        from . import fbs_receivers  # noqa: F401
