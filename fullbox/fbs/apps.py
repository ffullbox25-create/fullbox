from django.apps import AppConfig


class FbsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "fbs"
    verbose_name = "FBS"

    def ready(self):
        from . import checks  # noqa: F401
        from . import order_signals  # noqa: F401
