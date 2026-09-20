from django.apps import AppConfig


class OtgReachtruckConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "otg_reachtruck"

    def ready(self):
        from . import signals  # noqa: F401
