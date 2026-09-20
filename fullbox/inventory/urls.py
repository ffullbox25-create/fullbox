from django.urls import path

from .views import (
    InventoryActionView,
    InventoryCreateView,
    InventoryDetailView,
    InventoryExportView,
    InventoryListView,
    InventorySkuSearchView,
)


app_name = "inventory"

urlpatterns = [
    path("", InventoryListView.as_view(), name="list"),
    path("new/", InventoryCreateView.as_view(), name="create"),
    path("api/sku-search/", InventorySkuSearchView.as_view(), name="sku-search"),
    path("<int:pk>/", InventoryDetailView.as_view(), name="detail"),
    path("<int:pk>/action/", InventoryActionView.as_view(), name="action"),
    path("<int:pk>/export/", InventoryExportView.as_view(), name="export"),
]
