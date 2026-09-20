"""Thin compatibility facade for head manager views and helpers."""

from . import web_ui as _web_ui
from .fbs_new import (
    HeadManagerFbsNewAssemblyProblemView,
    HeadManagerFbsNewAssemblyScanView,
    HeadManagerFbsNewAssemblyStartView,
    HeadManagerFbsNewOrderCreateView,
    HeadManagerFbsNewOrderUpdateView,
    HeadManagerFbsNewExtraFieldActiveView,
    HeadManagerFbsNewExtraFieldCreateView,
    HeadManagerFbsNewExtraFieldExportView,
    HeadManagerFbsNewExtraFieldUpdateView,
    HeadManagerFbsNewExtraFieldValueView,
    HeadManagerFbsNewInventoryActionView,
    HeadManagerFbsNewInventoryCreateView,
    HeadManagerFbsNewInventoryExportView,
    HeadManagerFbsNewBoxArchiveView,
    HeadManagerFbsNewBoxCreateView,
    HeadManagerFbsNewBoxUpdateView,
    HeadManagerFbsNewDocumentActView,
    HeadManagerFbsNewDocumentActionView,
    HeadManagerFbsNewDocumentCreateView,
    HeadManagerFbsNewBundleActionView,
    HeadManagerFbsNewMovementActionView,
    HeadManagerFbsNewMovementExportView,
    HeadManagerFbsNewLogisticsManifestCreateView,
    HeadManagerFbsNewLogisticsManifestActionView,
    HeadManagerFbsNewLogisticsOrderActionView,
    HeadManagerFbsNewLogisticsOrderCreateView,
    HeadManagerFbsNewLogisticsOrderImportView,
    HeadManagerFbsNewLogisticsPackageActionView,
    HeadManagerFbsNewLogisticsPackageCreateView,
    HeadManagerFbsNewLogisticsPackageLabelsView,
    HeadManagerFbsNewLogisticsSettingsView,
    HeadManagerFbsNewPilotAccessView,
    HeadManagerFbsNewMarkingActionView,
    HeadManagerFbsNewMarkingCreateView,
    HeadManagerFbsNewMarkingExportView,
    HeadManagerFbsNewMarketplaceStockExportView,
    HeadManagerFbsNewMarketplaceStockSyncView,
    HeadManagerFbsNewPartnerBillingActionView,
    HeadManagerFbsNewPartnerCreateView,
    HeadManagerFbsNewPartnerUpdateView,
    HeadManagerFbsNewInvoiceActionView,
    HeadManagerFbsNewInvoiceCreateView,
    HeadManagerFbsNewPrimaryDocumentDownloadView,
    HeadManagerFbsNewPrimaryDocumentInvoiceView,
    HeadManagerFbsNewProductActionView,
    HeadManagerFbsNewProductCreateView,
    HeadManagerFbsNewProductExportView,
    HeadManagerFbsNewProductImportView,
    HeadManagerFbsNewProductUpdateView,
    HeadManagerFbsNewQueueActionView,
    HeadManagerFbsNewReturnActionView,
    HeadManagerFbsNewReturnCreateView,
    HeadManagerFbsNewReturnDetailActionView,
    HeadManagerFbsNewReturnExportView,
    HeadManagerFbsNewReportExportView,
    HeadManagerFbsNewShipmentActionView,
    HeadManagerFbsNewShipmentBulkView,
    HeadManagerFbsNewShipmentCreateView,
    HeadManagerFbsNewShipmentDocumentsView,
    HeadManagerFbsNewSettingsActionView,
    HeadManagerFbsNewHelpActionView,
    HeadManagerFbsNewTaskCreateView,
    HeadManagerFbsNewTaskBoardActionView,
    HeadManagerFbsNewTaskDetailActionView,
    HeadManagerFbsNewTaskUpdateView,
    HeadManagerFbsNewMultiacceptanceActionView,
    HeadManagerFbsNewProblemActionView,
    HeadManagerFbsNewWaveActionView,
    HeadManagerFbsNewWaveCollectingListView,
    HeadManagerFbsNewWaveUpdateView,
    HeadManagerFbsNewView,
)

CarrierCreateView = _web_ui.CarrierCreateView
CarrierListView = _web_ui.CarrierListView
CarrierUpdateView = _web_ui.CarrierUpdateView
HeadManagerDashboard = _web_ui.HeadManagerDashboard
HeadManagerAIInspectionView = _web_ui.HeadManagerAIInspectionView
HeadManagerFbsEquipmentCreateView = _web_ui.HeadManagerFbsEquipmentCreateView
HeadManagerFbsEquipmentLabelsView = _web_ui.HeadManagerFbsEquipmentLabelsView
HeadManagerFbsEquipmentUpdateView = _web_ui.HeadManagerFbsEquipmentUpdateView
HeadManagerFbsEmployeeReportExportView = _web_ui.HeadManagerFbsEmployeeReportExportView
HeadManagerFbsEmployeeReportView = _web_ui.HeadManagerFbsEmployeeReportView
HeadManagerEmployeeActivityDirectoryView = _web_ui.HeadManagerEmployeeActivityDirectoryView
HeadManagerEmployeeActivityDetailView = _web_ui.HeadManagerEmployeeActivityDetailView
HeadManagerFbsOperationsView = _web_ui.HeadManagerFbsOperationsView
HeadManagerFbsStaffView = _web_ui.HeadManagerFbsStaffView
HeadManagerFbsControllerPolicyView = _web_ui.HeadManagerFbsControllerPolicyView
HeadManagerFbsHandoverVerificationOverrideView = (
    _web_ui.HeadManagerFbsHandoverVerificationOverrideView
)
HeadManagerFbsComplianceOverrideView = _web_ui.HeadManagerFbsComplianceOverrideView
HeadManagerFbsSettingsView = _web_ui.HeadManagerFbsSettingsView
HeadManagerFbsWavePolicyUpdateView = _web_ui.HeadManagerFbsWavePolicyUpdateView
HeadManagerFbsQueuePriorityUpdateView = _web_ui.HeadManagerFbsQueuePriorityUpdateView
HeadManagerWarehouseLocationsView = _web_ui.HeadManagerWarehouseLocationsView
HeadManagerWarehouseLocationUpdateView = (
    _web_ui.HeadManagerWarehouseLocationUpdateView
)
HeadManagerFbsClientSettingsView = _web_ui.HeadManagerFbsClientSettingsView
HeadManagerFbsClientSettingsUpdateView = _web_ui.HeadManagerFbsClientSettingsUpdateView
HeadManagerFbsStoragePolicyUpdateView = _web_ui.HeadManagerFbsStoragePolicyUpdateView
HeadManagerFbsReplenishmentPolicyUpdateView = _web_ui.HeadManagerFbsReplenishmentPolicyUpdateView
HeadManagerMarkingSearchView = _web_ui.HeadManagerMarkingSearchView
HeadManagerClientsView = _web_ui.HeadManagerClientsView
HeadManagerContainerReportExportView = _web_ui.HeadManagerContainerReportExportView
HeadManagerContainerReportView = _web_ui.HeadManagerContainerReportView
HeadManagerMovementReportExportView = _web_ui.HeadManagerMovementReportExportView
HeadManagerMovementReportView = _web_ui.HeadManagerMovementReportView
HeadManagerReportCategoryView = _web_ui.HeadManagerReportCategoryView
HeadManagerReportDetailView = _web_ui.HeadManagerReportDetailView
HeadManagerReportsView = _web_ui.HeadManagerReportsView
HeadManagerRequestsView = _web_ui.HeadManagerRequestsView
HeadManagerSkuMovementReportExportView = _web_ui.HeadManagerSkuMovementReportExportView
HeadManagerSkuMovementReportView = _web_ui.HeadManagerSkuMovementReportView
HeadManagerStockEditorView = _web_ui.HeadManagerStockEditorView
HeadManagerReferenceMixin = _web_ui.HeadManagerReferenceMixin
MarketplaceWarehousesSyncView = _web_ui.MarketplaceWarehousesSyncView
MarketplaceWarehousesView = _web_ui.MarketplaceWarehousesView
OwnCompanyCreateView = _web_ui.OwnCompanyCreateView
OwnCompanyListView = _web_ui.OwnCompanyListView
OwnCompanyUpdateView = _web_ui.OwnCompanyUpdateView

_extract_value = _web_ui._extract_value
_fetch_ozon_clusters = _web_ui._fetch_ozon_clusters
_fetch_ozon_warehouses = _web_ui._fetch_ozon_warehouses
_fetch_wb_warehouses = _web_ui._fetch_wb_warehouses
_find_agency = _web_ui._find_agency
_format_address_line = _web_ui._format_address_line
_guess_marketplace_warehouse_type = _web_ui._guess_marketplace_warehouse_type
_load_marketplace_warehouses = _web_ui._load_marketplace_warehouses
_marketplace_warehouses_path = _web_ui._marketplace_warehouses_path
_normalize_lines = _web_ui._normalize_lines
_normalize_marketplace_warehouse_rows = _web_ui._normalize_marketplace_warehouse_rows
_normalize_ozon_client_id = _web_ui._normalize_ozon_client_id
_ozon_headers = _web_ui._ozon_headers
_ozon_post = _web_ui._ozon_post
_parse_items_payload = _web_ui._parse_items_payload
_parse_ozon_clusters = _web_ui._parse_ozon_clusters
_save_marketplace_warehouses = _web_ui._save_marketplace_warehouses
_sync_marketplace_warehouses = _web_ui._sync_marketplace_warehouses
_warehouse_display_line = _web_ui._warehouse_display_line
_warehouse_row_from_legacy_line = _web_ui._warehouse_row_from_legacy_line
