from django.apps import AppConfig


class TodoConfig(AppConfig):
    name = 'todo'

    def ready(self):
        from . import attention_signals  # noqa: F401
        from . import signals  # noqa: F401
