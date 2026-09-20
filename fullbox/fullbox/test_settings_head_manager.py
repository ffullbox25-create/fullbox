import os

from .settings import *


ROOT_URLCONF = "head_manager.test_urls"

if os.environ.get("FULLBOX_HEAD_MANAGER_USE_LOCAL_DB") != "1":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
