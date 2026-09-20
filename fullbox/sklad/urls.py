from django.urls import path

from .views import (
    dashboard,
    inventory_journal,
    inventory_journal_box_info,
    inventory_journal_pallet_boxes,
    knowledge_article,
    knowledge_catalog,
    missing_box_found,
    missing_box_not_found,
    missing_box_take,
    problem_box_release,
    problem_box_return,
    problem_box_take,
    sku_stock,
    warehouse_box_check,
)

app_name = "sklad"

urlpatterns = [
    path("", dashboard, name="dashboard"),
    path("box-check/", warehouse_box_check, name="warehouse_box_check"),
    path("sku-stock/", sku_stock, name="sku_stock"),
    path("journal/", inventory_journal, name="inventory_journal"),
    path("journal/pallet-boxes/", inventory_journal_pallet_boxes, name="inventory_journal_pallet_boxes"),
    path("journal/box-info/", inventory_journal_box_info, name="inventory_journal_box_info"),
    path("journal/problem-boxes/<int:snapshot_id>/take/", problem_box_take, name="problem_box_take"),
    path("journal/problem-boxes/<int:snapshot_id>/return/", problem_box_return, name="problem_box_return"),
    path("journal/problem-boxes/<int:snapshot_id>/release/", problem_box_release, name="problem_box_release"),
    path("journal/missing-boxes/<int:operation_id>/take/", missing_box_take, name="missing_box_take"),
    path("journal/missing-boxes/<int:operation_id>/found/", missing_box_found, name="missing_box_found"),
    path("journal/missing-boxes/<int:operation_id>/not-found/", missing_box_not_found, name="missing_box_not_found"),
    path("knowledge/", knowledge_catalog, name="knowledge"),
    path("knowledge/<slug:slug>/", knowledge_article, name="knowledge-article"),
]
