"""Read-only presentation for the controller's shipment work queue."""
from collections import defaultdict
from types import SimpleNamespace

from django.db.models import Q, Exists, OuterRef, Count
from django.urls import reverse
from .models import (FbsHandoverBatch as Batch, FbsControllerCheckTote as Check,
                     FbsPickRestockRequest as Restock, FbsHandoverOrderAssignment as Assignment,
                     FbsMarketplaceMetadataTransfer as Transfer)


def supply_label_ready(batch):
    # Ozon's shipment label is generated locally by the existing print service.
    if getattr(getattr(batch, 'profile', None), 'marketplace', None) == 'ozon':
        return batch.status == Batch.STATUS_READY
    return batch.marketplace_state == Batch.MARKETPLACE_COMPLETE and bool(batch.supply_label_file)


def shipment_sent(batch):
    """Return whether verified goods were handed to the delivery flow."""
    return batch.status in {
        Batch.STATUS_DISPATCHED, Batch.STATUS_ACCEPTED,
    } or bool(getattr(batch, 'dispatched_at', None))


def controller_shipment_stage(batch):
    """Describe the controller-facing physical stage of one shipment."""
    if batch.status == Batch.STATUS_ARCHIVED:
        return SimpleNamespace(
            key='archived', label='В архиве',
            hint='Отгрузка закрыта для новых операций.',
        )
    if batch.status == Batch.STATUS_ACCEPTED:
        return SimpleNamespace(
            key='accepted', label='Принято маркетплейсом',
            hint='Маркетплейс подтвердил фактическую приёмку отгрузки.',
        )
    if shipment_sent(batch):
        return SimpleNamespace(
            key='transit', label='В пути',
            hint='Проверка завершена. Отгрузка автоматически передана в доставку.',
        )
    if batch.status == Batch.STATUS_PROBLEM:
        return SimpleNamespace(
            key='problem', label='Требуется действие',
            hint='Есть ошибка передачи или проверки. Откройте причину.',
        )
    if batch.status == Batch.STATUS_READY:
        if supply_label_ready(batch):
            return SimpleNamespace(
                key='ready', label='Готово к отгрузке',
                hint='Проверка завершена. Печать ШК — отдельное внутреннее действие.',
            )
        return SimpleNamespace(
            key='checked', label='Проверено',
            hint='Проверка завершена. Ожидается ШК поставки от маркетплейса.',
        )
    if batch.status == Batch.STATUS_OPEN:
        return SimpleNamespace(
            key='checking', label='Проверяется',
            hint='Завершите проверку состава и коробов.',
        )
    return SimpleNamespace(
        key='new', label='Новая', hint='Отгрузка ожидает начала проверки.',
    )


# The same physical stages drive both role views, their filters and counters.
SHIPMENT_STAGE_CHOICES = (
    ('checking', 'Проверяется'), ('checked', 'Проверено'),
    ('ready', 'Готово к отгрузке'), ('transit', 'В пути'),
    ('accepted', 'Принято маркетплейсом'), ('problem', 'Требуется действие'),
    ('archived', 'В архиве'),
)


def shipment_stage_filters():
    archived = Q(status=Batch.STATUS_ARCHIVED)
    accepted = ~archived & Q(status=Batch.STATUS_ACCEPTED)
    transit = ~archived & ~accepted & (
        Q(status=Batch.STATUS_DISPATCHED) | Q(dispatched_at__isnull=False)
    )
    at_warehouse = ~archived & ~accepted & ~transit
    checked = at_warehouse & Q(status=Batch.STATUS_READY)
    ready = checked & (Q(profile__marketplace='ozon') | (
        Q(marketplace_state=Batch.MARKETPLACE_COMPLETE) & ~Q(supply_label_file='')
    ))
    return dict(
        archived=archived, accepted=accepted, transit=transit,
        checking=at_warehouse & Q(status=Batch.STATUS_OPEN),
        checked=checked & ~ready, ready=ready,
        problem=at_warehouse & Q(status=Batch.STATUS_PROBLEM),
        active=at_warehouse,
    )


def shipment_stage_counts(queryset=None):
    if queryset is None:
        queryset = Batch.objects.all()
    return queryset.aggregate(**{
        key: Count('pk', filter=condition, distinct=True)
        for key, condition in shipment_stage_filters().items()
    })


def controller_queue(queryset, request, workstation):
    """Filter only the controller's GET view; preserve all records and permissions."""
    live_return = Restock.objects.filter(handover_assignment__batch_id=OuterRef('pk')).filter(
        Q(source_tote__isnull=True, status__in=['waiting_marketplace','queued','failed']) | Q(status='failed'))
    error = Transfer.objects.filter(order_item__order__handover_assignment__batch_id=OuterRef('pk'),
        status__in=['failed','conflict','unsupported','canceled'])
    queryset = queryset.annotate(controller_return_issue=Exists(live_return),controller_metadata_issue=Exists(error))
    scope = request.GET.get('scope') or ('all' if any(request.GET.get(k) for k in ['q','status','agency_id','marketplace']) else 'work')
    scope = {'waiting': 'checked', 'sent': 'transit'}.get(scope, scope)
    if scope not in {'work','attention','checked','ready','transit','accepted','all'}: scope='work'
    mine_checks = (Check.objects.filter(session__workstation=workstation) if workstation is not None
                   else Check.objects.filter(session__controller=request.user))
    mine=queryset.filter(pk__in=mine_checks.values('handover_batch_id'))
    stages = shipment_stage_filters()
    accepted = stages['accepted']
    transit = stages['transit']
    physically_left = transit | accepted
    issue = Q(controller_return_issue=True) | (~physically_left & (Q(controller_metadata_issue=True) | Q(marketplace_state=Batch.MARKETPLACE_ERROR) | Q(status=Batch.STATUS_PROBLEM)))
    active = Q(status__in=[Batch.STATUS_OPEN,Batch.STATUS_READY]) & ~physically_left
    ready = stages['ready']
    checked = stages['checked']
    sets={'work':mine.filter(active | issue), 'attention':mine.filter(issue),
          'checked':mine.filter(checked), 'ready':mine.filter(ready),
          'transit':mine.filter(transit), 'accepted':mine.filter(accepted),
          'all':queryset}
    labels={'work':'В работе моего стола','attention':'Нужны действия',
            'checked':'Проверено','ready':'Готово к отгрузке',
            'transit':'В пути','accepted':'Принято маркетплейсом',
            'all':'Все отгрузки'}
    tabs=[]
    for key, rows in sets.items():
        query=request.GET.copy();query['scope']=key;query.pop('page',None);query.pop('status',None)
        tabs.append(dict(key=key,label=labels[key],count=rows.count(),selected=scope==key,url='?'+query.urlencode()))
    return sets[scope],dict(controller_queue_scope=scope,controller_queue_tabs=tabs,
                           controller_queue_label=labels[scope],controller_queue_workstation=workstation)


def decorate_controller_shipments(batches):
    batch_ids = [batch.pk for batch in batches]
    return_rows = list(
        Restock.objects.filter(
            handover_assignment__batch_id__in=batch_ids
        )
        .filter(
            Q(
                source_tote__isnull=True,
                status__in=['waiting_marketplace', 'queued', 'failed'],
            )
            | Q(status='failed')
        )
        .values(
            'handover_assignment__batch_id',
            'order__external_order_id',
            'planned_qty',
            'status',
            'reason',
        )
        .order_by('created_at', 'id')
    )
    returns_by_batch = defaultdict(list)
    for row in return_rows:
        returns_by_batch[row['handover_assignment__batch_id']].append(row)
    for batch in batches:
        sent=shipment_sent(batch)
        stage=controller_shipment_stage(batch)
        batch.movement_stage=stage
        batch.controller_sent=sent
        batch.controller_stage_key=stage.key
        batch.controller_action_url=reverse('fbs:tsd_handover_detail',kwargs={'batch_id':batch.pk})
        batch.controller_state=stage.label
        batch.controller_hint=stage.hint
        batch.controller_action='Продолжить проверку'
        batch.controller_errors=[]
        batch.controller_error_quantity=0
        for row in returns_by_batch.get(batch.pk, []):
            quantity = int(row['planned_qty'] or 0)
            batch.controller_errors.append({
                'title': f"Отменённый заказ {row['order__external_order_id']}",
                'reason': str(row['reason'] or 'Требуется возврат товара.').strip(),
                'quantity': quantity,
            })
            batch.controller_error_quantity += quantity
        order_qty_by_number = {}
        for assignment in getattr(batch, 'list_assignments', []):
            order_number = assignment.order.external_order_id
            order_qty_by_number[order_number] = sum(
                int(item.quantity or 0) for item in assignment.order.items.all()
            )
            if assignment.status == Assignment.STATUS_ERROR:
                quantity = order_qty_by_number[order_number]
                batch.controller_errors.append({
                    'title': f'Ошибка заказа {order_number}',
                    'reason': str(assignment.error or 'Маркетплейс не подтвердил заказ.').strip(),
                    'quantity': quantity,
                })
                batch.controller_error_quantity += quantity
        for issue in getattr(batch, 'operator_metadata_issues', []):
            order_number = issue['order_number']
            quantity = int(order_qty_by_number.get(order_number, 0))
            batch.controller_errors.append({
                'title': f'Ошибка данных заказа {order_number}',
                'reason': str(issue['reason'] or 'Данные товара не подтверждены.').strip(),
                'quantity': quantity,
            })
            batch.controller_error_quantity += quantity
        if not batch.controller_errors and getattr(batch, 'command_problem_count', 0):
            batch.controller_errors.append({
                'title': 'Ошибка обмена с маркетплейсом',
                'reason': f'Неуспешных команд: {batch.command_problem_count}. Откройте отгрузку для просмотра ответа площадки.',
                'quantity': 0,
            })
        if not batch.controller_errors and (
            batch.status == Batch.STATUS_PROBLEM
            or batch.marketplace_state == Batch.MARKETPLACE_ERROR
        ):
            batch.controller_errors.append({
                'title': 'Ошибка отгрузки',
                'reason': 'Маркетплейс не подтвердил передачу или проверку. Откройте отгрузку для просмотра причины.',
                'quantity': 0,
            })
        batch.controller_error_count=len(batch.controller_errors)
        if batch.status == Batch.STATUS_ARCHIVED:
            batch.controller_action='Посмотреть результат'
            continue
        if sent:
            batch.controller_action='Посмотреть результат'
        elif stage.key == 'ready':
            batch.controller_action='Открыть и распечатать ШК'
        elif stage.key == 'checked':
            batch.controller_action='Проверить готовность ШК'
        if returns_by_batch.get(batch.pk):
            batch.controller_stage_key='problem'
            batch.controller_hint='Остались незавершённые возвраты.' if sent else 'Нужно завершить действия по отменённым заказам.'
            batch.controller_action=(
                'Открыть возвраты'
                if sent
                else f'Решить проблемы · {batch.controller_error_count}'
            )
            batch.controller_action_url+='?problems=1'
        elif sent and batch.status == Batch.STATUS_PROBLEM:
            batch.controller_hint='Отгрузка в пути. Есть зарегистрированная проблема — проверьте ответ площадки.'
            batch.controller_action='Проверить проблему'
        elif batch.operator_metadata_issues and not sent:
            batch.controller_stage_key='problem'
            batch.controller_state='Нужно разобрать ошибку данных'
            batch.controller_hint='; '.join(f"Заказ {i['order_number']}: {i['reason']}" for i in batch.operator_metadata_issues[:2])
            batch.controller_action=f'Разобрать ошибки · {len(batch.operator_metadata_issues)}'
            batch.controller_action_url+='?problems=1'
        elif not sent and batch.metadata_unconfirmed_count:
            batch.controller_hint=f'Данные без подтверждения: {batch.metadata_unconfirmed_count}. Откройте причину.'
            batch.controller_action='Проверить данные'
        elif not sent and batch.assigned_order_count > batch.boxed_order_count:
            batch.controller_hint=f'В коробе {batch.boxed_order_count} из {batch.assigned_order_count} заказов. Осталось {batch.assigned_order_count-batch.boxed_order_count}.'
        elif not sent and batch.has_problem:
            batch.controller_stage_key='problem'
            batch.controller_hint='Есть ошибка передачи или проверки. Откройте причину.'
            batch.controller_action=f'Разобрать ошибки · {batch.controller_error_count}'
    return batches


def check_progress(check_tote, orders):
    total=Assignment.objects.filter(batch_id=check_tote.handover_batch_id).exclude(status=Assignment.STATUS_CANCELED).count() if check_tote.handover_batch_id else len(orders)
    total=max(total,len(orders))
    pending=[]
    for tote in check_tote.pick_totes.exclude(status='closed').select_related('tote','pick_batch').order_by('id'):
        pending.append(dict(name=tote.tote.name,barcode=tote.tote.barcode,
            remaining=max(0,tote.planned_qty-tote.processed_qty),
            empty_confirmation=tote.status=='awaiting_empty',
            url=(reverse('fbs:controller_home') if tote.status=='awaiting_empty' else reverse('fbs:tsd_pick_verification',kwargs={'batch_id':tote.pick_batch_id}))))
    return dict(controller_total_orders=total,controller_remaining_orders=max(0,total-len(orders)),controller_pending_pick_totes=pending)
