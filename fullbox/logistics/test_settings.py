from fullbox.settings import *  # noqa: F403


ROOT_URLCONF = "logistics.test_urls"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
