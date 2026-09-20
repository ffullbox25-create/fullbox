from django.apps import AppConfig


class AgentConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "agent"
    verbose_name = "\u0410\u0433\u0435\u043d\u0442 \u043e\u0431\u043e\u0440\u0443\u0434\u043e\u0432\u0430\u043d\u0438\u044f"

    def ready(self):
        from . import signals  # noqa: F401
