from django.urls import path

from . import views, web_ui

app_name = "shipping"

urlpatterns = [
    path("", views.shipping_list, name="list"),
    path("new/", views.shipping_create, name="create"),
    path("template.xlsx", views.shipping_client_template, name="client-template"),
    path("template-ozon.xlsx", views.shipping_ozon_client_template, name="client-ozon-template"),
    path("ozon-supplies/", views.shipping_ozon_supplies_list, name="ozon-supplies"),
    path("ozon-supplies/batch/", web_ui.shipping_ozon_supplies_batch, name="ozon-supplies-batch"),
    path("ozon-supplies/<int:order_id>/gm-preload/", web_ui.shipping_ozon_gm_preload, name="ozon-gm-preload"),
    path("ozon-supplies/<int:order_id>/", views.shipping_ozon_supplies_detail, name="ozon-supplies-detail"),
    path("<int:pk>/", views.shipping_detail, name="detail"),
    path("<int:pk>/ozon-gm-labels/", web_ui.shipping_ozon_gm_labels, name="ozon-gm-labels"),
    path("<int:pk>/report/", views.shipping_report, name="report"),
    path("<int:pk>/report.xlsx", views.shipping_report_xlsx, name="report-xlsx"),
    path("<int:pk>/boxes.xlsx", views.shipping_box_composition_xlsx, name="box-composition-xlsx"),
    path("<int:pk>/packing-list.xlsx", views.shipping_packing_list_xlsx, name="packing-list-xlsx"),
    path("<int:pk>/documents/", views.shipping_documents, name="documents"),
    path("<int:pk>/act/", views.shipping_dispatch_act, name="dispatch-act"),
    path("<int:pk>/act/sign-logistician/", views.shipping_sign_dispatch_act_logistician, name="dispatch-act-sign-logistician"),
    path("<int:pk>/act/sign-manager/", views.shipping_sign_dispatch_act_manager, name="dispatch-act-sign-manager"),
    path("<int:pk>/attachments/<int:attachment_id>/", views.shipping_attachment_download, name="attachment-download"),
    path("<int:pk>/attachments/upload/", web_ui.shipping_attachment_upload, name="attachment-upload"),
    path("<int:pk>/attachments/<int:attachment_id>/delete/", web_ui.shipping_attachment_delete, name="attachment-delete"),
    path("<int:pk>/packing/loose/container-code/", views.shipping_loose_packing_container_code, name="packing-loose-container-code"),
    path("<int:pk>/packing/loose/marking-scan/", web_ui.shipping_loose_packing_marking_scan, name="packing-loose-marking-scan"),
    path("<int:pk>/packing/loose/draft/", web_ui.shipping_loose_packing_draft, name="packing-loose-draft"),
    path("<int:pk>/packing/loose/", views.shipping_loose_packing, name="packing-loose"),
    path("<int:pk>/packing/pallet-removal/request/", views.shipping_packing_pallet_removal_request, name="packing-pallet-removal-request"),
    path("<int:pk>/packing/pallet-removal/review/", views.shipping_packing_pallet_removal_review, name="packing-pallet-removal-review"),
    path("<int:pk>/packing/draft/", web_ui.shipping_packing_draft, name="packing-draft"),
    path("<int:pk>/packing/", views.shipping_packing, name="packing"),
    path("<int:pk>/packing-slips/", views.shipping_packing_slips, name="packing-slips"),
    path("<int:pk>/packing-slips/status/", views.shipping_packing_slips_status, name="packing-slips-status"),
    path("<int:pk>/transport-note/", views.shipping_transport_note, name="transport-note"),
    path("<int:pk>/transport-note/pdf/", views.shipping_transport_note_pdf, name="transport-note-pdf"),
    path("<int:pk>/transport-note/docx/", views.shipping_transport_note_docx, name="transport-note-docx"),
    path("<int:pk>/return-act/doc/", views.shipping_return_act_doc, name="return-act-doc"),
]
