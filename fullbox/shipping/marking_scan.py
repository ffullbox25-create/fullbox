"""Read-only resolution of a single Data Matrix to live shipping stock."""
from marking.codes import validate_import_marking_code
from sku.models import SKUBarcode
from sklad.services.warehouse_write_path import WarehouseWritePathService


def _gtin(value):
    text = str(value or '').strip()
    return text.zfill(14) if text.isascii() and text.isdigit() and len(text) in (8, 12, 13, 14) else ''


def _size(value):
    return str(value or '').strip().casefold() or '0'


def resolve_shipping_product_barcode(*, order, barcode, candidate_keys):
    """Resolve a catalog alias without changing stock or its stored barcode."""
    from .packing import _shipping_repack_items

    token = str(barcode or '').strip()
    if not token or len(token) > 128:
        raise ValueError('Отсканируйте штрихкод товара.')
    if not isinstance(candidate_keys, list) or not candidate_keys or len(candidate_keys) > 500:
        raise ValueError('Сначала откройте нужное грузоместо для товара.')
    keys = {key for key in candidate_keys if isinstance(key, str)}
    items = [item for item in _shipping_repack_items(order)
             if item['key'] in keys and int(item.get('qty') or 0) > 0]
    rows = list(SKUBarcode.objects.filter(
        sku__agency_id=order.agency_id, sku__deleted=False, value__iexact=token,
    ).select_related('sku'))
    identities = {(row.sku_id, _size(row.size)) for row in rows}
    aliases = {}
    for row in SKUBarcode.objects.filter(sku_id__in={row.sku_id for row in rows}).select_related('sku'):
        if (row.sku_id, _size(row.size)) in identities:
            aliases.setdefault(str(row.value).strip().casefold(), set()).add(
                (str(row.sku.sku_code).strip().casefold(), _size(row.size)))
    matches = []
    for item in items:
        stored_barcode = str(item.get('barcode') or '').strip()
        identity = (str(item.get('sku_code') or '').strip().casefold(), _size(item.get('size')))
        if stored_barcode.casefold() == token.casefold() or identity in aliases.get(stored_barcode.casefold(), set()):
            matches.append(item)
    if not matches:
        raise ValueError(f'ШК {token} не соответствует товару в выбранном грузоместе. Проверьте артикул, размер и откройте нужный короб.')
    if len(matches) != 1:
        raise ValueError(f'ШК {token} соответствует нескольким позициям. Уточните грузоместо; товар не добавлен.')
    source = matches[0]
    return {'source_key': source['key'], 'barcode': source['barcode'],
            'sku_code': source['sku_code'], 'size': source['size']}


def resolve_shipping_data_matrix(*, order, marking_code, candidate_keys):
    from .packing import _shipping_repack_items, _shipping_repack_snapshots

    code = validate_import_marking_code(marking_code)
    if not (code.startswith('01') and code[2:16].isdigit() and code[16:18] == '21'):
        raise ValueError('Для одного сканирования нужен Data Matrix с GTIN. Отсканируйте квадратный код целиком.')
    if not isinstance(candidate_keys, list) or not candidate_keys or len(candidate_keys) > 500:
        raise ValueError('В открытом коробе нет товара, ожидающего Data Matrix. Выберите нужное грузоместо.')
    keys = {str(key) for key in candidate_keys if isinstance(key, str)}
    # Client keys can only narrow current warehouse stock, never introduce rows.
    items = [item for item in _shipping_repack_items(order)
             if item['key'] in keys and item.get('requires_marking_scan') and int(item.get('qty') or 0) > 0]
    gtin = code[2:16]
    variants = {gtin[-length:] for length in (8, 12, 13, 14)
                if gtin[-length:].zfill(14) == gtin}
    gtin_rows = list(SKUBarcode.objects.filter(
        sku__agency_id=order.agency_id, sku__deleted=False, value__in=variants,
    ).select_related('sku'))
    identities = {(row.sku_id, _size(row.size)) for row in gtin_rows}
    aliases = {}
    for row in SKUBarcode.objects.filter(sku_id__in={row.sku_id for row in gtin_rows}).select_related('sku'):
        if (row.sku_id, _size(row.size)) in identities:
            aliases.setdefault(str(row.value).strip().casefold(), set()).add(
                (str(row.sku.sku_code).strip().casefold(), _size(row.size)))
    matches = []
    for item in items:
        barcode = str(item.get('barcode') or '').strip()
        identity = (str(item.get('sku_code') or '').strip().casefold(), _size(item.get('size')))
        if _gtin(barcode) == gtin or identity in aliases.get(barcode.casefold(), set()):
            matches.append(item)
    if not matches:
        raise ValueError(f'GTIN {gtin} не соответствует товару, ожидающему ЧЗ в открытом коробе этой заявки. Проверьте грузоместо и связь GTIN с ШК товара в номенклатуре.')
    if len(matches) != 1:
        raise ValueError(f'GTIN {gtin} соответствует нескольким позициям. Уточните товар в открытом коробе; скан не засчитан.')
    source = matches[0]
    # Preserve existing stock, marking-format and order-membership validation.
    verified = WarehouseWritePathService.validate_loose_shipping_marking_scan(
        agency=order.agency, order_id=order.number,
        barcode=source['barcode'], marking_code=code,
        source_bindings=[(row.id, row.container_id, row.container_code)
                         for row in _shipping_repack_snapshots(order)
                         if row.id in source['snapshot_ids']],
    )
    return {**verified, 'source_key': source['key'], 'gtin': gtin}
