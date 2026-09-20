from django.contrib import admin

from .models import (
    WmsNewAcceptance,
    WmsNewBox,
    WmsNewBoxItem,
    WmsNewEvent,
    WmsNewExtraFieldDefinition,
    WmsNewExtraFieldValue,
    WmsNewInventoryLine,
    WmsNewInventoryLock,
    WmsNewInventoryScan,
    WmsNewInventorySession,
    WmsNewMarkingCode,
    WmsNewOrder,
    WmsNewOrderItem,
    WmsNewProduct,
    WmsNewRecord,
    WmsNewSyncRun,
    WmsNewTask,
    WmsNewWave,
    WmsNewWaveOrder,
)


for model in (
    WmsNewAcceptance,
    WmsNewBox,
    WmsNewBoxItem,
    WmsNewExtraFieldDefinition,
    WmsNewExtraFieldValue,
    WmsNewInventorySession,
    WmsNewInventoryLine,
    WmsNewInventoryScan,
    WmsNewInventoryLock,
    WmsNewMarkingCode,
    WmsNewOrder,
    WmsNewOrderItem,
    WmsNewWave,
    WmsNewWaveOrder,
    WmsNewTask,
    WmsNewProduct,
    WmsNewRecord,
    WmsNewEvent,
    WmsNewSyncRun,
):
    admin.site.register(model)
