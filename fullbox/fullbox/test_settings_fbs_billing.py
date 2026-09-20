import os

from .settings import *


ROOT_URLCONF = "fbs.test_urls_billing"
FBS_MODULE_ENABLED = True
FBS_WAREHOUSE_WRITES_ENABLED = True
FBS_ZONE_CODE = "FBS"
DEBUG = True
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
CHANNEL_LAYERS = {
    "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
}
CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
}

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(os.environ.get("FULLBOX_FBS_BILLING_TEST_DB") or "").strip()
        or ":memory:",
    }
}
