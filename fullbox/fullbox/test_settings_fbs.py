from .settings import *


INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.messages",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "agent",
    "audit",
    "employees",
    "sku",
    "sklad",
    "head_manager",
    "logistics",
    "shipping",
    "todo",
    "orders",
    "marking",
    "stockmap",
    "billing",
    "labels",
    "processing_app",
    "processing_reachtruck",
    "reachtruck",
    "reachtruck_box_move",
    "fbs.apps.FbsConfig",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "fbs.test_urls"
FBS_MODULE_ENABLED = True
FBS_WAREHOUSE_WRITES_ENABLED = True
FBS_ZONE_CODE = "FBS"
CSRF_FAILURE_VIEW = "django.views.csrf.csrf_failure"
TEMPLATES = [
    {
        "BACKEND": "fbs.test_template_backend.FbsTestDjangoTemplates",
        "DIRS": [
            BASE_DIR / "templates",
            BASE_DIR / "reachtruck" / "templates",
            BASE_DIR / "reachtruck_box_move" / "templates",
        ],
        "APP_DIRS": False,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(os.environ.get("FULLBOX_FBS_VISUAL_DB") or "").strip() or ":memory:",
    }
}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"null": {"class": "logging.NullHandler"}},
    "loggers": {"audit.models": {"handlers": ["null"], "propagate": False}},
}
