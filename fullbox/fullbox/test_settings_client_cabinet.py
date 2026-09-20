import os

from .settings import *


ROOT_URLCONF = "client_cabinet.test_urls"
DEBUG = True
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

visual_db = str(os.environ.get("FULLBOX_CLIENT_VISUAL_DB") or "").strip()
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": visual_db or ":memory:",
    }
}
