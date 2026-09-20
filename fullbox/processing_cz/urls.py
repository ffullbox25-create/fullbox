from django.urls import path

from .views import (
    ProcessingCzFlowView,
    processing_cz_container_code,
    processing_cz_delete_box_units,
    processing_cz_delete_unit,
    processing_cz_scan,
)


app_name = "processing_cz"

urlpatterns = [
    path("", ProcessingCzFlowView.as_view(), name="flow"),
    path("container-code/", processing_cz_container_code, name="container-code"),
    path("scan/", processing_cz_scan, name="scan"),
    path("box/delete-units/", processing_cz_delete_box_units, name="delete-box-units"),
    path("unit/delete/", processing_cz_delete_unit, name="delete-unit"),
]
