SECRET_KEY = "ai-support-tests-only"
DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost"]
USE_TZ = True
TIME_ZONE = "Europe/Moscow"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "/login/"
AI_SUPPORT_DIAGNOSTICS_ENABLED = False
OPENAI_API_KEY = ""
AI_SUPPORT_OPENAI_MODEL = ""
AI_SUPPORT_OPENAI_BASE_URL = "https://api.openai.com/v1"
AI_SUPPORT_OPENAI_TIMEOUT_SECONDS = 45
ROOT_URLCONF = "ai_support.test_urls"

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "employees",
    "ai_support.apps.AiSupportConfig",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
