from django.urls import path

from .views import (
    ReachtruckDashboardView,
    create_move_request,
    lookup_item_pallets,
    lookup_pallet_location,
)

app_name = "reachtruck"

urlpatterns = [
    path("", ReachtruckDashboardView.as_view(), name="dashboard"),
    path("lookup/", lookup_pallet_location, name="lookup"),
    path("lookup-item/", lookup_item_pallets, name="lookup-item"),
    path("requests/create/", create_move_request, name="request-create"),
]
