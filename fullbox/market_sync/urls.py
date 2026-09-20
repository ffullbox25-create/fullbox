from django.urls import path

from .views import (
    dashboard,
    wb_settings,
    wb_sync_run,
    ozon_settings,
    ozon_sync_run,
    sync_client_run,
    sync_all_run,
    report_detail,
)

urlpatterns = [
    path("", dashboard, name="market-sync"),
    path("wb/", wb_settings, name="market-sync-wb"),
    path("ozon/", ozon_settings, name="market-sync-ozon"),
    path("wb/run/", wb_sync_run, name="market-sync-wb-run"),
    path("ozon/run/", ozon_sync_run, name="market-sync-ozon-run"),
    path("client/run/", sync_client_run, name="market-sync-client-run"),
    path("run-all/", sync_all_run, name="market-sync-run-all"),
    path("report/<int:report_id>/", report_detail, name="market-sync-report"),
]
