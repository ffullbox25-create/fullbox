"""Read-only explanations for controllers; never determines print permission."""
from .models import FbsMarketplaceMetadataTransfer as Transfer
from .services.traceability import marketplace_metadata_transfer_resolved


def handover_metadata_issues(orders):
    """Explain rejected transfers, including optional KIZ already sent to WB."""
    issues = []
    seen_orders = set()
    for order in orders:
        if order.pk in seen_orders:
            continue
        seen_orders.add(order.pk)
        for item in order.items.all():
            for transfer in item.metadata_transfers.all():
                if marketplace_metadata_transfer_resolved(transfer):
                    continue
                if transfer.status not in {
                    Transfer.STATUS_FAILED, Transfer.STATUS_CONFLICT,
                    Transfer.STATUS_UNSUPPORTED, Transfer.STATUS_CANCELED,
                }:
                    continue
                kind = "КИЗ" if transfer.metadata_type == Transfer.TYPE_MARKING_CODE else "Срок годности"
                code = str(transfer.external_status or "").strip().casefold()
                if code == "wb_rejected:sgtininvalidformat":
                    reason = "WB отклонил КИЗ: неверный формат кода."
                    action = "На рабочем столе контролёра повторно отсканируйте Data Matrix с товара. Если ошибка повторится, передайте заказ ответственному за маркировку."
                elif code == "wb_rejected:sgtinnotfound":
                    reason = "WB отклонил КИЗ: код не найден."
                    action = "Сверьте Data Matrix на товаре и повторно отсканируйте его на рабочем столе контролёра. Если код верный, обратитесь к ответственному за маркировку для проверки регистрации кода."
                elif transfer.status == Transfer.STATUS_UNSUPPORTED:
                    reason = f"{kind}: передача не поддерживается API площадки."
                    action = "Передайте ошибку менеджеру или поддержке для проверки требований площадки. Не подтверждайте код вручную."
                elif transfer.status == Transfer.STATUS_CANCELED:
                    reason = f"{kind}: передача отменена, подтверждения площадки нет."
                    action = "Откройте проверку заказа и уточните причину отмены у менеджера перед повторной передачей."
                else:
                    reason = f"{kind}: площадка не подтвердила передачу."
                    action = "Проверьте данные заказа на рабочем столе контролёра. Если причина неясна, передайте ответ площадки менеджеру или поддержке."
                issues.append({
                    "order_number": order.external_order_id,
                    "order_id": order.pk, "item_id": item.pk,
                    "metadata_type": transfer.metadata_type,
                    "transfer_status": transfer.status,
                    "reason": reason,
                    "action": action,
                    "technical": str(transfer.last_error or transfer.external_status or ""),
                })
    return issues
