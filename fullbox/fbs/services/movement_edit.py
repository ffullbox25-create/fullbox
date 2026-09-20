"""Manager edits of submitted FBS movements, before warehouse planning."""
from collections import defaultdict

from django.core.exceptions import ValidationError
from billing.permissions import filter_agencies_for_user
from sku.models import Agency
from sklad.services.warehouse_write_path import WarehouseWritePathService
from ..models import FbsClientMovementRequest
from .client_movements import MANAGER_ROLES, _actor_role, _require_actor_role


def can_edit_movement(item, user):
    return bool(item.uses_hard_reserve and item.status == FbsClientMovementRequest.STATUS_SUBMITTED
                and not item.warehouse_accepted_at and not item.actual_moved_qty and not item.actual_moved_box_count
                and _actor_role(user) in MANAGER_ROLES and not item.replenishment_plans.exists())


def lock_movement_for_edit(*, agency, request_id, user, expected_updated_at):
    _require_actor_role(user, MANAGER_ROLES, "Редактировать FBS-заявку может только менеджер.")
    if not filter_agencies_for_user(Agency.objects.filter(pk=agency.pk), user).exists():
        raise ValidationError("Нет доступа к заявкам этого клиента.")
    item = FbsClientMovementRequest.objects.select_for_update().get(pk=request_id, agency=agency)
    if not can_edit_movement(item, user) or item.actual_moved_box_count:
        raise ValidationError("Редактировать можно только заявку, ожидающую менеджера, до передачи на склад.")
    if not expected_updated_at or item.updated_at.isoformat() != str(expected_updated_at):
        raise ValidationError("Заявка уже изменена в другом окне. Обновите страницу и проверьте состав перед сохранением.")
    return item


def movement_edit_initial(item):
    """Reconstruct physical box multiplicities, including mixed/unequal boxes."""
    lines = list(item.lines.order_by('id'))
    if item.mode == FbsClientMovementRequest.MODE_ITEM:
        rows = [{'barcode':row.barcode,'sku_code':row.sku_code,'product_name':row.product_name,
                 'qty':row.requested_qty,'units_per_box':1} for row in lines]
        mixed_codes = []
    else:
        allocations = WarehouseWritePathService.fbs_movement_reserve_allocations(agency=item.agency,request_id=item.pk)
        containers = defaultdict(lambda: defaultdict(int))
        by_id = {line.pk: line for line in lines}
        for row in allocations:
            line = by_id.get(row['request_line_id'])
            if line is None:
                raise ValidationError("Резерв не соответствует составу заявки. Обновите страницу.")
            containers[str(row.get('container_code') or '')][line.barcode] += int(row.get('qty') or 0)
        mixed_codes = [code for code, values in containers.items() if len(values)>1]
        plan = defaultdict(int)
        for code, values in containers.items():
            if code in mixed_codes:
                continue
            for barcode, qty in values.items():
                plan[(barcode, qty)] += qty
        by_barcode={row.barcode:row for row in lines}
        rows=[]
        for (barcode, units), qty in plan.items():
            line=by_barcode.get(barcode)
            rows.append({'barcode':barcode,'qty':qty,'units_per_box':units,
                         'sku_code':line.sku_code if line else '', 'product_name':line.product_name if line else ''})
        if not allocations:
            rows=[{'barcode':row.barcode,'qty':row.requested_qty,'units_per_box':row.units_per_box,
                   'sku_code':row.sku_code,'product_name':row.product_name} for row in lines]
    return {'rows':rows,'mixed_codes':mixed_codes,'comment':item.comment,
            'requested_box_count':item.requested_box_count,'requested_mixed_box_count':item.requested_mixed_box_count,
            'version':item.updated_at.isoformat()}
