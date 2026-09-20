from django.urls import path

from . import views


app_name = "warehouse_goods"

urlpatterns = [
    path("", views.GoodsListView.as_view(), name="list"),
    path("export.xlsx", views.GoodsExportView.as_view(), name="export"),
    path("new/", views.GoodsCreateView.as_view(), name="create"),
    path("<int:pk>/", views.GoodsDetailView.as_view(), name="detail"),
    path("<int:pk>/edit/", views.GoodsEditView.as_view(), name="edit"),
    path("<int:pk>/tab/<slug:tab>/", views.GoodsTabView.as_view(), name="tab"),
    path("<int:pk>/profile/", views.GoodsProfileUpdateView.as_view(), name="profile-update"),
    path("<int:pk>/barcodes/add/", views.GoodsBarcodeAddView.as_view(), name="barcode-add"),
    path("<int:pk>/barcodes/<int:barcode_id>/delete/", views.GoodsBarcodeDeleteView.as_view(), name="barcode-delete"),
    path("<int:pk>/bindings/add/", views.GoodsBindingAddView.as_view(), name="binding-add"),
    path("<int:pk>/bindings/<int:binding_id>/delete/", views.GoodsBindingDeleteView.as_view(), name="binding-delete"),
    path("<int:pk>/files/upload/", views.GoodsFileUploadView.as_view(), name="file-upload"),
    path("<int:pk>/files/<int:file_id>/delete/", views.GoodsFileDeleteView.as_view(), name="file-delete"),
    path("<int:pk>/extra-fields/", views.GoodsExtraFieldsUpdateView.as_view(), name="extra-update"),
    path("<int:pk>/extra-fields/add/", views.GoodsExtraDefinitionAddView.as_view(), name="extra-add"),
    path("<int:pk>/services/add/", views.GoodsServiceAddView.as_view(), name="service-add"),
    path("<int:pk>/services/<int:service_id>/delete/", views.GoodsServiceDeleteView.as_view(), name="service-delete"),
    path("<int:pk>/history/", views.GoodsHistoryView.as_view(), name="history"),
    path("<int:pk>/history.xlsx", views.GoodsHistoryExportView.as_view(), name="history-export"),
    path("<int:pk>/move/", views.GoodsMoveView.as_view(), name="move"),
    path("<int:pk>/transfer/", views.GoodsTransferView.as_view(), name="transfer"),
    path("<int:pk>/labels/pdf/", views.GoodsLabelPdfView.as_view(), name="label-pdf"),
    path("<int:pk>/labels/queue/", views.GoodsLabelQueueView.as_view(), name="label-queue"),
]
