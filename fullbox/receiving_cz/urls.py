from django.urls import path

from .views import (
    ReceivingCzFlowView,
    receiving_cz_container_code,
    receiving_cz_flow_export,
    receiving_cz_delete_box_units,
    receiving_cz_delete_unit,
    receiving_cz_scan,
)


app_name = "receiving_cz"

urlpatterns = [
    path("", ReceivingCzFlowView.as_view(), name="flow"),
    path("container-code/", receiving_cz_container_code, name="container-code"),
    path("export/", receiving_cz_flow_export, name="export"),
    path("scan/", receiving_cz_scan, name="scan"),
    path("box/delete-units/", receiving_cz_delete_box_units, name="delete-box-units"),
    path("unit/delete/", receiving_cz_delete_unit, name="delete-unit"),
]
