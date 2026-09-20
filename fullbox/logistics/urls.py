from django.urls import path

from . import driver_views, external_trip_views, views

app_name = "logistics"

urlpatterns = [
    path("", views.logistics_dashboard, name="dashboard"),
    path("help/", views.logistics_help, name="help"),
    path("trips/", views.logistics_trip_list, name="trip-list"),
    path("trips/planning/", views.logistics_trip_planning, name="trip-planning"),
    path("trips/planning/<int:pk>/action/", views.logistics_trip_planning_action, name="trip-planning-action"),
    path("trips/external/new/", external_trip_views.external_trip_create, name="external-trip-create"),
    path("trips/external/<int:pk>/", external_trip_views.external_trip_detail, name="external-trip-detail"),
    path("trips/<int:pk>/", views.logistics_trip_detail, name="trip-detail"),
    path("trips/<int:pk>/loading/", views.logistics_trip_loading, name="trip-loading"),
    path("trips/<int:pk>/driver-assignment/", driver_views.trip_driver_assignment, name="trip-driver-assignment"),
    path("driver/notifications/", driver_views.driver_notifications, name="driver-notifications"),
    path("driver/trips/", driver_views.driver_trip_list, name="driver-trip-list"),
    path("driver/trips/<int:pk>/", driver_views.driver_trip_detail, name="driver-trip-detail"),
    path("problems/", driver_views.problem_trip_list, name="problem-trip-list"),
    path("problems/<int:pk>/", driver_views.problem_trip_detail, name="problem-trip-detail"),
    path(
        "shipping/<int:order_id>/return-clarify/",
        views.shipping_return_clarify,
        name="shipping-return-clarify",
    ),
    path(
        "shipping/<int:order_id>/resolve-clarify/",
        views.shipping_resolve_clarify,
        name="shipping-resolve-clarify",
    ),
]
