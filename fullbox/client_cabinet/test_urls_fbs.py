from django.urls import path

from .fbs_lk import (
    api_fbs_movement_detail,
    api_fbs_movement_import,
    api_fbs_movement_stock,
    api_fbs_movements,
    api_fbs_order_detail,
    api_fbs_orders,
    api_fbs_overview,
    api_fbs_reports,
    api_fbs_stock,
    api_fbs_stock_detail,
    fbs_movement_template,
)
from .web_ui import dashboard_lk


urlpatterns = [
    path("client/", dashboard_lk),
    path("client/api/v1/fbs/", api_fbs_overview),
    path("client/api/v1/fbs/orders/", api_fbs_orders),
    path("client/api/v1/fbs/orders/<int:order_id>/", api_fbs_order_detail),
    path("client/api/v1/fbs/stock/", api_fbs_stock),
    path("client/api/v1/fbs/stock/<str:barcode>/", api_fbs_stock_detail),
    path("client/api/v1/fbs/reports/", api_fbs_reports),
    path("client/api/v1/fbs/movements/", api_fbs_movements),
    path("client/api/v1/fbs/movement-stock/", api_fbs_movement_stock),
    path("client/api/v1/fbs/movements/import/", api_fbs_movement_import),
    path("client/api/v1/fbs/movements/<int:request_id>/", api_fbs_movement_detail),
    path("client/fbs/movement-template.xlsx", fbs_movement_template),
]
