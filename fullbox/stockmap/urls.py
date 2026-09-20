from django.urls import path

from .views import StockMapPickerDraftView, StockMapPrView, StockMapRowView, StockMapView, StockMapVisualView

app_name = "stockmap"

urlpatterns = [
    path("", StockMapView.as_view(), name="index"),
    path("visual/", StockMapVisualView.as_view(), name="visual"),
    path("picker/draft/", StockMapPickerDraftView.as_view(), name="picker-draft"),
    path("pr/", StockMapPrView.as_view(), name="pr-zone"),
    path("os/<int:row>/", StockMapRowView.as_view(), name="os-row"),
]
