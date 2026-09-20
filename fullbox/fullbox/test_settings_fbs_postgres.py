import copy

from . import settings as base_settings
from .test_settings_fbs import *


DATABASES = copy.deepcopy(base_settings.DATABASES)
DATABASES["default"].setdefault("TEST", {})["NAME"] = "test_fullbox_fbs_load"
ROOT_URLCONF = "fullbox.test_urls_minimal"
