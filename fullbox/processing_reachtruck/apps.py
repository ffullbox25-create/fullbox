from django.apps import AppConfig


class ProcessingReachtruckConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "processing_reachtruck"

    def ready(self):
        from . import queue_signals  # noqa: F401
