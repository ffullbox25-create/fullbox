"""Read-only controller instructions based on the existing shipment checks."""
import hashlib
import json
from django.urls import reverse
from .models import (FbsControllerCheckTote, FbsControllerSession, FbsHandoverBatch,
                     FbsOrderLabel, FbsOrderStockAllocation, FbsPickRestockLine,
                     FbsPickBatch, FbsPickRestockRequest, FbsPickTask,
                     FbsIntegrationProfile)


def controller_problem_context(batch, summary, actor):
    from .controller_shipment_ui import shipment_sent
    sent = shipment_sent(batch)
    editable = batch.status in {FbsHandoverBatch.STATUS_OPEN, FbsHandoverBatch.STATUS_READY} and not sent and batch.marketplace_state != FbsHandoverBatch.MARKETPLACE_DELIVERY_PENDING
    marketplace = "WB" if batch.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB else "маркетплейсом"
    cards = []
    seen = set()
    active_returns = {FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE,
                      FbsPickRestockRequest.STATUS_FAILED, FbsPickRestockRequest.STATUS_QUEUED,
                      FbsPickRestockRequest.STATUS_IN_PROGRESS}
    assignments = summary['handover_assignments']
    requests = summary['handover_exclusion_requests']
    issues_by_order = {}
    for issue in summary.get('handover_operator_metadata_issues', []):
        issues_by_order.setdefault(issue['order_number'], []).append(issue)
    orders = {a.order_id: a.order for a in assignments}
    orders.update({r.order_id: r.order for r in requests})
    labels = {}
    for label in FbsOrderLabel.objects.filter(order_id__in=orders).order_by('requested_at', 'id'):
        labels[label.order_id] = label
    tasks = FbsPickTask.objects.filter(order_id__in=orders).select_related('batch').order_by('id')
    tasks = list(tasks)
    workstation_by_order = {task.order_id: task.batch.workstation_id for task in tasks}
    batch_by_order = {task.order_id: task.batch_id for task in tasks}
    scan_task_by_order = {
        task.order_id: task for task in tasks
        if task.status == FbsPickTask.STATUS_PICKED
        and task.batch.status in {FbsPickBatch.STATUS_VERIFICATION, FbsPickBatch.STATUS_DONE}
        and task.batch.picking_completed_at is not None
    }
    restock_lines = {}
    for line in FbsPickRestockLine.objects.filter(
        request_id__in=[request.id for request in requests]
    ).select_related(
        'allocation__order_item', 'allocation__balance', 'allocation__traceability'
    ).order_by('id'):
        restock_lines.setdefault(line.request_id, []).append(line)
    allocations_by_task = {}
    for allocation in FbsOrderStockAllocation.objects.filter(
        pick_task_id__in=[task.id for task in scan_task_by_order.values()],
        status=FbsOrderStockAllocation.STATUS_PICKED,
        qty_picked__gt=0,
    ).select_related('order_item', 'balance', 'traceability').order_by('id'):
        allocations_by_task.setdefault(allocation.pick_task_id, []).append(allocation)
    sessions = {s.workstation_id: s for s in FbsControllerSession.objects.filter(
        controller=actor, status=FbsControllerSession.STATUS_ACTIVE,
    ).select_related('canceled_tote', 'problem_tote', 'workstation')}

    def card(order, kind, reason, instruction):
        label = labels.get(order.pk)
        return dict(order_id=order.pk, order_number=order.external_order_id,
                    sticker=(label.external_label_id or label.barcode) if label else '',
                    order_barcode=label.barcode if label else '', kind=kind, reason=reason,
                    instruction=instruction, quantity=getattr(order, 'unit_count', None),
                    retry_items=[], actionable=False, label_actionable=False,
                    product_scan_actionable=False, product_scan_rows=[])

    def product_scan_rows(order_id, restock=None):
        units = []
        if restock is not None:
            source_rows = [
                (line.allocation, int(line.planned_qty or 0))
                for line in restock_lines.get(restock.id, [])
            ]
        else:
            task = scan_task_by_order.get(order_id)
            source_rows = [
                (allocation, int(allocation.qty_picked or 0))
                for allocation in allocations_by_task.get(getattr(task, 'id', None), [])
            ]
        for allocation, quantity in source_rows:
            traceability = getattr(allocation, 'traceability', None)
            requires_marking = bool(
                str(getattr(traceability, 'marking_code', '') or '').strip()
                or str(allocation.balance.marking_code or '').strip()
            )
            item = allocation.order_item
            for _unit in range(quantity):
                units.append(dict(
                    allocation_id=allocation.id,
                    name=item.product_name or allocation.balance.name or item.external_sku,
                    barcode=allocation.balance.barcode or item.barcode,
                    requires_marking=requires_marking,
                ))
        for scan_no, unit in enumerate(units, start=1):
            unit['scan_no'] = scan_no
            unit['next_scan_no'] = scan_no + 1 if scan_no < len(units) else None
        return units

    for restock in requests:
        if restock.status not in active_returns:
            continue
        if restock.source_tote_id is not None:
            if restock.status in {FbsPickRestockRequest.STATUS_QUEUED, FbsPickRestockRequest.STATUS_IN_PROGRESS}:
                seen.add(restock.order_id)
                continue
            row = card(restock.order, 'waiting', 'Товар уже в служебной таре',
                       f'Повторно перекладывать и сканировать не нужно. Возврат ожидает сверки с {marketplace}.')
            if restock.status == FbsPickRestockRequest.STATUS_FAILED:
                row['instruction'] = 'Сверка возврата завершилась с ошибкой. Передайте её руководителю смены. Повторный физический скан не нужен.'
            row['technical'] = getattr(restock, 'last_error', '') or getattr(restock, 'reason', '')
        else:
            row = card(restock.order, 'cancel', 'Нужно подтвердить тару возврата' if sent else 'Заказ нужно убрать из текущей отгрузки',
                       'Найдите весь товар заказа. Используйте этикетку заказа, а если её физически нет — проверьте каждый товар. Затем отсканируйте QR назначенной тары.')
            session = sessions.get(restock.batch.workstation_id)
            scan_rows = product_scan_rows(restock.order_id, restock)
            base_actionable = bool(batch.status in {'open','ready'} and session and session.canceled_tote_id)
            row.update(
                tote=session.canceled_tote if session else None,
                request_id=restock.id,
                quantity=restock.planned_qty,
                label_actionable=bool(base_actionable and row['order_barcode']),
                product_scan_actionable=bool(base_actionable and scan_rows),
                product_scan_rows=scan_rows,
                product_scan_total=len(scan_rows),
            )
            row['actionable'] = row['label_actionable'] or row['product_scan_actionable']
            if not row['actionable']:
                row['instruction'] = 'Для этого заказа нужна активная смена его стола с привязанной тарой и сохранённый состав отобранного товара. Обратитесь к руководителю смены.'
        row['after_delivery'] = sent
        if sent:
            row['instruction'] = 'Поставка уже передана. Это отдельный незавершённый возврат. ' + row['instruction']
        row['status'] = restock.status
        cards.append(row); seen.add(restock.order_id)

    for assignment in assignments:
        order = assignment.order
        if order.pk in seen:
            continue
        if assignment.client_canceled and editable:
            row = card(order, 'cancel', 'Заказ отменён покупателем',
                       'Используйте этикетку заказа, а если её физически нет — проверьте каждый товар. Переложите весь заказ в тару отменённых заказов и отсканируйте QR тары.')
            session = sessions.get(workstation_by_order.get(order.pk))
            scan_rows = product_scan_rows(order.pk)
            base_actionable = bool(batch.status in {'open','ready'} and session and session.canceled_tote_id)
            row.update(
                tote=session.canceled_tote if session else None,
                label_actionable=bool(base_actionable and row['order_barcode']),
                product_scan_actionable=bool(base_actionable and scan_rows),
                product_scan_rows=scan_rows,
                product_scan_total=len(scan_rows),
            )
            row['actionable'] = row['label_actionable'] or row['product_scan_actionable']
            cards.append(row); continue
        retries = []
        canonical_issues = issues_by_order.get(order.external_order_id, [])
        reasons = [issue['reason'] for issue in canonical_issues]
        retry_blocks = []
        waiting = False
        missing = []
        for item in getattr(order, 'ui_items', []):
            for check in item.metadata_checks:
                if editable and check.can_retry and check.transfer is not None and check.transfer.status in {'failed', 'conflict'}:
                    retries.append(dict(id=item.pk, name=item.product_name or item.external_sku,
                                        barcode=item.barcode))
                if check.transfer is not None and check.transfer.status in {'failed','conflict','unsupported','canceled'} and getattr(check,'retry_block_reason',''):
                    retry_blocks.append(check.retry_block_reason)
                if check.state == 'problem':
                    reasons.append(f'{check.label}: {check.state_label}')
                elif check.required and check.state == 'in_progress':
                    if check.transfer is None:
                        missing.append(check.label)
                    else:
                        waiting = True
        if retries or reasons or assignment.invalid_kiz_route_available:
            row = card(order, 'marking', 'Нужно проверить маркировку товара',
                       'Сначала повторно отсканируйте Data Matrix с товара. Если WB окончательно отклоняет код, уберите товар в проблемную тару по одному из доступных маршрутов.')
            row.update(retry_items=retries, problem_reasons=list(dict.fromkeys(i['reason'] for i in canonical_issues)), technical='; '.join(reasons),
                       can_reroute=editable and assignment.can_reroute_invalid_kiz,
                       problem_tote=assignment.invalid_kiz_problem_tote,
                       actionable=bool(retries or (editable and assignment.can_reroute_invalid_kiz)))
            if not row['actionable']:
                row['instruction'] = 'Для текущего состояния заказа повторный скан недоступен. Передайте заказ руководителю смены для сверки с маркетплейсом.'
                if retry_blocks:
                    row['technical'] += '; ' + ' '.join(dict.fromkeys(retry_blocks))
            if sent:
                row['instruction'] = 'Поставка уже передана. Ошибка сохранена для разбора руководителем смены; пересканировать переданный товар здесь нельзя.'
            cards.append(row)
        elif missing and editable:
            row = card(order, 'manual', 'Нужно записать данные товара',
                       'Не заполнено: ' + ', '.join(missing) + '. Откройте проверку товара, отсканируйте код или укажите требуемые данные.')
            pick_batch_id = batch_by_order.get(order.pk)
            if pick_batch_id:
                row.update(verification_url=reverse('fbs:tsd_pick_verification', kwargs={'batch_id':pick_batch_id}), actionable=True)
            cards.append(row)
        elif waiting and editable:
            cards.append(card(order, 'waiting', 'Ожидается проверка данных',
                              'Данные уже обрабатываются маркетплейсом. Повторно сканировать без сообщения об ошибке не нужно.'))
        elif editable and assignment.check_state == 'problem' and assignment.check_label != 'Не проверен':
            cards.append(card(order, 'manual', assignment.check_label,
                              assignment.check_detail or 'Откройте сведения заказа и передайте причину руководителю смены.'))
    displayed = {row['order_number'] for row in cards} | {orders[pk].external_order_id for pk in seen if pk in orders}
    for number, issues in issues_by_order.items():
        if number in displayed:
            continue
        issue = issues[0]
        cards.append(dict(order_id=issue.get('order_id',number),order_number=number,
            kind='manual',reason='Нужно проверить данные заказа',sticker='',retry_items=[],actionable=False,
            instruction='Поставка уже передана. Передайте сохранённую ошибку руководителю смены.' if sent else 'Передайте заказ руководителю смены: ошибка есть в составе поставки, доступного действия по заказу нет.',
            technical='; '.join(i['reason'] for i in issues)))
    cards.sort(key=lambda row: (not row['actionable'], row['kind'] != 'cancel', str(row['order_id'])))
    check = FbsControllerCheckTote.objects.filter(handover_batch=batch).order_by('-id').first()
    fingerprint = hashlib.sha256(json.dumps([
        batch.status, batch.marketplace_state, check.status if check else '',
        [(row['order_id'], row['kind'], row['actionable'], row.get('label_actionable'),
          row.get('product_scan_actionable'), row.get('product_scan_total'),
          row.get('status'), row.get('technical'),
          [item['id'] for item in row['retry_items']]) for row in cards],
    ], sort_keys=True).encode()).hexdigest()[:20]
    instructions = {row['order_number']: row['instruction'] for row in cards}
    visible_issues = [dict(issue, action=instructions.get(issue['order_number'], issue['action']))
                      for issue in summary.get('handover_operator_metadata_issues', [])]
    return dict(controller_operator_metadata_issues=visible_issues, controller_delivery_sent=sent, controller_problem_auto_open=bool(editable and cards),
                controller_problems=cards, controller_problem_count=len(cards),
                controller_problem_action_count=sum(row['actionable'] for row in cards),
                controller_problem_fingerprint=fingerprint,
                controller_problem_url=reverse('fbs:tsd_handover_detail', kwargs={'batch_id':batch.id}),
                controller_problem_check_url=(reverse('fbs:controller_check_tote', kwargs={'check_tote_id':check.id}) if check else ''),
                controller_problem_batch_id=batch.id)
