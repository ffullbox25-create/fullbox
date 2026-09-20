"""Unallocated FBS demand. Physical sources are chosen only at warehouse acceptance.

Quantity reserves never modify snapshot counters. Allocation atomically replaces
them with the existing exact warehouse reservations; executing plans stay intact.
"""
from collections import Counter

from django.db import transaction
from django.conf import settings
from django.utils import timezone

from sku.models import Agency
from sklad.models import WarehouseEvent, WarehouseReserve, WarehouseStockSnapshot

EVENT = 'fbs_movement_quantity_reserved'
CLOSED = ('released', 'canceled', 'satisfied')


def pool_reserves(agency_id=None, request_id=None):
    qs = WarehouseReserve.objects.filter(
        reserve_type=WarehouseReserve.TYPE_FBS_MOVEMENT,
        context_type='fbs_client_movement', events__event_type=EVENT,
    ).exclude(status__in=CLOSED).distinct()
    if agency_id is not None:
        qs = qs.filter(agency_id=agency_id)
    if request_id is not None:
        qs = qs.filter(context_id=str(request_id))
    return qs


def identity(row):
    from .stock_availability import normalize_goods_type
    def value(name, alternate=None):
        return row.get(name, row.get(alternate, '')) if isinstance(row, dict) else getattr(row, name, '')
    return (int(value('agency_id') or 0), str(value('sku_code', 'sku') or '').strip().casefold(),
            str(value('size') or '').strip().casefold(), str(value('barcode') or '').strip().casefold(),
            normalize_goods_type(value('goods_type')))


def source_queryset(agency_id):
    from fbs.goods_types import fbs_client_movement_source_stock_q
    qs = WarehouseStockSnapshot.objects.filter(
        fbs_client_movement_source_stock_q(agency_id=agency_id), agency_id=agency_id,
        is_archived=False, is_in_vehicle=False, qty__gt=0, active_operation__isnull=True,
    ).exclude(zone_code__iexact=getattr(settings,'FBS_ZONE_CODE','FBS'))
    if any(f.name == 'expiry_date' for f in WarehouseStockSnapshot._meta.fields):
        from django.db.models import Q
        qs = qs.filter(Q(expiry_date__isnull=True) | Q(expiry_date__gte=timezone.localdate()))
    return qs


def demand(agency_id, exclude_request_id=None):
    qs = pool_reserves(agency_id)
    if exclude_request_id is not None:
        qs = qs.exclude(context_id=str(exclude_request_id))
    result = Counter()
    for reserve in qs:
        result[identity(reserve)] += max(reserve.qty_reserved - reserve.qty_satisfied, 0)
    return result


def _limit_sources_to_keys(qs, required_keys):
    """Limit the stock source query without changing identity matching rules."""
    from django.db.models import Q

    key_filter = Q()
    has_filter = False
    for key in required_keys:
        identity_filter = Q()
        identity_has_filter = False
        if key[1]:
            identity_filter &= Q(sku_code__iexact=key[1])
            identity_has_filter = True
        if key[3]:
            identity_filter &= Q(barcode__iexact=key[3])
            identity_has_filter = True
        if identity_has_filter:
            key_filter |= identity_filter
            has_filter = True
    return qs.filter(key_filter) if has_filter else qs


def _availability_rows_for_sources(sources, agency_id):
    """Apply the shared reserve truth to an already narrowed source set."""
    from .stock_availability import _apply_reserve_truth_to_rows
    from .warehouse_stock_rows import normalize_stock_row_from_snapshot

    rows = []
    for source in sources:
        row = normalize_stock_row_from_snapshot(source)
        if row is not None:
            rows.append(row)
    rows = _apply_reserve_truth_to_rows(
        rows,
        reserve_type=WarehouseReserve.TYPE_PROCESSING,
        reserved_field='processing_reserved_qty',
        agency_id=agency_id,
    )
    return _apply_reserve_truth_to_rows(
        rows,
        reserve_type=WarehouseReserve.TYPE_SHIPPING,
        reserved_field='shipping_reserved_qty',
        agency_id=agency_id,
    )


def capacity(agency_id, exclude_request_id=None, required_keys=None):
    from .stock_availability import stock_rows_with_availability

    # Capacity checks usually concern one movement line. Restrict only the stock
    # read; identity matching and all processing/shipping/FBS reserve deductions
    # remain unchanged for the requested keys.
    required_keys = set(required_keys or ())
    source_qs = source_queryset(agency_id)
    if required_keys:
        source_qs = _limit_sources_to_keys(source_qs, required_keys)
    source_rows = list(source_qs.select_related(
        'agency', 'sku_ref', 'container', 'parent_container', 'location',
        'active_operation', 'last_event',
    ).order_by('created_at', 'id'))
    sources = {s.id: s for s in source_rows}
    amounts = Counter()
    if required_keys:
        availability_rows = _availability_rows_for_sources(source_rows, agency_id)
    else:
        availability_rows = stock_rows_with_availability(
            agency_id=agency_id,
            include_fbs_pool=False,
        )
    for row in availability_rows:
        source = sources.get(int(row.get('snapshot_id') or row.get('id') or 0))
        if source:
            amounts[identity(source)] += max(0, min(int(row.get('available_qty') or 0), source.available_qty, source.qty))
    for key, qty in demand(agency_id, exclude_request_id).items():
        if not required_keys or key in required_keys:
            amounts[key] -= qty
    return amounts


def available_by_barcode(agency_id):
    result = Counter()
    for key, qty in capacity(agency_id).items():
        result[key[3]] += max(qty, 0)
    return result


def apply_to_rows(rows, agency_id=None, protected_box_codes=None):
    """Display-only distribution; no physical box is reserved by this function."""
    result = [dict(row) for row in rows]
    protected_keys = {
        str(code or '').strip().casefold()
        for code in (protected_box_codes or [])
        if str(code or '').strip()
    }
    agencies = {int(row.get('agency_id') or 0) for row in result}
    for owner in agencies:
        pending = demand(owner)
        eligible = set(source_queryset(owner).values_list('id', flat=True))
        owner_rows = [row for row in result if int(row.get('agency_id') or 0) == owner]
        owner_rows.sort(key=lambda row: (
            str(row.get('box_code') or row.get('container_code') or '').strip().casefold()
            in protected_keys,
        ))
        for row in owner_rows:
            if int(row.get('snapshot_id') or row.get('id') or 0) not in eligible:
                continue
            key = identity(row)
            qty = min(max(int(row.get('available_qty') or 0), 0), max(pending[key], 0))
            row['available_qty'] = max(int(row.get('available_qty') or 0) - qty, 0)
            row['other_reserved_qty'] = int(row.get('other_reserved_qty') or 0) + qty
            row['fbs_quantity_reserved_qty'] = qty
            pending[key] -= qty
    return result


def assert_capacity(agency_id, required, exclude_request_id=None):
    # No intersecting demand means there is nothing to compare. In particular,
    # unrelated shipping/processing claims must not reload the agency's stock.
    # Keep nonempty mappings (including zero values) on the existing guard path.
    if not required:
        return
    from .warehouse_write_path import WarehouseTransitionError
    free = capacity(agency_id, exclude_request_id, required_keys=required)
    for key, qty in required.items():
        if free[key] < qty:
            raise WarehouseTransitionError(
                f'Для FBS-перемещения по ШК {key[3]} доступно {max(free[key], 0)} шт., '
                f'требуется {qty} шт. Учтены резервы отгрузки, обработки и FBS.'
            )


def quantity_allocations(reserves):
    result = []
    events = {e.reserve_id: e for e in WarehouseEvent.objects.filter(
        reserve_id__in=[r.id for r in reserves], event_type=EVENT).order_by('id')}
    for r in reserves:
        event = events.get(r.id)
        if event:
            result.append(dict(reserve_id=r.id, request_line_id=event.payload['request_line_id'],
                               snapshot_id=0, container_id=0, container_code='',
                               qty=max(r.qty_reserved-r.qty_satisfied, 0), reserve_scope='quantity'))
    return result


@transaction.atomic
def reserve_quantity(*, agency, request_id, allocations, created_by=None):
    from .warehouse_write_path import WarehouseTransitionError
    Agency.objects.select_for_update().get(pk=agency.pk)
    if WarehouseReserve.objects.filter(reserve_type='fbs_movement', context_id=str(request_id)).exclude(status__in=CLOSED).exists():
        raise WarehouseTransitionError('По заявке уже существует резерв FBS.')
    sources = {s.id: s for s in WarehouseStockSnapshot.objects.filter(id__in=[r['snapshot_id'] for r in allocations])}
    groups = {}
    needed = Counter()
    for row in allocations:
        source = sources[row['snapshot_id']]
        if source.agency_id != agency.id or int(row['qty']) <= 0:
            raise WarehouseTransitionError('Некорректный клиент или количество резерва FBS.')
        key = identity(source)
        needed[key] += int(row['qty'])
        group = groups.setdefault((row['request_line_id'], key), [source, 0])
        group[1] += int(row['qty'])
    assert_capacity(agency.id, needed)
    actor = created_by if getattr(created_by, 'is_authenticated', False) else None
    for (line_id, key), (source, qty) in groups.items():
        r = WarehouseReserve.objects.create(
            agency=agency, reserve_type='fbs_movement', context_type='fbs_client_movement',
            context_id=str(request_id), sku_ref_id=source.sku_ref_id, sku_code=source.sku_code,
            size=source.size, barcode=source.barcode, goods_type=source.goods_type,
            qty_reserved=qty, qty_allocated=0, status='active',
            source_document_type='fbs_movement', source_document_id=str(request_id), created_by=actor,
        )
        WarehouseEvent.objects.create(agency=agency, reserve=r, event_type=EVENT,
            stock_context_type='fbs_client_movement', stock_context_id=str(request_id),
            source_document_type='fbs_movement', source_document_id=str(request_id), qty=qty,
            occurred_at=timezone.now(), performed_by=actor,
            payload={'reserve_scope':'quantity','request_line_id':line_id})


def validate_pool(agency_id, request_id):
    rows = list(pool_reserves(agency_id, request_id))
    if rows:
        needed = Counter()
        for r in rows:
            needed[identity(r)] += max(r.qty_reserved-r.qty_satisfied, 0)
        assert_capacity(agency_id, needed, exclude_request_id=request_id)


def pool_coverage(agency_id, request_id):
    required = Counter()
    for r in pool_reserves(agency_id,request_id):
        required[identity(r)] += max(r.qty_reserved-r.qty_satisfied,0)
    if not required:
        return 0
    free = capacity(
        agency_id,
        exclude_request_id=request_id,
        required_keys=required,
    )
    return sum(min(qty,max(free[key],0)) for key,qty in required.items())


def release_pool(reserves, actor=None, reason=''):
    allocations = quantity_allocations(reserves)
    for reserve in reserves:
        reserve.status = 'released'
        reserve.released_by = actor if getattr(actor, 'is_authenticated', False) else None
        reserve.save(update_fields=['status','released_by','updated_at'])
        WarehouseEvent.objects.create(agency_id=reserve.agency_id, reserve=reserve,
            event_type='fbs_movement_quantity_released', qty=max(reserve.qty_reserved-reserve.qty_satisfied,0),
            stock_context_type='fbs_client_movement', stock_context_id=reserve.context_id,
            occurred_at=timezone.now(), performed_by=reserve.released_by,
            payload={'reason':reason,'reserve_scope':'quantity'})
    return allocations


def _select_fallback_whole_boxes(candidates, *, box_limit, qty_limit):
    """Keep the largest possible whole-box part without exceeding the request."""
    box_limit = max(int(box_limit or 0), 0)
    qty_limit = max(int(qty_limit or 0), 0)
    if box_limit == 0 or qty_limit == 0:
        return []

    states = [dict() for _ in range(box_limit + 1)]
    states[0][0] = ()
    for candidate in candidates:
        units = int(candidate.units_per_box or 0)
        if units <= 0 or units > qty_limit:
            continue
        for used_count in range(box_limit, 0, -1):
            for previous_qty, selected in list(states[used_count - 1].items()):
                total_qty = previous_qty + units
                if total_qty > qty_limit or total_qty in states[used_count]:
                    continue
                states[used_count][total_qty] = (*selected, candidate)

    choices = [
        (total_qty, used_count, selected)
        for used_count, totals in enumerate(states)
        for total_qty, selected in totals.items()
    ]
    if not choices:
        return []
    _total_qty, _used_count, selected = max(
        choices,
        key=lambda choice: (choice[0], choice[1]),
    )
    return list(selected)


@transaction.atomic
def allocate_pool(request_row, actor=None, *, allow_item_fallback=False):
    from .warehouse_write_path import WarehouseWritePathService, WarehouseTransitionError
    from fbs.services.client_movements import (
        _select_exact_box_combination,
        eligible_source_boxes,
    )
    Agency.objects.select_for_update().get(pk=request_row.agency_id)
    reserves = list(pool_reserves(request_row.agency_id, request_row.id))
    if not reserves:
        return
    validate_pool(request_row.agency_id, request_row.id)
    release_pool(reserves, actor, 'Количество передано в складской план')
    request_lines = list(request_row.lines.select_related('sku').order_by('id'))
    remaining = {line.id: int(line.requested_qty) for line in request_lines}
    allocations = []
    used = set()
    whole_container_ids = set()
    box_candidates = []
    if request_row.mode == 'box':
        box_candidates = eligible_source_boxes(
            agency_id=request_row.agency_id,
            barcodes=tuple({line.barcode for line in request_lines}),
            units_per_box=None,
            lock=True,
        )
    for line in request_lines:
        # A box-mode request may choose another physical source at acceptance,
        # but it must preserve both the requested number of whole boxes and
        # their total quantity. This also supports a saved mixed-size plan such
        # as 40 + 39 units without turning it into item repacking.
        if request_row.mode == 'box':
            required_box_count = int(line.requested_box_count or 0)
            candidates = [
                candidate
                for candidate in box_candidates
                if str(candidate.barcode or '').strip() == str(line.barcode or '').strip()
                and candidate.container.movement_stock
                and all(
                    int(snapshot.sku_ref_id or 0) == int(line.sku_id or 0)
                    for snapshot in candidate.container.movement_stock
                )
            ]
            mixed_size_plan = (
                required_box_count > 0
                and int(line.requested_qty or 0)
                != required_box_count * int(line.units_per_box or 0)
            )
            if mixed_size_plan:
                exact_candidates = _select_exact_box_combination(
                    candidates,
                    requested_box_count=required_box_count,
                    requested_qty=int(line.requested_qty or 0),
                )
                if exact_candidates:
                    candidates = exact_candidates
            else:
                candidates = [
                    candidate
                    for candidate in candidates
                    if int(candidate.units_per_box or 0)
                    == int(line.units_per_box or 0)
                ]
            selected_candidates = candidates[:required_box_count]
            needs_fallback = (
                len(selected_candidates) != required_box_count
                or sum(
                    int(candidate.units_per_box or 0)
                    for candidate in selected_candidates
                )
                != int(line.requested_qty or 0)
            )
            if needs_fallback and not allow_item_fallback:
                raise WarehouseTransitionError(
                    f'ШК {line.barcode}: не удалось подобрать '
                    f'{required_box_count} целых коробов на '
                    f'{int(line.requested_qty or 0)} шт. '
                    'FIFO и поштучный добор для FBS не используются.'
                )
            if needs_fallback:
                selected_candidates = _select_fallback_whole_boxes(
                    candidates,
                    box_limit=required_box_count,
                    qty_limit=int(line.requested_qty or 0),
                )
            for candidate in selected_candidates:
                whole_container_ids.add(candidate.container.id)
                for s in source_queryset(request_row.agency_id).filter(container=candidate.container).order_by('id'):
                    if s.id in used or s.sku_ref_id != line.sku_id:
                        continue
                    allocations.append(dict(snapshot_id=s.id,request_line_id=line.id,qty=s.qty,
                        container_id=s.container_id,container_code=s.container_code,barcode=s.barcode,sku_id=s.sku_ref_id))
                    remaining[line.id] -= s.qty
                    used.add(s.id)
            if remaining[line.id] and not allow_item_fallback:
                raise WarehouseTransitionError(
                    f'ШК {line.barcode}: выбранные целые короба не покрывают заявку; '
                    f'не распределено {remaining[line.id]} шт. '
                    'FIFO и поштучный добор для FBS не используются.'
                )
        if remaining[line.id] == 0:
            continue
        qs = source_queryset(request_row.agency_id).filter(sku_ref_id=line.sku_id,barcode=line.barcode).exclude(id__in=used).select_for_update().order_by('created_at','id')
        candidates = list(qs)
        blocked = WarehouseWritePathService.shipping_reserved_snapshot_ids(agency=request_row.agency,snapshots=candidates)
        for s in candidates:
            if s.id in blocked:
                continue
            qty = min(remaining[line.id], s.available_qty)
            if qty <= 0:
                continue
            allocations.append(dict(snapshot_id=s.id,request_line_id=line.id,qty=qty,
                container_id=s.container_id,container_code=s.container_code,barcode=s.barcode,sku_id=s.sku_ref_id))
            remaining[line.id] -= qty
            used.add(s.id)
        if remaining[line.id]:
            raise WarehouseTransitionError(f'ШК {line.barcode}: не удалось подобрать {remaining[line.id]} шт. Резерв количества сохранён.')
    WarehouseWritePathService.reserve_for_fbs_movement(
        agency=request_row.agency,
        request_id=request_row.id,
        allocations=allocations,
        whole_container_ids=whole_container_ids,
        created_by=actor,
    )


def protect_new_claims(agency, items):
    """Guard direct shipping/processing reservation calls, including non-UI callers."""
    Agency.objects.select_for_update().get(pk=agency.id)
    if not pool_reserves(agency.id).exists():
        return
    pools = demand(agency.id)
    needed = Counter()
    from .stock_availability import normalize_goods_type
    for item in items:
        for key in pools:
            if (str(item.get('sku_code') or item.get('sku') or '').strip().casefold() == key[1]
                and str(item.get('size') or '').strip().casefold() == key[2]
                and (not item.get('barcode') or str(item['barcode']).strip().casefold() == key[3])
                and (not item.get('goods_type') or normalize_goods_type(item['goods_type']) == key[4])):
                needed[key] += max(int(item.get('qty') or 0),0)
    assert_capacity(agency.id, needed)


def protect_sources(snapshots):
    """Free sources may move only if enough interchangeable stock remains for FBS."""
    rows = list(snapshots)
    for owner in {s.agency_id for s in rows}:
        Agency.objects.select_for_update().get(pk=owner)
        pools = demand(owner)
        if not pools:
            continue
        eligible = set(source_queryset(owner).values_list('id',flat=True))
        needed = Counter()
        for s in rows:
            source_key = identity(s)
            if s.id in eligible and source_key in pools:
                needed[source_key] += max(min(s.qty,s.available_qty),0)
        assert_capacity(owner, needed)


@transaction.atomic
def convert_unstarted_request(request_id):
    """Explicit migration only; never silently convert at ordinary read time."""
    from fbs.models import FbsClientMovementRequest
    from .warehouse_write_path import WarehouseWritePathService, WarehouseTransitionError
    request = FbsClientMovementRequest.objects.select_for_update().select_related('agency').get(pk=request_id)
    Agency.objects.select_for_update().get(pk=request.agency_id)
    if (request.status not in ('submitted','approved') or request.replenishment_plans.exists()
        or request.warehouse_accepted_at or request.actual_moved_qty):
        raise WarehouseTransitionError('Заявка уже передана в исполнение; перевод резерва запрещён.')
    if pool_reserves(request.agency_id, request.id).exists():
        validate_pool(request.agency_id,request.id)
        return {'request_id':request.id,'already_quantity':True}
    reserves = WarehouseWritePathService._fbs_movement_reserve_rows(agency=request.agency,request_id=request.id,lock=True)
    rows = WarehouseWritePathService._fbs_movement_allocations_from_reserves(reserves)
    expected = {line.id:line.requested_qty for line in request.lines.all()}
    actual = Counter()
    for row in rows:
        actual[row['request_line_id']] += row['qty']
    if dict(actual) != expected:
        raise WarehouseTransitionError('Состав старого резерва не совпадает с заявкой; нужен отдельный разбор.')
    snapshots = {s.id:s for s in WarehouseStockSnapshot.objects.select_for_update().filter(pk__in=[r['snapshot_id'] for r in rows])}
    by_id = {r.id:r for r in reserves}
    restored = 0
    for row in rows:
        s = snapshots.get(row['snapshot_id'])
        r = by_id[row['reserve_id']]
        if s is None or s.agency_id != request.agency_id or identity(s) != identity(r):
            raise WarehouseTransitionError('Источник старого резерва отсутствует или изменил владельца/товар.')
        if s.active_operation_id:
            raise WarehouseTransitionError('Источник старого резерва находится в действующей операции.')
        qty = row['qty']
        if s.other_reserved_qty < qty:
            raise WarehouseTransitionError('Счётчик старого резерва меньше ожидаемого; автоматический перевод запрещён.')
        before = {k:getattr(s,k) for k in ('qty','available_qty','other_reserved_qty','is_archived','is_in_vehicle')}
        s.other_reserved_qty -= qty
        # Never resurrect shipped/loaded goods, nor release the same units twice.
        restored_qty = 0
        if not s.is_archived and not s.is_in_vehicle and s.warehouse_state_code not in ('shipped','processing_consumed','canceled'):
            restored_qty = min(qty,max(s.qty-s.available_qty-s.processing_reserved_qty-s.shipping_reserved_qty-s.other_reserved_qty,0))
        s.available_qty += restored_qty
        s.snapshot_version += 1
        event = WarehouseEvent.objects.create(agency_id=request.agency_id, reserve=r,container_id=s.container_id,
            event_type='fbs_movement_exact_reserve_converted',qty=qty,
            stock_context_type='fbs_client_movement',stock_context_id=str(request.id),
            occurred_at=timezone.now(), payload={'source_snapshot_id':s.id,'before':before,
                'restored_available_qty':restored_qty,'request_line_id':row['request_line_id']})
        s.last_event=event
        s.save(update_fields=['available_qty','other_reserved_qty','snapshot_version','last_event','updated_at'])
        r.qty_allocated=0;r.status='active'
        r.save(update_fields=['qty_allocated','status','updated_at'])
        WarehouseEvent.objects.create(agency_id=request.agency_id,reserve=r,event_type=EVENT,
            qty=qty,stock_context_type='fbs_client_movement',stock_context_id=str(request.id),
            occurred_at=timezone.now(),payload={'reserve_scope':'quantity','request_line_id':row['request_line_id'],
                'converted_from_exact':True})
        restored += restored_qty
    validate_pool(request.agency_id,request.id)
    return {'request_id':request.id,'reserved_qty':sum(actual.values()),'restored_available_qty':restored}
