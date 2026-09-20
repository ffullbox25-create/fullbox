from django.urls import path

from .views import (
    LabelSettingsView,
    download_fullbox_agent_bundle,
    equipment_status,
    scanner_detect_start,
    scanner_detect_status,
    scanner_settings_apply,
    scanner_settings_save,
    scanner_test,
    scanner_test_status,
)

app_name = "labels"

urlpatterns = [
    path("settings/", LabelSettingsView.as_view(), name="settings"),
    path("equipment/status/", equipment_status, name="equipment-status"),
    path("scanners/settings/", scanner_settings_save, name="scanner-settings"),
    path("scanners/apply/", scanner_settings_apply, name="scanner-apply"),
    path("scanners/test/", scanner_test, name="scanner-test"),
    path("scanners/test/<int:command_id>/", scanner_test_status, name="scanner-test-status"),
    path("scanners/detect/", scanner_detect_start, name="scanner-detect-start"),
    path("scanners/detect/status/", scanner_detect_status, name="scanner-detect-status"),
    path("scanners/agent/download/", download_fullbox_agent_bundle, name="scanner-agent-download"),
]
