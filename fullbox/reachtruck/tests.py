import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.test import RequestFactory, SimpleTestCase, TestCase

from audit.models import OrderAuditEntry
from employees.models import Employee
from orders.views import _create_receiving_warehouse_moves
from processing_app.views import _replace_processing_reserves
from sku.models import Agency, SKU, SKUBarcode
from sklad.models import (
    WarehouseContainer,
    WarehouseEvent,
    WarehouseLocation,
    WarehouseOperation,
    WarehouseOperationTask,
    WarehouseReserve,
    WarehouseStockSnapshot,
)
from sklad.services.warehouse_transitions import WarehouseStateCode
from sklad.services.warehouse_write_path import WarehouseWritePathService
from sklad.services.stock_operations import OperationalStockService
from .models import BoxClaim, MoveRequest, MoveRequestItem, MoveTask, PalletLock
from .services import (
    build_mobile_execution_snapshot,
    build_mobile_request_execution_snapshot,
    complete_move_task,
    create_stock_move_task,
    putaway_location_scan_code,
    scan_move_request_step,
    scan_move_task_step,
    take_move_request,
    take_move_task,
)
from .services.task_commands import (
    confirm_move_task_unit_quantity,
    display_scan_text,
    _location_scan_code,
    _same_box_code_scan,
    _same_location_scan,
    _same_pallet_code_scan,
    _same_scan_value,
    _schedule_shipping_supplement_packing_stage,
    _should_use_container_scoped_otg_move,
    _validate_resolved_otg_full_box_count,
)
from .services.claims import claim_boxes_for_task
from .services.pallet_ops import (
    _all_stock_boxes_for_pallet,
    _item_matches,
    _matching_stock_boxes_for_pallet,
)

from .views import (
    MOVE_MODE_BOX_FULL,
    MOVE_MODE_BOX_PARTIAL,
    MOVE_MODE_PALLET_FULL,
    _barcode_qty_preview,
    _collect_moves,
    _latest_closed_placement_entries,
    _move_instruction,
    _move_boxes_to_otg,
    _normalize_move_mode,
    _box_execution_plan,
    _partial_request_covers_full_pallet,
    _pallet_box_plan,
    _parse_box_codes,
    _payload_box_codes,
    _resolve_box_partial_codes,
    _resolve_otg_box_codes,
)


def _warehouse_zone_kind(zone: str) -> str:
    return {
        "PR": WarehouseLocation.ZONE_KIND_RECEIVING,
        "OS": WarehouseLocation.ZONE_KIND_STORAGE,
        "OBR": WarehouseLocation.ZONE_KIND_PROCESSING,
        "OTG": WarehouseLocation.ZONE_KIND_SHIPPING,
    }.get(str(zone or "").strip().upper(), WarehouseLocation.ZONE_KIND_STORAGE)


def _warehouse_state_for_zone(zone: str) -> str:
    return {
        "PR": WarehouseStateCode.PLACED_IN_RECEIVING.value,
        "OS": WarehouseStateCode.STORED.value,
        "OBR": WarehouseStateCode.IN_PROCESSING_ZONE.value,
        "OTG": WarehouseStateCode.IN_OTG.value,
    }.get(str(zone or "").strip().upper(), WarehouseStateCode.STORED.value)


def _warehouse_location_label(zone: str, row: int = 0, section: int = 0, tier: int = 0, cell: int = 0, location: str = "") -> str:
    if location:
        return location
    zone = str(zone or "").strip().upper()
    if zone == "PR":
        return "PR · Зона приемки"
    if zone == "OTG":
        return "OTG · Зона отгрузки"
    if zone == "OBR":
        return "OBR · Зона обработки"
    if zone == "OS" and row and section and tier and cell:
        return f"OS · Ряд {row} · Секция {section} · Ярус {tier} · Ячейка {cell}"
    return zone or "OS"


def create_warehouse_snapshot_row(
    *,
    agency: Agency,
    order_type: str = "receiving",
    order_id: str = "1",
    sku: str = "SKU-1",
    name: str = "",
    size: str = "",
    barcode: str = "",
    goods_type: str = "",
    qty: int = 1,
    available_qty: int | None = None,
    processing_reserved_qty: int = 0,
    shipping_reserved_qty: int = 0,
    box_code: str = "",
    pallet_code: str,
    zone: str = "OS",
    row: int = 0,
    section: int = 0,
    tier: int = 0,
    cell: int = 0,
    location: str = "",
) -> WarehouseStockSnapshot:
    zone_code = str(zone or "").strip().upper()
    location_obj, _ = WarehouseLocation.objects.get_or_create(
        warehouse_code="MSK",
        zone_code=zone_code,
        row_no=int(row or 0),
        section_no=int(section or 0),
        tier_no=int(tier or 0),
        cell_no=int(cell or 0),
        defaults={
            "zone_kind": _warehouse_zone_kind(zone_code),
            "display_name": _warehouse_location_label(zone_code, row, section, tier, cell, location),
        },
    )
    pallet, _ = WarehouseContainer.objects.get_or_create(
        agency=agency,
        container_code=pallet_code,
        defaults={
            "container_type": WarehouseContainer.TYPE_PALLET,
            "current_location": location_obj,
            "source_context_type": order_type,
            "source_context_id": str(order_id),
        },
    )
    if pallet.current_location_id != location_obj.id:
        pallet.current_location = location_obj
        pallet.save(update_fields=["current_location", "updated_at"])
    container = pallet
    parent_container = None
    container_code = pallet_code
    if box_code:
        container, _ = WarehouseContainer.objects.get_or_create(
            agency=agency,
            container_code=box_code,
            defaults={
                "container_type": WarehouseContainer.TYPE_BOX,
                "parent_container": pallet,
                "current_location": location_obj,
                "source_context_type": order_type,
                "source_context_id": str(order_id),
            },
        )
        changed_fields = []
        if container.parent_container_id != pallet.id:
            container.parent_container = pallet
            changed_fields.append("parent_container")
        if container.current_location_id != location_obj.id:
            container.current_location = location_obj
            changed_fields.append("current_location")
        if changed_fields:
            container.save(update_fields=[*changed_fields, "updated_at"])
        parent_container = pallet
        container_code = box_code
    return WarehouseStockSnapshot.objects.create(
        agency=agency,
        source_context_type=order_type,
        source_context_id=str(order_id),
        sku_code=sku,
        name=name,
        size=size,
        barcode=barcode,
        goods_type=goods_type,
        qty=int(qty or 0),
        available_qty=int(available_qty if available_qty is not None else qty or 0),
        processing_reserved_qty=int(processing_reserved_qty or 0),
        shipping_reserved_qty=int(shipping_reserved_qty or 0),
        container=container,
        container_code=container_code,
        parent_container=parent_container,
        location=location_obj,
        zone_code=zone_code,
        zone_kind=location_obj.zone_kind,
        warehouse_state_code=_warehouse_state_for_zone(zone_code),
    )


def move_warehouse_pallet(
    *,
    agency: Agency,
    pallet_code: str,
    zone: str,
    row: int = 0,
    section: int = 0,
    tier: int = 0,
    cell: int = 0,
    location: str = "",
) -> int:
    zone_code = str(zone or "").strip().upper()
    location_obj, _ = WarehouseLocation.objects.get_or_create(
        warehouse_code="MSK",
        zone_code=zone_code,
        row_no=int(row or 0),
        section_no=int(section or 0),
        tier_no=int(tier or 0),
        cell_no=int(cell or 0),
        defaults={
            "zone_kind": _warehouse_zone_kind(zone_code),
            "display_name": _warehouse_location_label(zone_code, row, section, tier, cell, location),
        },
    )
    snapshots = list(
        WarehouseStockSnapshot.objects.filter(agency=agency, is_archived=False)
        .filter(
            Q(container__container_code=pallet_code)
            | Q(parent_container__container_code=pallet_code)
            | Q(container_code=pallet_code)
        )
        .select_related("container", "parent_container")
    )
    for snapshot in snapshots:
        snapshot.location = location_obj
        snapshot.zone_code = zone_code
        snapshot.zone_kind = location_obj.zone_kind
        snapshot.warehouse_state_code = _warehouse_state_for_zone(zone_code)
        snapshot.save(update_fields=["location", "zone_code", "zone_kind", "warehouse_state_code", "updated_at"])
        for container in (snapshot.container, snapshot.parent_container):
            if container and container.current_location_id != location_obj.id:
                container.current_location = location_obj
                container.save(update_fields=["current_location", "updated_at"])
    return len(snapshots)


class ReachtruckHelpersTests(SimpleTestCase):
    databases = {"default"}

    def test_item_match_uses_only_explicit_barcode_when_barcode_is_requested(self):
        item = {
            "sku": "SKU-SAME",
            "barcode": "BAR-WRONG",
            "goods_type": "gv",
        }

        self.assertFalse(
            _item_matches(
                item,
                {"BAR-RIGHT"},
                {"SKU-SAME"},
                {"gv"},
            )
        )

    def test_item_match_keeps_legacy_sku_fallback_without_requested_barcode(self):
        item = {
            "sku": "SKU-LEGACY",
            "goods_type": "gv",
        }

        self.assertTrue(
            _item_matches(
                item,
                set(),
                {"SKU-LEGACY"},
                set(),
            )
        )

    def test_normalize_move_mode_aliases(self):
        self.assertEqual(_normalize_move_mode("full"), MOVE_MODE_PALLET_FULL)
        self.assertEqual(_normalize_move_mode("boxes"), MOVE_MODE_BOX_FULL)
        self.assertEqual(_normalize_move_mode("partial"), MOVE_MODE_BOX_PARTIAL)
        self.assertEqual(_normalize_move_mode("", raw_pick_mode="partial"), MOVE_MODE_BOX_PARTIAL)
        self.assertEqual(_normalize_move_mode(None, raw_pick_mode=None), MOVE_MODE_PALLET_FULL)

    def test_scan_fact_full_pallet_uses_only_scanned_containers(self):
        self.assertTrue(
            _should_use_container_scoped_otg_move(
                scan_fact_mode=True,
                move_mode=MOVE_MODE_PALLET_FULL,
            )
        )
        self.assertFalse(
            _should_use_container_scoped_otg_move(
                scan_fact_mode=False,
                move_mode=MOVE_MODE_PALLET_FULL,
            )
        )

    def test_parse_box_codes_deduplicates_and_trims(self):
        parsed = _parse_box_codes(" box-1, BOX-1; box-2 \n box-3 ")
        self.assertEqual(parsed, ["box-1", "box-2", "box-3"])

        parsed_json = _parse_box_codes('["A-1", "a-1", "B-2"]')
        self.assertEqual(parsed_json, ["A-1", "B-2"])

    def test_payload_box_codes_from_list(self):
        payload = {"requested_boxes": ["x-1", "X-1", "x-2", "", "  "]}
        self.assertEqual(_payload_box_codes(payload), ["x-1", "x-2"])

    def test_strict_otg_full_box_guard_rejects_incomplete_resolved_list(self):
        error = _validate_resolved_otg_full_box_count(
            {
                "otg_scan_fact_mode": "scan_facts_v1",
                "move_mode": MOVE_MODE_BOX_FULL,
                "requested_box_selection": "fixed",
                "requested_box_count": 11,
                "requested_boxes": [f"BOX-{index:02d}" for index in range(1, 12)],
            },
            ["BOX-01"],
        )

        self.assertIn("по плану нужно переместить 11 коробов", error)
        self.assertIn("фактически подтверждено 1", error)

    def test_strict_otg_full_box_guard_accepts_complete_exact_list(self):
        boxes = ["BOX-01", "BOX-02", "BOX-03"]

        error = _validate_resolved_otg_full_box_count(
            {
                "otg_scan_fact_mode": "scan_facts_v1",
                "move_mode": MOVE_MODE_BOX_FULL,
                "requested_box_selection": "fixed",
                "requested_box_count": 3,
                "requested_boxes": boxes,
            },
            list(reversed(boxes)),
        )

        self.assertEqual(error, "")

    def test_barcode_qty_preview_with_ellipsis(self):
        source = {
            "200000000001": 10,
            "200000000002": 20,
            "200000000003": 30,
            "200000000004": 40,
            "200000000005": 50,
        }
        preview = _barcode_qty_preview(source, limit=3)
        self.assertIn("200000000001 - 10 шт.", preview)
        self.assertIn("200000000003 - 30 шт.", preview)
        self.assertTrue(preview.endswith("…"))

    def test_move_boxes_to_otg_returns_pallet_with_remainder(self):
        placement_payload = {
            "act_pallets": [
                {
                    "code": "PAL-1",
                    "boxes": ["BOX-1", "BOX-2"],
                    "items": [],
                    "location": {"zone": "OS", "row": 3, "section": 1, "tier": 1, "cell": 2},
                }
            ],
            "act_boxes": [
                {"code": "BOX-1", "items": [{"sku": "SKU-1", "qty": 10}]},
                {"code": "BOX-2", "items": [{"sku": "SKU-2", "qty": 5}]},
            ],
        }
        ok, err, meta = _move_boxes_to_otg(
            placement_payload,
            "PAL-1",
            ["BOX-1"],
            otg_location={"zone": "OTG", "row": "", "section": "", "tier": "", "cell": ""},
            return_location={"zone": "OS", "row": 3, "section": 1, "tier": 1, "cell": 2},
        )
        self.assertTrue(ok, err)
        self.assertEqual(meta.get("moved_boxes"), ["BOX-1"])
        self.assertFalse(meta.get("pallet_deleted"))
        self.assertTrue(meta.get("pallet_returned"))
        pallets = placement_payload["act_pallets"]
        self.assertEqual(len(pallets), 1)
        self.assertEqual(pallets[0].get("boxes"), ["BOX-2"])
        self.assertEqual((pallets[0].get("location") or {}).get("zone"), "OS")
        box1 = next(box for box in placement_payload["act_boxes"] if box.get("code") == "BOX-1")
        self.assertEqual((box1.get("location") or {}).get("zone"), "OTG")

    def test_same_scan_value_accepts_common_mojibake_for_cyrillic_pallet_codes(self):
        expected = "ТДТ-3004-247635-gv"
        mojibake_cp1251 = expected.encode("utf-8").decode("cp1251")
        mojibake_cp1252 = expected.encode("utf-8").decode("cp1252")
        mojibake_latin1 = expected.encode("cp1251").decode("latin1")

        self.assertTrue(_same_scan_value(mojibake_cp1251, expected))
        self.assertTrue(_same_scan_value(mojibake_cp1252, expected))
        self.assertTrue(_same_scan_value(mojibake_latin1, expected))

    def test_same_pallet_code_scan_accepts_matching_numeric_tail(self):
        expected = "ТДТ-3004-247635-gv"
        self.assertTrue(_same_pallet_code_scan("???-3004-247635-xx", expected))

    def test_display_scan_text_repairs_gbk_mojibake_for_cyrillic_pallet_codes(self):
        expected = "ТДТ-3004-247635-gv"
        mojibake_gbk = expected.encode("utf-8").decode("gb18030")
        self.assertEqual(display_scan_text(mojibake_gbk), expected)

    def test_same_box_code_scan_accepts_matching_tail_when_prefix_is_garbled(self):
        expected = "ТДТ-0405-020070-op"
        garbled_scan = "BAD�-0405-020070-op"

        self.assertTrue(_same_box_code_scan(garbled_scan, expected))
        self.assertFalse(_same_box_code_scan("BAD-0405-020070-op", expected))

    def test_location_scan_code_uses_stockmap_os_format(self):
        self.assertEqual(
            _location_scan_code({"zone": "OS", "row": 3, "section": 6, "tier": 3, "cell": 2}),
            "E-3/3-2",
        )

    def test_public_putaway_location_scan_code_matches_mobile_format(self):
        self.assertEqual(
            putaway_location_scan_code({"zone": "OS", "row": 1, "section": 1, "tier": 3, "cell": 2}),
            "0-1/3-2",
        )
        self.assertEqual(
            putaway_location_scan_code({"zone": "OBR"}),
            "OBR",
        )

    def test_same_location_scan_accepts_stockmap_and_legacy_os_codes(self):
        location = {"zone": "OS", "row": 3, "section": 6, "tier": 3, "cell": 2}
        self.assertTrue(_same_location_scan("E-3/3-2", location))
        self.assertTrue(_same_location_scan("OS-3-6-3-2", location))

    def test_move_boxes_to_otg_deletes_empty_pallet(self):
        placement_payload = {
            "act_pallets": [
                {
                    "code": "PAL-2",
                    "boxes": ["BOX-9"],
                    "items": [],
                    "location": {"zone": "OS", "row": 4, "section": 2, "tier": 1, "cell": 1},
                }
            ],
            "act_boxes": [
                {"code": "BOX-9", "items": [{"sku": "SKU-9", "qty": 8}]},
            ],
        }
        ok, err, meta = _move_boxes_to_otg(
            placement_payload,
            "PAL-2",
            ["BOX-9"],
            otg_location={"zone": "OTG", "row": "", "section": "", "tier": "", "cell": ""},
            return_location={"zone": "OS", "row": 4, "section": 2, "tier": 1, "cell": 1},
        )
        self.assertTrue(ok, err)
        self.assertTrue(meta.get("pallet_deleted"))
        self.assertFalse(meta.get("pallet_returned"))
        self.assertEqual(placement_payload["act_pallets"], [])

    def test_resolve_otg_box_codes_by_sku_and_qty(self):
        placement_payload = {
            "act_pallets": [
                {"code": "PAL-3", "boxes": ["BOX-A", "BOX-B"], "items": []},
            ],
            "act_boxes": [
                {"code": "BOX-A", "items": [{"sku": "SKU-1", "barcode": "200000000001", "qty": 10}]},
                {"code": "BOX-B", "items": [{"sku": "SKU-1", "barcode": "200000000002", "qty": 10}]},
            ],
        }
        payload = {
            "requested_sku": "SKU-1",
            "requested_qty": 10,
        }
        codes, err = _resolve_otg_box_codes(placement_payload, "PAL-3", payload)
        self.assertEqual(err, "")
        self.assertEqual(codes, ["BOX-A"])

    def test_resolve_otg_box_codes_by_barcode_qty_avoids_duplicate_box(self):
        placement_payload = {
            "act_pallets": [
                {"code": "PAL-4", "boxes": ["BOX-A", "BOX-B"], "items": []},
            ],
            "act_boxes": [
                {"code": "BOX-A", "items": [{"sku": "SKU-1", "barcode": "200000000111", "qty": 50}]},
                {"code": "BOX-B", "items": [{"sku": "SKU-1", "barcode": "200000000111", "qty": 50}]},
            ],
        }
        payload = {
            "requested_qty": 50,
            "requested_barcode_qty": {"200000000111": 50},
        }

        codes, err = _resolve_otg_box_codes(placement_payload, "PAL-4", payload)

        self.assertEqual(err, "")
        self.assertEqual(codes, ["BOX-A"])

    def test_resolve_box_partial_codes_for_obr_without_explicit_box(self):
        placement_payload = {
            "act_pallets": [
                {"code": "PAL-5", "boxes": ["BOX-A"], "items": []},
            ],
            "act_boxes": [
                {"code": "BOX-A", "items": [{"sku": "SKU-1", "barcode": "200000000555", "qty": 50}]},
            ],
        }
        payload = {
            "move_mode": MOVE_MODE_BOX_PARTIAL,
            "to_location": {"zone": "OBR"},
            "requested_sku": "SKU-1",
            "requested_qty": 50,
            "requested_barcodes": ["200000000555"],
        }

        codes, err = _resolve_box_partial_codes(placement_payload, "PAL-5", payload)

        self.assertEqual(err, "")
        self.assertEqual(codes, ["BOX-A"])

    def test_partial_request_covers_full_pallet_when_single_box_consumed_fully(self):
        placement_payload = {
            "act_pallets": [
                {"code": "PAL-6", "boxes": ["BOX-A"], "items": []},
            ],
            "act_boxes": [
                {"code": "BOX-A", "items": [{"sku": "SKU-1", "barcode": "200000000666", "qty": 50}]},
            ],
        }

        result = _partial_request_covers_full_pallet(
            placement_payload,
            "PAL-6",
            pick_qty=50,
            barcode_values={"200000000666"},
            sku_values={"SKU-1"},
            requested_box="BOX-A",
        )

        self.assertTrue(result)

    def test_partial_request_does_not_cover_full_pallet_when_other_box_exists(self):
        placement_payload = {
            "act_pallets": [
                {"code": "PAL-7", "boxes": ["BOX-A", "BOX-B"], "items": []},
            ],
            "act_boxes": [
                {"code": "BOX-A", "items": [{"sku": "SKU-1", "barcode": "200000000777", "qty": 50}]},
                {"code": "BOX-B", "items": [{"sku": "SKU-2", "barcode": "200000000778", "qty": 50}]},
            ],
        }

        result = _partial_request_covers_full_pallet(
            placement_payload,
            "PAL-7",
            pick_qty=50,
            barcode_values={"200000000777"},
            sku_values={"SKU-1"},
            requested_box="BOX-A",
        )

        self.assertFalse(result)

    def test_pallet_box_plan_marks_single_matching_obr_box_as_deliver(self):
        agency = Agency.objects.create(agn_name="Клиент OBR одного короба")
        create_warehouse_snapshot_row(
            agency=agency,
            order_type="receiving",
            order_id="3",
            sku="SKU-1",
            barcode="200000000555",
            goods_type="gv",
            qty=50,
            box_code="BOX-A",
            pallet_code="PAL-3",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=3,
            location="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 3",
        )
        payload = {
            "move_mode": MOVE_MODE_BOX_PARTIAL,
            "to_location": {"zone": "OBR"},
            "requested_sku": "SKU-1",
            "requested_qty": 50,
            "requested_barcodes": ["200000000555"],
        }

        rows = _pallet_box_plan(payload, "PAL-3", agency_id=agency.id)

        self.assertEqual([row["box_code"] for row in rows], ["BOX-A"])
        self.assertEqual([row["action"] for row in rows], ["Доставить"])

    def test_box_execution_plan_for_partial_mix_ships_only_picked_units(self):
        agency = Agency.objects.create(agn_name="Клиент partial mix")
        for box_code in ("BOX-MIX-1", "BOX-MIX-2"):
            create_warehouse_snapshot_row(
                agency=agency,
                order_type="receiving",
                order_id="MIX",
                sku="1108_Beige",
                name="Пальто осеннее",
                size="M",
                barcode="BC-M",
                goods_type="gv",
                qty=25,
                box_code=box_code,
                pallet_code="PAL-MIX",
                zone="OS",
                row=1,
                section=1,
                tier=1,
                cell=1,
            )
            create_warehouse_snapshot_row(
                agency=agency,
                order_type="receiving",
                order_id="MIX",
                sku="1108_Beige",
                name="Пальто осеннее",
                size="XL",
                barcode="BC-XL",
                goods_type="gv",
                qty=25,
                box_code=box_code,
                pallet_code="PAL-MIX",
                zone="OS",
                row=1,
                section=1,
                tier=1,
                cell=1,
            )
        payload = {
            "move_mode": MOVE_MODE_BOX_PARTIAL,
            "pick_mode": "partial",
            "to_location": {"zone": "OTG"},
            "requested_box_selection": "pattern_matching",
            "requested_box_count": 1,
            "requested_qty": 10,
            "requested_barcode_qty": {"BC-M": 10},
            "requested_box_patterns": [
                {
                    "box_qty": 50,
                    "requested_box_count": 1,
                    "barcode_qty": {"BC-M": 25, "BC-XL": 25},
                    "requested_article": "1108_Beige",
                    "requested_goods_type": "gv",
                    "requested_barcodes": ["BC-M", "BC-XL"],
                }
            ],
            "partial_pick_patterns": [
                {
                    "source_box_qty": 50,
                    "requested_box_count": 1,
                    "source_barcode_qty": {"BC-M": 25, "BC-XL": 25},
                    "pick_qty": 10,
                    "barcode_qty": {"BC-M": 10},
                    "requested_article": "1108_Beige",
                    "requested_goods_type": "gv",
                    "requested_barcodes": ["BC-M", "BC-XL"],
                }
            ],
            "ship_as_loose_units": True,
        }

        pallet_rows = _pallet_box_plan(payload, "PAL-MIX", agency_id=agency.id)
        execution_rows = _box_execution_plan(payload, "PAL-MIX", agency_id=agency.id, pallet_plan=pallet_rows)

        delivered = [row for row in execution_rows if row["action"] == "Доставить"]
        returned = [row for row in execution_rows if row["action"] == "Вернуть"]
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]["box_qty"], 50)
        self.assertEqual(delivered[0]["to_obr_qty"], 10)
        self.assertEqual(delivered[0]["remain_in_box_qty"], 40)
        self.assertEqual(delivered[0]["return_back_qty"], 40)
        self.assertEqual(len(returned), 1)
        self.assertEqual(returned[0]["to_obr_qty"], 0)
        self.assertEqual(returned[0]["return_back_qty"], 50)

    def test_move_instruction_prefers_explicit_service_instruction(self):
        payload = {
            "instruction": "Частичный отбор: возьми палету PAL-1 и доставь 15 шт. в OTG.",
            "move_mode": MOVE_MODE_BOX_PARTIAL,
            "requested_qty": 15,
        }

        self.assertEqual(
            _move_instruction(payload),
            "Частичный отбор: возьми палету PAL-1 и доставь 15 шт. в OTG.",
        )

class ReachtruckMoveRequestTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="processing_head_test", password="pwd")
        Employee.objects.create(
            full_name="Руководитель Обработки",
            role="processing_head",
            user=self.user,
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="ООО Тест Клиент")
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="R-100",
            sku="SKU-100",
            barcode="200000000100",
            goods_type="gv",
            qty=12,
            pallet_code="PAL-100",
            zone="PR",
            location="PR",
        )
        self.client.login(username="processing_head_test", password="pwd")

    def _create_snapshot_pallet(
        self,
        *,
        pallet_code: str = "PAL-100",
        qty: int = 12,
        zone_code: str = "PR",
        zone_kind: str = WarehouseLocation.ZONE_KIND_RECEIVING,
        display_name: str = "PR · Зона приемки",
    ) -> WarehouseStockSnapshot:
        del zone_kind
        return create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="R-100",
            sku="SKU-100",
            barcode="200000000100",
            goods_type="gv",
            qty=qty,
            pallet_code=pallet_code,
            zone=zone_code,
            location=display_name,
        )

    def test_create_move_request_plans_tasks_from_items(self):
        response = self.client.post(
            "/reachtruck/requests/create/",
            data={
                "to_zone": "OBR",
                "agency_id": str(self.agency.id),
                "request_items_json": json.dumps(
                    [
                        {
                            "requested_article": "SKU-100",
                            "requested_goods_type": "gv",
                            "requested_qty": 5,
                            "requested_barcodes": ["200000000100"],
                        }
                    ]
                ),
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("ok"))
        self.assertGreaterEqual(int(payload.get("tasks_created") or 0), 1)
        self.assertEqual(MoveRequest.objects.count(), 1)
        self.assertEqual(MoveTask.objects.count(), 1)
        task = MoveTask.objects.first()
        self.assertEqual(task.move_mode, "box_partial")
        self.assertEqual(task.to_zone, "OBR")

    def test_box_claim_prevents_second_task_claiming_same_box(self):
        request_1 = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            agency=self.agency,
        )
        request_2 = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            agency=self.agency,
        )
        task_1 = MoveTask.objects.create(
            request=request_1,
            pallet_code="PAL-CLAIM-1",
            legacy_order_id="CLAIM-1",
        )
        task_2 = MoveTask.objects.create(
            request=request_2,
            pallet_code="PAL-CLAIM-2",
            legacy_order_id="CLAIM-2",
        )

        claim_boxes_for_task(task_1, ["BOX-CLAIM-1"], claimed_by=self.user)

        with self.assertRaisesMessage(ValueError, "BOX-CLAIM-1"):
            claim_boxes_for_task(task_2, ["BOX-CLAIM-1"], claimed_by=self.user)
        self.assertEqual(BoxClaim.objects.filter(status=BoxClaim.STATUS_CLAIMED).count(), 1)

    def test_box_claim_rejects_unreconciled_delivered_full_box(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="R-DELIVERED-CLAIM",
            sku="SKU-DELIVERED-CLAIM",
            barcode="BAR-DELIVERED-CLAIM",
            goods_type="gv",
            qty=8,
            available_qty=8,
            box_code="BOX-DELIVERED-CLAIM",
            pallet_code="PAL-DELIVERED-CLAIM",
            zone="OS",
        )
        request_1 = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            agency=self.agency,
        )
        request_2 = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            agency=self.agency,
        )
        task_1 = MoveTask.objects.create(
            request=request_1,
            pallet_code="PAL-DELIVERED-CLAIM",
            legacy_order_id="DELIVERED-CLAIM-1",
        )
        task_2 = MoveTask.objects.create(
            request=request_2,
            pallet_code="PAL-DELIVERED-CLAIM",
            legacy_order_id="DELIVERED-CLAIM-2",
        )
        claim = claim_boxes_for_task(
            task_1,
            ["BOX-DELIVERED-CLAIM"],
            claimed_by=self.user,
            lock_pallet=False,
        )[0]
        task_1.status = MoveTask.STATUS_DONE
        task_1.save(update_fields=["status", "updated_at"])
        claim.status = BoxClaim.STATUS_DELIVERED
        claim.save(update_fields=["status", "updated_at"])

        with self.assertRaisesMessage(ValueError, "BOX-DELIVERED-CLAIM"):
            claim_boxes_for_task(
                task_2,
                ["BOX-DELIVERED-CLAIM"],
                claimed_by=self.user,
                lock_pallet=False,
            )

    def test_box_claim_locks_source_pallet_for_other_tasks(self):
        request_1 = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            agency=self.agency,
        )
        request_2 = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            agency=self.agency,
        )
        task_1 = MoveTask.objects.create(
            request=request_1,
            pallet_code="PAL-LOCK-1",
            legacy_order_id="LOCK-1",
        )
        task_2 = MoveTask.objects.create(
            request=request_2,
            pallet_code="PAL-LOCK-1",
            legacy_order_id="LOCK-2",
        )

        claim_boxes_for_task(task_1, ["BOX-LOCK-1"], claimed_by=self.user)

        with self.assertRaisesMessage(ValueError, "PAL-LOCK-1"):
            claim_boxes_for_task(task_2, ["BOX-LOCK-2"], claimed_by=self.user)
        self.assertEqual(PalletLock.objects.filter(status=PalletLock.STATUS_ACTIVE).count(), 1)

    def test_create_move_request_uses_warehouse_snapshot_when_legacy_rows_missing(self):
        WarehouseStockSnapshot.objects.filter(agency=self.agency).delete()
        WarehouseContainer.objects.filter(agency=self.agency).delete()
        self._create_snapshot_pallet()

        response = self.client.post(
            "/reachtruck/requests/create/",
            data={
                "to_zone": "OBR",
                "agency_id": str(self.agency.id),
                "request_items_json": json.dumps(
                    [
                        {
                            "requested_article": "SKU-100",
                            "requested_goods_type": "gv",
                            "requested_qty": 5,
                            "requested_barcodes": ["200000000100"],
                        }
                    ]
                ),
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("ok"))
        task = MoveTask.objects.get()
        self.assertEqual(task.pallet_code, "PAL-100")
        self.assertEqual(task.to_zone, "OBR")

    def test_create_move_request_with_processing_order_links_warehouse_operation(self):
        move_warehouse_pallet(
            agency=self.agency,
            pallet_code="PAL-100",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=2,
            location="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 2",
        )
        _replace_processing_reserves(
            "PROC-100",
            self.agency,
            [
                {
                    "sku": "SKU-100",
                    "barcode": "200000000100",
                    "goods_type": "gv",
                    "qty": 5,
                }
            ],
        )

        response = self.client.post(
            "/reachtruck/requests/create/",
            data={
                "processing_order_id": "PROC-100",
                "to_zone": "OBR",
                "agency_id": str(self.agency.id),
                "request_items_json": json.dumps(
                    [
                        {
                            "requested_article": "SKU-100",
                            "requested_goods_type": "gv",
                            "requested_qty": 5,
                            "requested_barcodes": ["200000000100"],
                        }
                    ]
                ),
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("ok"))
        task = MoveTask.objects.get()
        operation = WarehouseOperation.objects.get(id=(task.payload or {}).get("warehouse_operation_id"))
        warehouse_task = operation.tasks.get()
        self.assertEqual(operation.operation_type, WarehouseOperation.TYPE_MOVE_TO_PROCESSING)
        self.assertEqual(operation.context_type, "processing")
        self.assertEqual(operation.context_id, "PROC-100")
        self.assertEqual(warehouse_task.payload.get("legacy_move_id"), task.legacy_order_id)

    def test_complete_processing_move_updates_warehouse_snapshot_to_processing_zone(self):
        driver_user = get_user_model().objects.create_user(username="reachtruck_driver_processing", password="pwd")
        driver_employee = Employee.objects.create(
            full_name="Водитель Обработки",
            role="reachtruck_driver",
            user=driver_user,
            is_active=True,
        )
        move_warehouse_pallet(
            agency=self.agency,
            pallet_code="PAL-100",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=2,
            location="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 2",
        )
        _replace_processing_reserves(
            "PROC-101",
            self.agency,
            [
                {
                    "sku": "SKU-100",
                    "barcode": "200000000100",
                    "goods_type": "gv",
                    "qty": 5,
                }
            ],
        )
        response = self.client.post(
            "/reachtruck/requests/create/",
            data={
                "processing_order_id": "PROC-101",
                "to_zone": "OBR",
                "agency_id": str(self.agency.id),
                "request_items_json": json.dumps(
                    [
                        {
                            "requested_article": "SKU-100",
                            "requested_goods_type": "gv",
                            "requested_qty": 5,
                            "requested_barcodes": ["200000000100"],
                        }
                    ]
                ),
            },
        )
        self.assertEqual(response.status_code, 200)
        task = MoveTask.objects.get()
        task.status = MoveTask.STATUS_IN_PROGRESS
        task.assigned_to = driver_user
        task.assigned_to_name = driver_employee.full_name
        payload = dict(task.payload or {})
        payload["status"] = MoveTask.STATUS_IN_PROGRESS
        payload["assigned_to_id"] = driver_employee.id
        payload["assigned_to_name"] = driver_employee.full_name
        task.payload = payload
        task.save(update_fields=["status", "assigned_to", "assigned_to_name", "payload", "updated_at"])

        complete_result = complete_move_task(
            legacy_order_id=task.legacy_order_id,
            user=driver_user,
            employee_id=driver_employee.id,
            employee_name=driver_employee.full_name,
        )

        self.assertTrue(complete_result.ok, complete_result.error)
        operation = WarehouseOperation.objects.get(id=payload["warehouse_operation_id"])
        snapshot = WarehouseStockSnapshot.objects.filter(
            agency=self.agency,
            sku_code="SKU-100",
            is_archived=False,
        ).order_by("id").first()
        reserve = WarehouseReserve.objects.get(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_id="PROC-101",
        )
        self.assertIsNotNone(snapshot)
        self.assertEqual(operation.status, WarehouseOperation.STATUS_DONE)
        self.assertEqual(snapshot.warehouse_state_code, "in_processing_zone")
        self.assertEqual(snapshot.zone_code, "OBR")
        self.assertEqual(snapshot.processing_reserved_qty, 5)
        self.assertEqual(reserve.qty_reserved, 5)

    def test_complete_processing_move_autostarts_processing_when_order_already_taken(self):
        driver_user = get_user_model().objects.create_user(username="reachtruck_driver_processing_auto", password="pwd")
        driver_employee = Employee.objects.create(
            full_name="Водитель Обработки Автостарт",
            role="reachtruck_driver",
            user=driver_user,
            is_active=True,
        )
        move_warehouse_pallet(
            agency=self.agency,
            pallet_code="PAL-100",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=2,
            location="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 2",
        )
        OrderAuditEntry.objects.create(
            order_id="PROC-102",
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
            },
        )
        _replace_processing_reserves(
            "PROC-102",
            self.agency,
            [
                {
                    "sku": "SKU-100",
                    "barcode": "200000000100",
                    "goods_type": "gv",
                    "qty": 5,
                }
            ],
        )
        response = self.client.post(
            "/reachtruck/requests/create/",
            data={
                "processing_order_id": "PROC-102",
                "to_zone": "OBR",
                "agency_id": str(self.agency.id),
                "request_items_json": json.dumps(
                    [
                        {
                            "requested_article": "SKU-100",
                            "requested_goods_type": "gv",
                            "requested_qty": 5,
                            "requested_barcodes": ["200000000100"],
                        }
                    ]
                ),
            },
        )
        self.assertEqual(response.status_code, 200)
        task = MoveTask.objects.get()
        task.status = MoveTask.STATUS_IN_PROGRESS
        task.assigned_to = driver_user
        task.assigned_to_name = driver_employee.full_name
        payload = dict(task.payload or {})
        payload["status"] = MoveTask.STATUS_IN_PROGRESS
        payload["assigned_to_id"] = driver_employee.id
        payload["assigned_to_name"] = driver_employee.full_name
        task.payload = payload
        task.save(update_fields=["status", "assigned_to", "assigned_to_name", "payload", "updated_at"])

        complete_result = complete_move_task(
            legacy_order_id=task.legacy_order_id,
            user=driver_user,
            employee_id=driver_employee.id,
            employee_name=driver_employee.full_name,
        )

        self.assertTrue(complete_result.ok, complete_result.error)
        processing_operation = WarehouseOperation.objects.get(
            agency=self.agency,
            operation_type=WarehouseOperation.TYPE_PROCESSING,
            context_type="processing",
            context_id="PROC-102",
        )
        snapshot = WarehouseStockSnapshot.objects.filter(
            agency=self.agency,
            sku_code="SKU-100",
            is_archived=False,
        ).order_by("id").first()
        self.assertIsNotNone(snapshot)
        self.assertEqual(processing_operation.status, WarehouseOperation.STATUS_IN_PROGRESS)
        self.assertEqual(snapshot.warehouse_state_code, "processing_in_progress")
        self.assertEqual(snapshot.active_operation_id, processing_operation.id)

    def test_create_move_request_blocks_duplicate_processing_delivery_when_item_already_in_obr(self):
        existing_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_PROCESSING,
            context_id="PROC-DONE-OBR",
            agency=self.agency,
            requested_by=self.user,
            requested_by_role="processing_head",
            requested_by_name="Руководитель Обработки",
            destination_zone="OBR",
            status=MoveRequest.STATUS_DONE,
        )
        MoveRequestItem.objects.create(
            request=existing_request,
            sku_code="SKU-100",
            barcode="200000000100",
            goods_type="gv",
            qty_requested=5,
            qty_planned=5,
            qty_done=5,
        )

        response = self.client.post(
            "/reachtruck/requests/create/",
            data={
                "processing_order_id": "PROC-DONE-OBR",
                "to_zone": "OBR",
                "agency_id": str(self.agency.id),
                "request_items_json": json.dumps(
                    [
                        {
                            "requested_article": "SKU-100",
                            "requested_goods_type": "gv",
                            "requested_qty": 5,
                            "requested_barcodes": ["200000000100"],
                        }
                    ]
                ),
            },
        )

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertFalse(payload.get("ok"))
        self.assertEqual(int(payload.get("tasks_created") or 0), 0)
        self.assertEqual(payload.get("status"), "done")
        self.assertIn("уже доставлен в OBR", payload.get("error") or "")
        self.assertEqual(MoveRequest.objects.filter(context_type="processing", context_id="PROC-DONE-OBR").count(), 1)

    def test_create_move_request_blocks_duplicate_processing_delivery_by_warehouse_truth(self):
        snapshot = create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="legacy_stock",
            order_id="PROC-TRUTH-OBR",
            sku="SKU-100",
            barcode="200000000100",
            goods_type="gv",
            qty=12,
            available_qty=0,
            processing_reserved_qty=12,
            pallet_code="PAL-TRUTH-OBR",
            zone="OBR",
            location="OBR · Зона обработки",
        )
        snapshot.warehouse_state_code = "processing_in_progress"
        snapshot.save(update_fields=["warehouse_state_code", "updated_at"])
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="PROC-TRUTH-OBR",
            sku_code="SKU-100",
            size=str(snapshot.size or ""),
            barcode="200000000100",
            goods_type="gv",
            qty_reserved=5,
            status=WarehouseReserve.STATUS_ACTIVE,
            source_document_type="processing_order",
            source_document_id="PROC-TRUTH-OBR",
            created_by=self.user,
        )

        response = self.client.post(
            "/reachtruck/requests/create/",
            data={
                "processing_order_id": "PROC-TRUTH-OBR",
                "to_zone": "OBR",
                "agency_id": str(self.agency.id),
                "request_items_json": json.dumps(
                    [
                        {
                            "requested_article": "SKU-100",
                            "requested_goods_type": "gv",
                            "requested_qty": 5,
                            "requested_barcodes": ["200000000100"],
                        }
                    ]
                ),
            },
        )

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertFalse(payload.get("ok"))
        self.assertEqual(int(payload.get("tasks_created") or 0), 0)
        self.assertEqual(payload.get("status"), "done")
        self.assertIn("уже находится в OBR", payload.get("error") or "")
        self.assertEqual(MoveRequest.objects.filter(context_type="processing", context_id="PROC-TRUTH-OBR").count(), 0)

    def test_create_move_request_partial_when_qty_short(self):
        response = self.client.post(
            "/reachtruck/requests/create/",
            data={
                "to_zone": "OBR",
                "agency_id": str(self.agency.id),
                "request_items_json": json.dumps(
                    [
                        {
                            "requested_article": "SKU-100",
                            "requested_goods_type": "gv",
                            "requested_qty": 20,
                            "requested_barcodes": ["200000000100"],
                        }
                    ]
                ),
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("ok"))
        self.assertGreater(int(payload.get("shortage_qty") or 0), 0)
        request_obj = MoveRequest.objects.first()
        self.assertEqual(request_obj.status, MoveRequest.STATUS_PARTIAL)

    def test_create_move_request_uses_explicit_requested_rows(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="R-102",
            sku="SKU-100",
            barcode="200000000100",
            goods_type="gv",
            qty=100,
            box_code="BOX-100",
            pallet_code="PAL-100",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=2,
            location="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 2",
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="R-102",
            sku="SKU-100",
            barcode="200000000101",
            goods_type="gv",
            qty=100,
            box_code="BOX-101",
            pallet_code="PAL-100",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=2,
            location="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 2",
        )
        OrderAuditEntry.objects.create(
            order_id="R-102",
            order_type="receiving",
            action="status",
            agency=self.agency,
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_pallets": [
                    {
                        "code": "PAL-100",
                        "boxes": ["BOX-100", "BOX-101"],
                        "items": [],
                        "location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 2},
                    }
                ],
                "act_boxes": [
                    {"code": "BOX-100", "items": [{"sku": "SKU-100", "barcode": "200000000100", "qty": 100}]},
                    {"code": "BOX-101", "items": [{"sku": "SKU-100", "barcode": "200000000101", "qty": 100}]},
                ],
            },
        )
        response = self.client.post(
            "/reachtruck/requests/create/",
            data={
                "processing_order_id": "1",
                "to_zone": "OBR",
                "agency_id": str(self.agency.id),
                "request_items_json": json.dumps(
                    [
                        {
                            "requested_article": "SKU-100",
                            "requested_goods_type": "gv",
                            "requested_qty": 40,
                            "requested_barcodes": ["200000000100", "200000000101"],
                        }
                    ]
                ),
                "requested_rows_json": json.dumps(
                    [
                        {
                            "pallet_code": "PAL-100",
                            "box_code": "BOX-100",
                            "qty": 20,
                            "barcode_qty": {"200000000100": 20},
                            "requested_article": "SKU-100",
                            "requested_goods_type": "gv",
                            "requested_barcodes": ["200000000100"],
                        },
                        {
                            "pallet_code": "PAL-100",
                            "box_code": "BOX-101",
                            "qty": 20,
                            "barcode_qty": {"200000000101": 20},
                            "requested_article": "SKU-100",
                            "requested_goods_type": "gv",
                            "requested_barcodes": ["200000000101"],
                        },
                    ]
                ),
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("ok"))
        task = MoveTask.objects.get()
        self.assertEqual(task.move_mode, MOVE_MODE_BOX_PARTIAL)
        self.assertEqual(task.payload.get("from_code"), "0-1/1-2")
        self.assertEqual(task.payload.get("to_code"), "OBR")
        self.assertEqual(task.payload.get("source_code"), "0-1/1-2")
        self.assertEqual(task.payload.get("destination_code"), "OBR")
        self.assertEqual(
            task.payload.get("requested_rows"),
            [
                {"box_code": "BOX-100", "qty": 20, "barcode_qty": {"200000000100": 20}},
                {"box_code": "BOX-101", "qty": 20, "barcode_qty": {"200000000101": 20}},
            ],
        )
        self.assertEqual(
            task.payload.get("requested_barcode_qty"),
            {"200000000100": 20, "200000000101": 20},
        )
        plan_rows = _pallet_box_plan(task.payload, "PAL-100", agency_id=self.agency.id)
        deliver_codes = [row["box_code"] for row in plan_rows if row["action"] == "Доставить"]
        self.assertEqual(deliver_codes, ["BOX-100", "BOX-101"])

    def test_processing_request_uses_box_full_for_full_box_pick_from_shared_pallet(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="R-103",
            sku="SKU-100",
            barcode="200000000110",
            goods_type="gv",
            qty=25,
            box_code="BOX-110",
            pallet_code="PAL-110",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=3,
            location="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 3",
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="R-103",
            sku="SKU-100",
            barcode="200000000111",
            goods_type="gv",
            qty=25,
            box_code="BOX-111",
            pallet_code="PAL-110",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=3,
            location="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 3",
        )
        _replace_processing_reserves(
            "PROC-BOX-1",
            self.agency,
            [
                {
                    "sku": "SKU-100",
                    "barcode": "200000000110",
                    "goods_type": "gv",
                    "qty": 25,
                }
            ],
        )

        response = self.client.post(
            "/reachtruck/requests/create/",
            data={
                "processing_order_id": "PROC-BOX-1",
                "to_zone": "OBR",
                "agency_id": str(self.agency.id),
                "request_items_json": json.dumps(
                    [
                        {
                            "requested_article": "SKU-100",
                            "requested_goods_type": "gv",
                            "requested_qty": 25,
                            "requested_barcodes": ["200000000110"],
                        }
                    ]
                ),
                "requested_rows_json": json.dumps(
                    [
                        {
                            "pallet_code": "PAL-110",
                            "box_code": "BOX-110",
                            "qty": 25,
                            "barcode_qty": {"200000000110": 25},
                            "requested_article": "SKU-100",
                            "requested_goods_type": "gv",
                            "requested_barcodes": ["200000000110"],
                        }
                    ]
                ),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get("ok"))
        task = MoveTask.objects.get()
        payload = dict(task.payload or {})
        self.assertEqual(task.move_mode, MOVE_MODE_BOX_FULL)
        self.assertEqual(payload.get("requested_boxes"), ["BOX-110"])
        self.assertIn("сними короба: BOX-110", payload.get("instruction") or "")
        self.assertIn("Палету верни на исходное место", payload.get("instruction") or "")
        operation = WarehouseOperation.objects.get(id=payload["warehouse_operation_id"])
        warehouse_task = operation.tasks.get()
        self.assertEqual(warehouse_task.task_type, WarehouseOperationTask.TYPE_BOX_MOVE)
        self.assertEqual(warehouse_task.container.container_code, "BOX-110")

    def test_processing_request_blocks_same_pallet_while_active_obr_task_exists(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="R-104",
            sku="SKU-100",
            barcode="200000000120",
            goods_type="gv",
            qty=25,
            box_code="BOX-120",
            pallet_code="PAL-120",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=4,
            location="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 4",
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="R-104",
            sku="SKU-100",
            barcode="200000000121",
            goods_type="gv",
            qty=25,
            box_code="BOX-121",
            pallet_code="PAL-120",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=4,
            location="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 4",
        )
        _replace_processing_reserves(
            "PROC-BLOCK-1",
            self.agency,
            [
                {
                    "sku": "SKU-100",
                    "barcode": "200000000120",
                    "goods_type": "gv",
                    "qty": 25,
                }
            ],
        )
        first_response = self.client.post(
            "/reachtruck/requests/create/",
            data={
                "processing_order_id": "PROC-BLOCK-1",
                "to_zone": "OBR",
                "agency_id": str(self.agency.id),
                "request_items_json": json.dumps(
                    [
                        {
                            "requested_article": "SKU-100",
                            "requested_goods_type": "gv",
                            "requested_qty": 25,
                            "requested_barcodes": ["200000000120"],
                        }
                    ]
                ),
                "requested_rows_json": json.dumps(
                    [
                        {
                            "pallet_code": "PAL-120",
                            "box_code": "BOX-120",
                            "qty": 25,
                            "barcode_qty": {"200000000120": 25},
                            "requested_article": "SKU-100",
                            "requested_goods_type": "gv",
                            "requested_barcodes": ["200000000120"],
                        }
                    ]
                ),
            },
        )
        self.assertEqual(first_response.status_code, 200)
        self.assertTrue(first_response.json().get("ok"))

        _replace_processing_reserves(
            "PROC-BLOCK-2",
            self.agency,
            [
                {
                    "sku": "SKU-100",
                    "barcode": "200000000121",
                    "goods_type": "gv",
                    "qty": 25,
                }
            ],
        )
        second_response = self.client.post(
            "/reachtruck/requests/create/",
            data={
                "processing_order_id": "PROC-BLOCK-2",
                "to_zone": "OBR",
                "agency_id": str(self.agency.id),
                "request_items_json": json.dumps(
                    [
                        {
                            "requested_article": "SKU-100",
                            "requested_goods_type": "gv",
                            "requested_qty": 25,
                            "requested_barcodes": ["200000000121"],
                        }
                    ]
                ),
                "requested_rows_json": json.dumps(
                    [
                        {
                            "pallet_code": "PAL-120",
                            "box_code": "BOX-121",
                            "qty": 25,
                            "barcode_qty": {"200000000121": 25},
                            "requested_article": "SKU-100",
                            "requested_goods_type": "gv",
                            "requested_barcodes": ["200000000121"],
                        }
                    ]
                ),
            },
        )

        self.assertEqual(second_response.status_code, 400)
        payload = second_response.json()
        self.assertFalse(payload.get("ok"))
        self.assertEqual(int(payload.get("tasks_created") or 0), 0)
        self.assertIn("Паллета уже занята активным заданием", payload.get("error") or "")

    def test_lookup_item_pallets_returns_short_location_code_for_processing_modal(self):
        OrderAuditEntry.objects.create(
            order_id="PROC-LOOKUP-1",
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={"status": "processing_in_work"},
        )
        move_warehouse_pallet(
            agency=self.agency,
            pallet_code="PAL-100",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=2,
            location="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 2",
        )

        response = self.client.get(
            "/reachtruck/lookup-item/",
            data={
                "processing_order_id": "PROC-LOOKUP-1",
                "barcode": "200000000100",
                "include_moves": "1",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("ok"))
        self.assertEqual(len(payload.get("pallets") or []), 1)
        pallet = payload["pallets"][0]
        self.assertEqual(pallet.get("location"), "0-1/1-2")
        self.assertEqual(pallet.get("location_label"), "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 2")

    def test_create_move_request_prefers_minimal_number_of_pallets(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="R-101",
            sku="SKU-100",
            barcode="200000000100",
            goods_type="gv",
            qty=20,
            pallet_code="PAL-200",
            zone="OS",
            location="OS",
        )

        response = self.client.post(
            "/reachtruck/requests/create/",
            data={
                "to_zone": "OBR",
                "agency_id": str(self.agency.id),
                "request_items_json": json.dumps(
                    [
                        {
                            "requested_article": "SKU-100",
                            "requested_goods_type": "gv",
                            "requested_qty": 15,
                            "requested_barcodes": ["200000000100"],
                        }
                    ]
                ),
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("ok"))
        self.assertEqual(int(payload.get("tasks_created") or 0), 1)
        self.assertEqual(MoveTask.objects.count(), 1)
        task = MoveTask.objects.first()
        self.assertEqual(task.pallet_code, "PAL-200")
        self.assertEqual(task.move_mode, MOVE_MODE_BOX_PARTIAL)
        self.assertEqual(task.qty_planned, 15)

    def test_create_stock_move_task_persists_generated_ids_in_task_payload(self):
        move_id = create_stock_move_task(
            user=self.user,
            agency=self.agency,
            description="Тестовая задача ричтрака",
            requested_by_name="Руководитель Обработки",
            requested_by_role="processing_head",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "pallet_code": "PAL-100",
                "from_location": {"zone": "PR", "row": "", "section": "", "tier": "", "cell": ""},
                "to_location": {"zone": "OBR", "row": "", "section": "", "tier": "", "cell": ""},
                "from_label": "PR · Зона приемки",
                "to_label": "OBR · Зона обработки",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
                "processing_order_id": "1",
                "instruction": "Возьми палету PAL-100 целиком и доставь в OBR.",
            },
        )

        task = MoveTask.objects.get(legacy_order_id=move_id)

        self.assertEqual(task.payload.get("move_request_id"), task.request_id)
        self.assertEqual(task.payload.get("move_task_id"), task.id)

    def test_manual_move_creation_is_disabled(self):
        response = self.client.post(
            "/reachtruck/",
            data={
                "action": "create_move",
                "pallet_code": "PAL-OBR",
                "to_zone": "PR",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json(),
            {
                "ok": False,
                "error": (
                    "Ручное создание заданий отключено. Используйте автоматическое "
                    "планирование по потребности или сценарий перемещения на хранение."
                ),
            },
        )

    def test_dashboard_shows_single_box_execution_plan_for_partial_pick(self):
        for box_code, barcode in [
            ("BOX-31", "2000215562629"),
            ("BOX-32", "2000215562636"),
            ("BOX-36", "2000215562667"),
        ]:
            create_warehouse_snapshot_row(
                agency=self.agency,
                order_type="receiving",
                order_id="R-BOX-1",
                sku="SKU-100",
                barcode=barcode,
                goods_type="gv",
                qty=50,
                box_code=box_code,
                pallet_code="PAL-BOX-1",
                zone="OS",
                row=1,
                section=1,
                tier=1,
                cell=1,
                location="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 1",
            )
        create_stock_move_task(
            user=self.user,
            agency=self.agency,
            description="Частичный отбор по строкам",
            requested_by_name="Руководитель Обработки",
            requested_by_role="processing_head",
            payload={
                "status": "created",
                "status_label": "Ожидает отбора по потребности",
                "pallet_code": "PAL-BOX-1",
                "from_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
                "to_location": {"zone": "OBR", "row": "", "section": "", "tier": "", "cell": ""},
                "move_mode": MOVE_MODE_BOX_PARTIAL,
                "pick_mode": "partial",
                "requested_qty": 120,
                "requested_rows": [
                    {"box_code": "BOX-31", "qty": 20, "barcode_qty": {"2000215562629": 20}},
                    {"box_code": "BOX-32", "qty": 20, "barcode_qty": {"2000215562636": 20}},
                    {"box_code": "BOX-36", "qty": 20, "barcode_qty": {"2000215562667": 20}},
                ],
                "requested_barcode_qty": {
                    "2000215562629": 20,
                    "2000215562636": 20,
                    "2000215562667": 20,
                },
                "instruction": "Возьми палету PAL-BOX-1, отберите 120 шт. по потребности и доставь в OBR.",
            },
        )

        response = self.client.get("/reachtruck/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Потребность заявки")
        self.assertContains(response, "План отбора по коробам")
        self.assertContains(response, "В OBR")
        self.assertContains(response, "Останется")
        self.assertContains(response, "Вернуть назад")
        self.assertContains(response, "2000215562629")
        self.assertNotContains(response, "Что нужно подать")
        self.assertNotContains(response, "План по коробам с палеты")

    def test_complete_partial_obr_move_updates_warehouse_snapshot_without_audit(self):
        driver_user = get_user_model().objects.create_user(username="reachtruck_driver_obr_reserve", password="pwd")
        driver_employee = Employee.objects.create(
            full_name="Водитель Ричтрака OBR",
            role="reachtruck_driver",
            user=driver_user,
            is_active=True,
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="R-OBR-1",
            sku="SKU-100",
            name="Товар 100",
            size="31",
            barcode="2000215562629",
            goods_type="gv",
            qty=100,
            box_code="BOX-31",
            pallet_code="PAL-OBR-1",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=1,
            location="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 1",
            available_qty=100,
        )
        move_id = create_stock_move_task(
            user=self.user,
            agency=self.agency,
            description="Частичный отбор в OBR",
            requested_by_name="Руководитель Обработки",
            requested_by_role="processing_head",
            payload={
                "status": "created",
                "status_label": "Ожидает отбора по потребности",
                "pallet_code": "PAL-OBR-1",
                "from_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
                "to_location": {"zone": "OBR", "row": "", "section": "", "tier": "", "cell": ""},
                "move_mode": MOVE_MODE_BOX_PARTIAL,
                "pick_mode": "partial",
                "requested_qty": 20,
                "processing_order_id": "1",
                "requested_rows": [
                    {"box_code": "BOX-31", "qty": 20, "barcode_qty": {"2000215562629": 20}},
                ],
                "requested_barcode_qty": {"2000215562629": 20},
                "request_items": [
                    {
                        "requested_article": "SKU-100",
                        "requested_goods_type": "gv",
                        "requested_qty": 20,
                        "requested_barcodes": ["2000215562629"],
                    }
                ],
                "requested_goods_type": "gv",
                "instruction": "Возьми палету PAL-OBR-1, отберите 20 шт. и доставь в OBR.",
            },
        )

        take_result = take_move_task(
            legacy_order_id=move_id,
            user=driver_user,
            employee_id=driver_employee.id,
            employee_name=driver_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)

        complete_result = complete_move_task(
            legacy_order_id=move_id,
            user=driver_user,
            employee_id=driver_employee.id,
            employee_name=driver_employee.full_name,
        )

        self.assertTrue(complete_result.ok, complete_result.error)
        stock_row = WarehouseStockSnapshot.objects.get(
            agency=self.agency,
            container__container_code="BOX-31",
            is_archived=False,
        )
        self.assertEqual(stock_row.qty, 80)
        self.assertEqual(stock_row.available_qty, 80)

    def test_complete_partial_obr_move_completes_linked_warehouse_operation(self):
        driver_user = get_user_model().objects.create_user(username="reachtruck_driver_obr_linked_partial", password="pwd")
        driver_employee = Employee.objects.create(
            full_name="Водитель Ричтрака OBR partial",
            role="reachtruck_driver",
            user=driver_user,
            is_active=True,
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="R-OBR-LINKED-PARTIAL",
            sku="SKU-100",
            name="Товар 100",
            size="31",
            barcode="BAR-OBR-LINKED-PARTIAL",
            goods_type="gv",
            qty=100,
            box_code="BOX-OBR-LINKED-PARTIAL",
            pallet_code="PAL-OBR-LINKED-PARTIAL",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=1,
            location="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 1",
            available_qty=100,
        )
        WarehouseWritePathService.reserve_for_processing(
            agency=self.agency,
            order_id="PROC-OBR-LINKED-PARTIAL",
            items=[
                {
                    "sku_code": "SKU-100",
                    "barcode": "BAR-OBR-LINKED-PARTIAL",
                    "goods_type": "gv",
                    "qty": 20,
                }
            ],
            created_by=self.user,
        )
        operation = WarehouseWritePathService.request_move_to_processing(
            agency=self.agency,
            order_id="PROC-OBR-LINKED-PARTIAL",
            container_codes=["BOX-OBR-LINKED-PARTIAL"],
            requested_by=self.user,
        )
        move_id = create_stock_move_task(
            user=self.user,
            agency=self.agency,
            description="Частичный отбор в OBR со складской операцией",
            requested_by_name="Руководитель Обработки",
            requested_by_role="processing_head",
            payload={
                "status": "created",
                "status_label": "Ожидает отбора по потребности",
                "pallet_code": "PAL-OBR-LINKED-PARTIAL",
                "from_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
                "to_location": {"zone": "OBR", "row": "", "section": "", "tier": "", "cell": ""},
                "move_mode": MOVE_MODE_BOX_PARTIAL,
                "pick_mode": "partial",
                "requested_qty": 20,
                "processing_order_id": "PROC-OBR-LINKED-PARTIAL",
                "warehouse_operation_id": operation.id,
                "requested_rows": [
                    {
                        "box_code": "BOX-OBR-LINKED-PARTIAL",
                        "qty": 20,
                        "barcode_qty": {"BAR-OBR-LINKED-PARTIAL": 20},
                    },
                ],
                "requested_barcode_qty": {"BAR-OBR-LINKED-PARTIAL": 20},
                "request_items": [
                    {
                        "requested_article": "SKU-100",
                        "requested_goods_type": "gv",
                        "requested_qty": 20,
                        "requested_barcodes": ["BAR-OBR-LINKED-PARTIAL"],
                    }
                ],
                "requested_goods_type": "gv",
            },
        )

        take_result = take_move_task(
            legacy_order_id=move_id,
            user=driver_user,
            employee_id=driver_employee.id,
            employee_name=driver_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)
        complete_result = complete_move_task(
            legacy_order_id=move_id,
            user=driver_user,
            employee_id=driver_employee.id,
            employee_name=driver_employee.full_name,
        )

        self.assertTrue(complete_result.ok, complete_result.error)
        operation.refresh_from_db()
        operation_task = operation.tasks.get()
        self.assertEqual(operation.status, WarehouseOperation.STATUS_DONE)
        self.assertEqual(operation.done_qty, 20)
        self.assertEqual(operation_task.status, WarehouseOperationTask.STATUS_DONE)
        self.assertEqual(operation_task.qty_done, 20)
        self.assertEqual(
            WarehouseEvent.objects.filter(
                stock_context_type="processing",
                stock_context_id="PROC-OBR-LINKED-PARTIAL",
                event_type="processing_zone_arrived",
                source_document_id=move_id,
                operation=operation,
            ).count(),
            1,
        )

    def test_complete_full_box_obr_move_completes_linked_warehouse_operation(self):
        driver_user = get_user_model().objects.create_user(username="reachtruck_driver_obr_linked_box", password="pwd")
        driver_employee = Employee.objects.create(
            full_name="Водитель Ричтрака OBR box",
            role="reachtruck_driver",
            user=driver_user,
            is_active=True,
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="R-OBR-LINKED-BOX",
            sku="SKU-100",
            name="Товар 100",
            size="31",
            barcode="BAR-OBR-LINKED-BOX",
            goods_type="gv",
            qty=25,
            box_code="BOX-OBR-LINKED-BOX",
            pallet_code="PAL-OBR-LINKED-BOX",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=1,
            location="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 1",
            available_qty=25,
        )
        WarehouseWritePathService.reserve_for_processing(
            agency=self.agency,
            order_id="PROC-OBR-LINKED-BOX",
            items=[
                {
                    "sku_code": "SKU-100",
                    "barcode": "BAR-OBR-LINKED-BOX",
                    "goods_type": "gv",
                    "qty": 25,
                }
            ],
            created_by=self.user,
        )
        operation = WarehouseWritePathService.request_move_to_processing(
            agency=self.agency,
            order_id="PROC-OBR-LINKED-BOX",
            container_codes=["BOX-OBR-LINKED-BOX"],
            requested_by=self.user,
        )
        move_id = create_stock_move_task(
            user=self.user,
            agency=self.agency,
            description="Полный короб в OBR со складской операцией",
            requested_by_name="Руководитель Обработки",
            requested_by_role="processing_head",
            payload={
                "status": "created",
                "status_label": "Ожидает отбора короба",
                "pallet_code": "PAL-OBR-LINKED-BOX",
                "from_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
                "to_location": {"zone": "OBR", "row": "", "section": "", "tier": "", "cell": ""},
                "move_mode": MOVE_MODE_BOX_FULL,
                "pick_mode": "full",
                "requested_qty": 25,
                "processing_order_id": "PROC-OBR-LINKED-BOX",
                "warehouse_operation_id": operation.id,
                "requested_boxes": ["BOX-OBR-LINKED-BOX"],
                "request_items": [
                    {
                        "requested_article": "SKU-100",
                        "requested_goods_type": "gv",
                        "requested_qty": 25,
                        "requested_barcodes": ["BAR-OBR-LINKED-BOX"],
                    }
                ],
                "requested_goods_type": "gv",
            },
        )

        take_result = take_move_task(
            legacy_order_id=move_id,
            user=driver_user,
            employee_id=driver_employee.id,
            employee_name=driver_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)
        complete_result = complete_move_task(
            legacy_order_id=move_id,
            user=driver_user,
            employee_id=driver_employee.id,
            employee_name=driver_employee.full_name,
            require_scan_confirmation=False,
        )

        self.assertTrue(complete_result.ok, complete_result.error)
        operation.refresh_from_db()
        operation_task = operation.tasks.get()
        moved_snapshot = WarehouseStockSnapshot.objects.get(
            agency=self.agency,
            container__container_code="BOX-OBR-LINKED-BOX",
            is_archived=False,
        )
        self.assertEqual(operation.status, WarehouseOperation.STATUS_DONE)
        self.assertEqual(operation.done_qty, 25)
        self.assertEqual(operation_task.status, WarehouseOperationTask.STATUS_DONE)
        self.assertEqual(operation_task.qty_done, 25)
        self.assertEqual(moved_snapshot.zone_code, "OBR")
        self.assertEqual(moved_snapshot.active_operation_id, None)
        self.assertEqual(
            WarehouseEvent.objects.filter(
                stock_context_type="processing",
                stock_context_id="PROC-OBR-LINKED-BOX",
                event_type="processing_zone_arrived",
                source_document_id=move_id,
                operation=operation,
            ).count(),
            1,
        )

    def test_complete_move_updates_task_payload_for_full_pallet_otg(self):
        driver_user = get_user_model().objects.create_user(username="reachtruck_driver_test", password="pwd")
        driver_employee = Employee.objects.create(
            full_name="Водитель Ричтрака",
            role="reachtruck_driver",
            user=driver_user,
            is_active=True,
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="R-200",
            sku="SKU-100",
            barcode="200000000100",
            goods_type="gv",
            qty=12,
            box_code="BOX-OTG",
            pallet_code="PAL-OTG",
            zone="PR",
            location="PR · Зона приемки",
        )
        move_id = create_stock_move_task(
            user=self.user,
            agency=self.agency,
            description="Тестовая доставка в OTG",
            requested_by_name="Руководитель Обработки",
            requested_by_role="processing_head",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "pallet_code": "PAL-OTG",
                "from_location": {"zone": "PR", "row": "", "section": "", "tier": "", "cell": ""},
                "to_location": {"zone": "OTG", "row": "", "section": "", "tier": "", "cell": ""},
                "from_label": "PR · Зона приемки",
                "to_label": "OTG · Зона отгрузки",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
                "requested_boxes": [],
                "requested_box": "",
                "requested_rows": [],
                "requested_barcodes": [],
                "requested_barcode_qty": {},
                "requested_goods_type": "",
                "instruction": "Возьми палету PAL-OTG целиком и доставь в OTG · Зона отгрузки.",
            },
        )

        self.client.logout()
        self.client.login(username="reachtruck_driver_test", password="pwd")

        take_response = self.client.post(
            "/reachtruck/",
            data={"action": "take_move", "order_id": move_id},
        )
        self.assertEqual(take_response.status_code, 302)
        task = MoveTask.objects.get(legacy_order_id=move_id)
        self.assertEqual((task.payload or {}).get("status"), MoveTask.STATUS_IN_PROGRESS)
        self.assertEqual((task.payload or {}).get("assigned_to_id"), driver_employee.id)

        complete_response = self.client.post(
            "/reachtruck/",
            data={"action": "complete_move", "order_id": move_id},
        )

        self.assertEqual(complete_response.status_code, 302)
        self.assertEqual(complete_response["Location"], "/reachtruck/")

        task = MoveTask.objects.get(legacy_order_id=move_id)
        self.assertEqual(task.status, MoveTask.STATUS_DONE)
        self.assertEqual((task.payload or {}).get("status"), "done")
        self.assertEqual((task.payload or {}).get("status_label"), "Короба доставлены в OTG, паллета удалена")

    def test_take_and_complete_move_services_update_task_payload(self):
        driver_user = get_user_model().objects.create_user(username="reachtruck_driver_service", password="pwd")
        driver_employee = Employee.objects.create(
            full_name="Водитель Сервис",
            role="reachtruck_driver",
            user=driver_user,
            is_active=True,
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="R-201",
            sku="SKU-100",
            barcode="200000000100",
            goods_type="gv",
            qty=12,
            box_code="BOX-SERVICE",
            pallet_code="PAL-SERVICE",
            zone="PR",
            location="PR · Зона приемки",
        )
        move_id = create_stock_move_task(
            user=self.user,
            agency=self.agency,
            description="Тест сервисного цикла",
            requested_by_name="Руководитель Обработки",
            requested_by_role="processing_head",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "pallet_code": "PAL-SERVICE",
                "from_location": {"zone": "PR", "row": "", "section": "", "tier": "", "cell": ""},
                "to_location": {"zone": "OTG", "row": "", "section": "", "tier": "", "cell": ""},
                "from_label": "PR · Зона приемки",
                "to_label": "OTG · Зона отгрузки",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
                "requested_boxes": [],
                "requested_box": "",
                "requested_rows": [],
                "requested_barcodes": [],
                "requested_barcode_qty": {},
                "requested_goods_type": "",
                "instruction": "Возьми палету PAL-SERVICE целиком и доставь в OTG · Зона отгрузки.",
            },
        )

        take_result = take_move_task(
            legacy_order_id=move_id,
            user=driver_user,
            employee_id=driver_employee.id,
            employee_name=driver_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)
        task = MoveTask.objects.get(legacy_order_id=move_id)
        self.assertEqual(task.status, MoveTask.STATUS_IN_PROGRESS)
        self.assertEqual((task.payload or {}).get("status"), MoveTask.STATUS_IN_PROGRESS)
        self.assertEqual((task.payload or {}).get("assigned_to_id"), driver_employee.id)

        complete_result = complete_move_task(
            legacy_order_id=move_id,
            user=driver_user,
            employee_id=driver_employee.id,
            employee_name=driver_employee.full_name,
        )
        self.assertTrue(complete_result.ok, complete_result.error)
        task.refresh_from_db()
        self.assertEqual(task.status, MoveTask.STATUS_DONE)
        self.assertEqual((task.payload or {}).get("status"), MoveTask.STATUS_DONE)
        self.assertEqual((task.payload or {}).get("status_label"), "Короба доставлены в OTG, паллета удалена")

    def test_take_and_complete_move_services_use_operational_stock_without_placement_audit(self):
        driver_user = get_user_model().objects.create_user(username="reachtruck_driver_operational", password="pwd")
        driver_employee = Employee.objects.create(
            full_name="Водитель Оперативный",
            role="reachtruck_driver",
            user=driver_user,
            is_active=True,
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="R-203",
            sku="SKU-100",
            name="Товар 100",
            size="42",
            barcode="200000000100",
            goods_type="gv",
            qty=12,
            box_code="BOX-OP",
            pallet_code="PAL-OP",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=1,
            location="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 1",
        )
        move_id = create_stock_move_task(
            user=self.user,
            agency=self.agency,
            description="Тест оперативного склада",
            requested_by_name="Руководитель Обработки",
            requested_by_role="processing_head",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "pallet_code": "PAL-OP",
                "from_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
                "to_location": {"zone": "OTG", "row": "", "section": "", "tier": "", "cell": ""},
                "from_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 1",
                "to_label": "OTG · Зона отгрузки",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
                "requested_boxes": [],
                "requested_box": "",
                "requested_rows": [],
                "requested_barcodes": [],
                "requested_barcode_qty": {},
                "requested_goods_type": "",
                "instruction": "Возьми палету PAL-OP целиком и доставь в OTG · Зона отгрузки.",
            },
        )

        take_result = take_move_task(
            legacy_order_id=move_id,
            user=driver_user,
            employee_id=driver_employee.id,
            employee_name=driver_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)

        complete_result = complete_move_task(
            legacy_order_id=move_id,
            user=driver_user,
            employee_id=driver_employee.id,
            employee_name=driver_employee.full_name,
        )
        self.assertTrue(complete_result.ok, complete_result.error)

        task = MoveTask.objects.get(legacy_order_id=move_id)
        self.assertEqual(task.status, MoveTask.STATUS_DONE)

        moved_row = WarehouseStockSnapshot.objects.get(
            agency=self.agency,
            container__container_code="BOX-OP",
            is_archived=False,
        )
        self.assertEqual(moved_row.parent_container_id, None)
        self.assertEqual(moved_row.zone_code, "OTG")
        self.assertEqual(moved_row.location.display_name, "OTG · Зона отгрузки")
        self.assertEqual(moved_row.available_qty, 12)

    def test_complete_move_uses_task_status_when_payload_status_is_stale(self):
        driver_user = get_user_model().objects.create_user(username="reachtruck_driver_stale", password="pwd")
        driver_employee = Employee.objects.create(
            full_name="Водитель С рассинхроном",
            role="reachtruck_driver",
            user=driver_user,
            is_active=True,
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="R-202",
            sku="SKU-100",
            barcode="200000000100",
            goods_type="gv",
            qty=12,
            box_code="BOX-STALE",
            pallet_code="PAL-STALE",
            zone="PR",
            location="PR · Зона приемки",
        )
        move_id = create_stock_move_task(
            user=self.user,
            agency=self.agency,
            description="Тест рассинхрона статуса",
            requested_by_name="Руководитель Обработки",
            requested_by_role="processing_head",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "pallet_code": "PAL-STALE",
                "from_location": {"zone": "PR", "row": "", "section": "", "tier": "", "cell": ""},
                "to_location": {"zone": "OTG", "row": "", "section": "", "tier": "", "cell": ""},
                "from_label": "PR · Зона приемки",
                "to_label": "OTG · Зона отгрузки",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
                "requested_boxes": [],
                "requested_box": "",
                "requested_rows": [],
                "requested_barcodes": [],
                "requested_barcode_qty": {},
                "requested_goods_type": "",
                "instruction": "Возьми палету PAL-STALE целиком и доставь в OTG · Зона отгрузки.",
            },
        )

        task = MoveTask.objects.get(legacy_order_id=move_id)
        task.status = MoveTask.STATUS_IN_PROGRESS
        task.assigned_to = driver_user
        task.assigned_to_name = driver_employee.full_name
        payload = dict(task.payload or {})
        payload["status"] = MoveTask.STATUS_CREATED
        payload["assigned_to_id"] = driver_employee.id
        payload["assigned_to_name"] = driver_employee.full_name
        task.payload = payload
        task.save(update_fields=["status", "assigned_to", "assigned_to_name", "payload", "updated_at"])

        complete_result = complete_move_task(
            legacy_order_id=move_id,
            user=driver_user,
            employee_id=driver_employee.id,
            employee_name=driver_employee.full_name,
        )

        self.assertTrue(complete_result.ok, complete_result.error)
        task.refresh_from_db()
        self.assertEqual(task.status, MoveTask.STATUS_DONE)
        self.assertEqual((task.payload or {}).get("status"), MoveTask.STATUS_DONE)

    def test_complete_receiving_move_finishes_warehouse_putaway(self):
        driver_user = get_user_model().objects.create_user(username="reachtruck_driver_putaway", password="pwd")
        driver_employee = Employee.objects.create(
            full_name="Водитель Приемки",
            role="reachtruck_driver",
            user=driver_user,
            is_active=True,
        )
        placement_entry = OrderAuditEntry.objects.create(
            order_id="R-WH-1",
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {
                        "code": "BOX-WH-1",
                        "sealed": True,
                        "items": [
                            {
                                "sku": "SKU-100",
                                "name": "Товар 100",
                                "size": "42",
                                "barcode": "200000000100",
                                "goods_type": "gv",
                                "qty": 12,
                            }
                        ],
                    }
                ],
                "act_pallets": [
                    {
                        "code": "PAL-WH-1",
                        "sealed": True,
                        "boxes": ["BOX-WH-1"],
                        "items": [],
                        "location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 4},
                    }
                ],
            },
        )
        OperationalStockService.replace_order_placement(
            self.agency,
            "receiving",
            "R-WH-1",
            placement_entry.payload,
        )
        WarehouseWritePathService.sync_receiving_placement(
            agency=self.agency,
            order_id="R-WH-1",
            placement_payload=placement_entry.payload,
            performed_by=self.user,
        )
        request = RequestFactory().post("/orders/receiving/R-WH-1/create-warehouse-moves/")
        request.user = self.user
        created, skipped_existing, skipped_missing, total = _create_receiving_warehouse_moves(
            "R-WH-1",
            [placement_entry],
            request,
        )
        self.assertEqual((created, skipped_existing, skipped_missing, total), (1, 0, 0, 1))

        task = MoveTask.objects.get(legacy_order_id__isnull=False, pallet_code="PAL-WH-1")
        task.status = MoveTask.STATUS_IN_PROGRESS
        task.assigned_to = driver_user
        task.assigned_to_name = driver_employee.full_name
        payload = dict(task.payload or {})
        payload["status"] = MoveTask.STATUS_IN_PROGRESS
        payload["assigned_to_id"] = driver_employee.id
        payload["assigned_to_name"] = driver_employee.full_name
        task.payload = payload
        task.save(update_fields=["status", "assigned_to", "assigned_to_name", "payload", "updated_at"])

        complete_result = complete_move_task(
            legacy_order_id=task.legacy_order_id,
            user=driver_user,
            employee_id=driver_employee.id,
            employee_name=driver_employee.full_name,
        )

        self.assertTrue(complete_result.ok, complete_result.error)
        operation = WarehouseOperation.objects.get(id=payload["warehouse_operation_id"])
        snapshot = WarehouseStockSnapshot.objects.get(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id="R-WH-1",
        )
        self.assertEqual(operation.status, WarehouseOperation.STATUS_DONE)
        self.assertEqual(snapshot.warehouse_state_code, "stored")
        self.assertEqual(snapshot.zone_code, "OS")
        self.assertEqual(snapshot.location.zone_code, "OS")
        self.assertEqual(snapshot.location.row_no, 1)
        self.assertEqual(snapshot.location.section_no, 1)
        self.assertEqual(snapshot.location.tier_no, 1)
        self.assertEqual(snapshot.location.cell_no, 4)


class ReachtruckSupplementPackingStageScheduleTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="reachtruck_supplement_stage",
            password="pwd",
        )

    def test_otg_completion_schedules_stage_only_after_transaction_commit(self):
        payload = {
            "shipping_order_id": "OTG-000999",
            "shipping_order_pk": 999,
        }
        with mock.patch(
            "shipping.discrepancy.mark_supplement_awaiting_packing",
        ) as mark_stage:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                scheduled = _schedule_shipping_supplement_packing_stage(
                    payload=payload,
                    to_zone="OTG",
                    completed_move_id="MOVE-999",
                    user=self.user,
                )
                mark_stage.assert_not_called()

        self.assertTrue(scheduled)
        self.assertEqual(len(callbacks), 1)
        mark_stage.assert_called_once()
        call_kwargs = mark_stage.call_args.kwargs
        self.assertEqual(call_kwargs["completed_move_id"], "MOVE-999")
        self.assertEqual(call_kwargs["order_pk"], 999)
        self.assertEqual(call_kwargs["order_number"], "OTG-000999")
        self.assertEqual(call_kwargs["user"].pk, self.user.pk)

    def test_non_otg_or_unidentified_move_does_not_schedule_stage(self):
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            obr_scheduled = _schedule_shipping_supplement_packing_stage(
                payload={"shipping_order_id": "OTG-000999", "shipping_order_pk": 999},
                to_zone="OBR",
                completed_move_id="MOVE-999",
                user=self.user,
            )
            unidentified_scheduled = _schedule_shipping_supplement_packing_stage(
                payload={},
                to_zone="OTG",
                completed_move_id="MOVE-999",
                user=self.user,
            )

        self.assertFalse(obr_scheduled)
        self.assertFalse(unidentified_scheduled)
        self.assertEqual(callbacks, [])


class ReachtruckPalletBoxPlanTests(TestCase):
    def test_item_match_resolves_missing_barcode_through_same_client_sku(self):
        first_agency = Agency.objects.create(agn_name="Клиент SKU ШК 1")
        second_agency = Agency.objects.create(agn_name="Клиент SKU ШК 2")
        first_sku = SKU.objects.create(
            agency=first_agency,
            sku_code="SKU-SAME",
            name="Товар первого клиента",
        )
        second_sku = SKU.objects.create(
            agency=second_agency,
            sku_code="SKU-SAME",
            name="Товар второго клиента",
        )
        SKUBarcode.objects.create(
            sku=first_sku,
            value="BAR-FIRST",
        )
        SKUBarcode.objects.create(
            sku=second_sku,
            value="BAR-SECOND",
        )
        item = {
            "sku": "SKU-SAME",
            "barcode": "",
            "goods_type": "gv",
            "qty": 10,
        }

        self.assertTrue(
            _item_matches(
                item,
                {"BAR-FIRST"},
                {"SKU-SAME"},
                {"gv"},
                agency_id=first_agency.id,
            )
        )
        self.assertFalse(
            _item_matches(
                item,
                {"BAR-SECOND"},
                {"SKU-SAME"},
                {"gv"},
                agency_id=first_agency.id,
            )
        )
        explicit_wrong_barcode = dict(item, barcode="BAR-WRONG")
        self.assertFalse(
            _item_matches(
                explicit_wrong_barcode,
                {"BAR-FIRST"},
                {"SKU-SAME"},
                {"gv"},
                agency_id=first_agency.id,
            )
        )
        with mock.patch(
            "reachtruck.services.pallet_ops._all_stock_boxes_for_pallet",
            return_value=[
                {
                    "code": "BOX-FIRST",
                    "items": [item],
                    "qty": 10,
                    "barcode_qty": {},
                }
            ],
        ):
            boxes = _matching_stock_boxes_for_pallet(
                "PAL-FIRST",
                {"BAR-FIRST"},
                {"SKU-SAME"},
                {"gv"},
                agency_id=first_agency.id,
            )

        self.assertEqual(
            boxes,
            [
                {
                    "code": "BOX-FIRST",
                    "qty": 10,
                    "barcode_qty": {"BAR-FIRST": 10},
                }
            ],
        )

    def test_stock_box_lookup_isolated_by_client(self):
        first_agency = Agency.objects.create(agn_name="Клиент строгого добора 1")
        second_agency = Agency.objects.create(agn_name="Клиент строгого добора 2")
        create_warehouse_snapshot_row(
            agency=first_agency,
            sku="SKU-SAME",
            barcode="BAR-FIRST",
            qty=10,
            box_code="BOX-FIRST",
            pallet_code="PAL-SAME",
        )
        create_warehouse_snapshot_row(
            agency=second_agency,
            sku="SKU-SAME",
            barcode="BAR-SECOND",
            qty=10,
            box_code="BOX-SECOND",
            pallet_code="PAL-SAME",
        )

        first_boxes = _all_stock_boxes_for_pallet(
            "PAL-SAME",
            agency_id=first_agency.id,
        )
        second_boxes = _all_stock_boxes_for_pallet(
            "PAL-SAME",
            agency_id=second_agency.id,
        )

        self.assertEqual([row["code"] for row in first_boxes], ["BOX-FIRST"])
        self.assertEqual([row["code"] for row in second_boxes], ["BOX-SECOND"])

    def test_pallet_box_plan_marks_all_boxes_deliver_for_full_pallet_move(self):
        agency = Agency.objects.create(agn_name="Клиент полной паллеты")
        create_warehouse_snapshot_row(
            agency=agency,
            order_type="receiving",
            order_id="10",
            sku="SKU-1",
            barcode="200000000901",
            goods_type="gv",
            qty=50,
            box_code="BOX-A",
            pallet_code="PAL-FULL",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=4,
            location="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 4",
        )
        payload = {
            "move_mode": MOVE_MODE_PALLET_FULL,
            "pick_mode": "full",
            "to_location": {"zone": "OBR"},
        }

        rows = _pallet_box_plan(payload, "PAL-FULL", agency_id=agency.id)

        self.assertEqual([row["box_code"] for row in rows], ["BOX-A"])
        self.assertEqual([row["action"] for row in rows], ["Доставить"])

    def test_pallet_box_plan_marks_deliver_then_return(self):
        agency = Agency.objects.create(agn_name="Клиент коробов паллеты")
        create_warehouse_snapshot_row(
            agency=agency,
            order_type="receiving",
            order_id="1",
            sku="SKU-1",
            barcode="200000000001",
            goods_type="gv",
            qty=10,
            box_code="BOX-A",
            pallet_code="PAL-1",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=1,
            location="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 1",
        )
        create_warehouse_snapshot_row(
            agency=agency,
            order_type="receiving",
            order_id="1",
            sku="SKU-2",
            barcode="200000000002",
            goods_type="gv",
            qty=5,
            box_code="BOX-B",
            pallet_code="PAL-1",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=1,
            location="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 1",
        )
        payload = {
            "move_mode": MOVE_MODE_BOX_PARTIAL,
            "to_location": {"zone": "OTG"},
            "requested_qty": 10,
            "requested_barcode_qty": {"200000000001": 10},
        }

        rows = _pallet_box_plan(payload, "PAL-1", agency_id=agency.id)

        self.assertEqual([row["box_code"] for row in rows], ["BOX-A", "BOX-B"])
        self.assertEqual([row["action"] for row in rows], ["Доставить", "Вернуть"])

    def test_pallet_box_plan_marks_duplicate_barcode_box_as_return(self):
        agency = Agency.objects.create(agn_name="Клиент дублей коробов")
        for box_code in ["BOX-A", "BOX-B"]:
            create_warehouse_snapshot_row(
                agency=agency,
                order_type="receiving",
                order_id="2",
                sku="SKU-1",
                barcode="200000000111",
                goods_type="gv",
                qty=50,
                box_code=box_code,
                pallet_code="PAL-2",
                zone="OS",
                row=1,
                section=1,
                tier=1,
                cell=2,
                location="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 2",
            )
        payload = {
            "move_mode": MOVE_MODE_BOX_PARTIAL,
            "to_location": {"zone": "OTG"},
            "requested_qty": 50,
            "requested_barcode_qty": {"200000000111": 50},
        }

        rows = _pallet_box_plan(payload, "PAL-2", agency_id=agency.id)

        self.assertEqual([row["box_code"] for row in rows], ["BOX-A", "BOX-B"])
        self.assertEqual([row["action"] for row in rows], ["Доставить", "Вернуть"])


class ReachtruckCollectMovesTests(TestCase):
    def _create_task(
        self,
        *,
        request: MoveRequest,
        legacy_order_id: str,
        pallet_code: str,
        move_mode: str,
        to_zone: str,
        payload: dict,
    ) -> MoveTask:
        return MoveTask.objects.create(
            request=request,
            legacy_order_id=legacy_order_id,
            pallet_code=pallet_code,
            from_zone="OS",
            to_zone=to_zone,
            move_mode=move_mode,
            status=MoveTask.STATUS_CREATED,
            payload={
                "status": MoveTask.STATUS_CREATED,
                "status_label": "Ожидает перевозки",
                "pallet_code": pallet_code,
                "from_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
                "to_location": {"zone": to_zone},
                "move_mode": move_mode,
                **payload,
            },
        )

    def test_collect_moves_uses_operational_stock_for_box_plans(self):
        agency = Agency.objects.create(agn_name="Клиент lookup")
        create_warehouse_snapshot_row(
            agency=agency,
            order_type="receiving",
            order_id="R-LOOKUP",
            sku="SKU-1",
            barcode="200000000333",
            goods_type="gv",
            qty=10,
            box_code="BOX-LOOKUP",
            pallet_code="PAL-LOOKUP",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=5,
            location="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 5",
        )
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id="LOOKUP",
            agency=agency,
            destination_zone="OBR",
            status=MoveRequest.STATUS_IN_PROGRESS,
        )
        self._create_task(
            request=move_request,
            legacy_order_id="MOVE-LOOKUP",
            pallet_code="PAL-LOOKUP",
            move_mode=MOVE_MODE_PALLET_FULL,
            to_zone="OBR",
            payload={"pick_mode": "full"},
        )
        OrderAuditEntry.objects.create(
            order_id="MOVE-LOOKUP",
            order_type="stock_move",
            action="status",
            agency=agency,
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "pallet_code": "PAL-LOOKUP",
                "from_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 5},
                "to_location": {"zone": "OBR"},
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
            },
        )

        with mock.patch("reachtruck.views._latest_closed_placement_entries", wraps=_latest_closed_placement_entries) as latest_mock:
            active, done = _collect_moves(employee_id=None, driver_view=False)

        self.assertEqual(latest_mock.call_count, 0)
        self.assertEqual(len(done), 0)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["pallet_box_plan"][0]["box_code"], "BOX-LOOKUP")
        self.assertEqual(active[0]["box_execution_plan"][0]["box_code"], "BOX-LOOKUP")

    def test_collect_moves_reuses_stock_lookup_for_tasks_on_same_pallet(self):
        agency = Agency.objects.create(agn_name="Клиент кеша паллеты")
        create_warehouse_snapshot_row(
            agency=agency,
            order_type="shipping",
            order_id="S-CACHE",
            sku="SKU-CACHE",
            barcode="200000000334",
            goods_type="gv",
            qty=10,
            box_code="BOX-CACHE",
            pallet_code="PAL-CACHE",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=1,
        )
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id="CACHE",
            agency=agency,
            destination_zone="OBR",
            status=MoveRequest.STATUS_IN_PROGRESS,
        )
        for index in range(3):
            self._create_task(
                request=move_request,
                legacy_order_id=f"MOVE-CACHE-{index}",
                pallet_code="PAL-CACHE",
                move_mode=MOVE_MODE_PALLET_FULL,
                to_zone="OBR",
                payload={"pick_mode": "full"},
            )

        with mock.patch.object(
            OperationalStockService,
            "get_pallet_boxes",
            wraps=OperationalStockService.get_pallet_boxes,
        ) as stock_lookup:
            active, done = _collect_moves(
                employee_id=None,
                driver_view=False,
                include_done=False,
                include_plans=True,
            )

        self.assertEqual(len(active), 3)
        self.assertEqual(len(done), 0)
        self.assertEqual(stock_lookup.call_count, 1)

    def test_collect_moves_does_not_persist_stale_partial_normalization(self):
        agency = Agency.objects.create(agn_name="Клиент read-only GET")
        create_warehouse_snapshot_row(
            agency=agency,
            order_type="processing",
            order_id="P-READ-ONLY",
            sku="SKU-READ-ONLY",
            barcode="200000000335",
            goods_type="gv",
            qty=10,
            box_code="BOX-READ-ONLY",
            pallet_code="PAL-READ-ONLY",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=1,
        )
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_PROCESSING,
            context_id="P-READ-ONLY",
            agency=agency,
            destination_zone="OBR",
            status=MoveRequest.STATUS_IN_PROGRESS,
        )
        task = self._create_task(
            request=move_request,
            legacy_order_id="MOVE-READ-ONLY",
            pallet_code="PAL-READ-ONLY",
            move_mode=MOVE_MODE_BOX_PARTIAL,
            to_zone="OBR",
            payload={
                "pick_mode": "partial",
                "requested_rows": [{"box_code": "BOX-READ-ONLY", "qty": 10}],
            },
        )
        original_payload = dict(task.payload)
        original_updated_at = task.updated_at

        active, done = _collect_moves(
            employee_id=None,
            driver_view=False,
            include_done=False,
            include_plans=True,
        )

        task.refresh_from_db()
        self.assertEqual(len(active), 1)
        self.assertEqual(len(done), 0)
        self.assertEqual(active[0]["move_mode"], MOVE_MODE_BOX_FULL)
        self.assertEqual(task.move_mode, MOVE_MODE_BOX_PARTIAL)
        self.assertEqual(task.payload, original_payload)
        self.assertEqual(task.updated_at, original_updated_at)

    def test_collect_moves_prefilters_otg_completion_checks_in_bulk(self):
        agency = Agency.objects.create(agn_name="Клиент пакетной OTG проверки")
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id="OTG-BULK",
            agency=agency,
            destination_zone="OTG",
            status=MoveRequest.STATUS_IN_PROGRESS,
        )
        for index in range(3):
            self._create_task(
                request=move_request,
                legacy_order_id=f"MOVE-OTG-BULK-{index}",
                pallet_code=f"PAL-OTG-BULK-{index}",
                move_mode=MOVE_MODE_BOX_FULL,
                to_zone="OTG",
                payload={
                    "pick_mode": "box_full",
                    "requested_boxes": [f"BOX-OTG-BULK-{index}"],
                },
            )

        with mock.patch(
            "reachtruck.services.ui_flows.otg_already_delivered_box_codes"
        ) as exact_otg_check:
            active, done = _collect_moves(
                employee_id=None,
                driver_view=False,
                include_done=False,
                include_plans=False,
            )

        self.assertEqual(len(active), 3)
        self.assertEqual(len(done), 0)
        exact_otg_check.assert_not_called()

    def test_collect_moves_includes_move_tasks_without_stock_move_audit_entries(self):
        user_model = get_user_model()
        driver_user = user_model.objects.create_user(username="reachtruck_collect_driver", password="pwd")
        driver_employee = Employee.objects.create(
            full_name="Водитель выборки",
            role="reachtruck_driver",
            user=driver_user,
            is_active=True,
        )
        agency = Agency.objects.create(agn_name="Клиент без аудита")
        create_warehouse_snapshot_row(
            agency=agency,
            order_type="receiving",
            order_id="91",
            sku="SKU-1",
            barcode="200000000991",
            goods_type="gv",
            qty=10,
            box_code="BOX-NO-AUDIT",
            pallet_code="PAL-NO-AUDIT",
            zone="PR",
            row=1,
            section=1,
            tier=1,
            cell=1,
            location="PR · Зона приемки",
        )
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_RECEIVING,
            context_id="91",
            agency=agency,
            destination_zone="OS",
            destination_row=1,
            destination_section=1,
            destination_tier=1,
            destination_cell=1,
            status=MoveRequest.STATUS_IN_PROGRESS,
        )
        MoveTask.objects.create(
            request=move_request,
            legacy_order_id="MOVE-NO-AUDIT",
            pallet_code="PAL-NO-AUDIT",
            from_zone="PR",
            to_zone="OS",
            to_row=1,
            to_section=1,
            to_tier=1,
            to_cell=1,
            move_mode=MOVE_MODE_PALLET_FULL,
            status=MoveTask.STATUS_IN_PROGRESS,
            assigned_to=driver_user,
            assigned_to_name=driver_employee.full_name,
            payload={
                "status": "in_progress",
                "status_label": "В работе",
                "receiving_order_id": "91",
                "pallet_code": "PAL-NO-AUDIT",
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
                "from_label": "PR · Зона приемки",
                "to_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 1",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
                "assigned_to_id": driver_employee.id,
                "assigned_to_name": driver_employee.full_name,
                "mobile_request_batch_mode": True,
            },
        )

        active, done = _collect_moves(employee_id=driver_employee.id, driver_view=True)

        self.assertEqual(len(done), 0)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["order_id"], "MOVE-NO-AUDIT")
        self.assertEqual(active[0]["assigned_to_id"], driver_employee.id)
        self.assertEqual(active[0]["mobile_request_key"], "receiving:91")
        self.assertEqual(active[0]["mobile_category"], "movement")


class ReachtruckMobileFlowTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.driver_user = user_model.objects.create_user(username="reachtruck_mobile_driver", password="pwd")
        self.driver_employee = Employee.objects.create(
            full_name="Водитель Мобильный",
            role="reachtruck_driver",
            user=self.driver_user,
            is_active=True,
        )
        self.manager_user = user_model.objects.create_user(username="reachtruck_mobile_manager", password="pwd")
        self.storekeeper_user = user_model.objects.create_user(username="reachtruck_mobile_storekeeper", password="pwd")
        self.storekeeper_employee = Employee.objects.create(
            full_name="Кладовщиков Мобильный",
            role="storekeeper",
            user=self.storekeeper_user,
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="ИП Талеев ПП")
        self.client.force_login(self.driver_user)

    def test_mobile_dashboard_includes_scan_audio_assets(self):
        response = self.client.get("/reachtruck/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '/static/reachtruck/audio/scan-success.mp3')
        self.assertContains(response, '/static/reachtruck/audio/scan-error.mp3')

    def _create_placement(self, *, pallet_code: str, boxes: list[tuple[str, str, int]], zone: str = "OS"):
        for code, barcode, qty in boxes:
            create_warehouse_snapshot_row(
                agency=self.agency,
                order_type="receiving",
                order_id=f"R-{pallet_code}",
                sku="SKU-1",
                barcode=barcode,
                goods_type="gv",
                qty=qty,
                box_code=code,
                pallet_code=pallet_code,
                zone=zone,
                row=1,
                section=1,
                tier=1,
                cell=1,
                location=(
                    "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 1"
                    if zone == "OS"
                    else "PR · Зона приемки"
                ),
            )

    def test_mobile_dashboard_shows_categories_and_filtered_task_buttons(self):
        self._create_placement(
            pallet_code="PAL-MOBILE-1",
            boxes=[("BOX-1", "200000000001", 10)],
        )
        move_id = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Мобильная отгрузка",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "shipping_order_id": "SO-000002",
                "shipping_order_pk": 12,
                "pallet_code": "PAL-MOBILE-1",
                "from_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
                "to_location": {"zone": "OTG"},
                "from_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 1",
                "to_label": "OTG · Зона отгрузки",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
            },
        )

        response = self.client.get("/reachtruck/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Выбор работы")
        self.assertContains(response, "Отгрузка")
        self.assertContains(response, "Перемещения")

        response = self.client.get("/reachtruck/?mobile_category=shipping")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2_OTG")
        self.assertNotContains(response, "12_OTG")
        self.assertContains(response, "ИП Талеев ПП")
        self.assertContains(response, "/reachtruck/?mobile_category=shipping&amp;mobile_request=shipping%3ASO-000002")
        self.assertNotContains(response, f"/reachtruck/?mobile_category=shipping&amp;mobile_task={move_id}")

        response = self.client.get("/reachtruck/?mobile_category=shipping&mobile_request=shipping:SO-000002")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Взять заявку в работу")
        self.assertNotContains(response, f"/reachtruck/?mobile_category=shipping&amp;mobile_request=shipping:SO-000002&amp;mobile_task={move_id}")

    def test_mobile_movement_uses_receiving_number_when_task_belongs_to_receiving(self):
        self._create_placement(
            pallet_code="PAL-MOBILE-MOV-1",
            boxes=[("BOX-MOV-1", "200000000021", 10)],
            zone="PR",
        )
        move_id_1 = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Перемещение по приемке",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "receiving_order_id": "7",
                "pallet_code": "PAL-MOBILE-MOV-1",
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
                "from_label": "PR · Зона приемки",
                "to_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 1",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
            },
        )
        self._create_placement(
            pallet_code="PAL-MOBILE-MOV-2",
            boxes=[("BOX-MOV-2", "200000000022", 10)],
            zone="PR",
        )
        move_id_2 = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Второе перемещение по приемке",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "receiving_order_id": "7",
                "pallet_code": "PAL-MOBILE-MOV-2",
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 2},
                "from_label": "PR · Зона приемки",
                "to_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 2",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
            },
        )

        response = self.client.get("/reachtruck/?mobile_category=movement")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "7_PR")
        self.assertNotContains(response, "7_MOV")
        self.assertContains(response, "/reachtruck/?mobile_category=movement&amp;mobile_request=receiving%3A7", count=1)
        self.assertNotContains(response, f"/reachtruck/?mobile_category=movement&amp;mobile_task={move_id_1}")
        self.assertNotContains(response, f"/reachtruck/?mobile_category=movement&amp;mobile_task={move_id_2}")

        response = self.client.get("/reachtruck/?mobile_category=movement&mobile_request=receiving:7")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "7_PR")
        self.assertContains(response, "Взять заявку в работу")
        self.assertNotContains(response, f"/reachtruck/?mobile_category=movement&amp;mobile_request=receiving:7&amp;mobile_task={move_id_1}")
        self.assertNotContains(response, f"/reachtruck/?mobile_category=movement&amp;mobile_request=receiving:7&amp;mobile_task={move_id_2}")

        response = self.client.get(f"/reachtruck/?mobile_category=movement&mobile_request=receiving:7&mobile_task={move_id_1}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Взять заявку в работу")

    def test_mobile_request_task_card_can_take_move_without_extra_preview_step(self):
        self._create_placement(
            pallet_code="PAL-MOBILE-DIRECT-1",
            boxes=[("BOX-MOBILE-DIRECT-1", "200000000023", 10)],
        )
        move_id = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Короб целиком без лишнего шага",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает отбора коробов",
                "receiving_order_id": "19",
                "pallet_code": "PAL-MOBILE-DIRECT-1",
                "from_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
                "to_location": {"zone": "OBR"},
                "from_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 1",
                "to_label": "OBR · Зона обработки",
                "move_mode": MOVE_MODE_BOX_FULL,
                "pick_mode": "box_full",
                "requested_boxes": ["BOX-MOBILE-DIRECT-1"],
            },
        )

        response = self.client.get("/reachtruck/?mobile_category=movement&mobile_request=receiving:19")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="action" value="take_move"', html=False)
        self.assertContains(response, f'name="mobile_task" value="{move_id}"', html=False)
        self.assertContains(response, "Взять в работу")
        self.assertNotContains(
            response,
            f'href="/reachtruck/?mobile_category=movement&amp;mobile_request=receiving:19&amp;mobile_task={move_id}"',
            html=False,
        )

        response = self.client.post(
            "/reachtruck/",
            data={
                "action": "take_move",
                "order_id": move_id,
                "mobile_task": move_id,
                "mobile_category": "movement",
                "mobile_request": "receiving:19",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Подтвердить скан")
        self.assertContains(response, "Подтверждение паллеты")
        self.assertContains(response, "Содержимое паллеты")
        self.assertEqual(MoveTask.objects.get(legacy_order_id=move_id).status, MoveTask.STATUS_IN_PROGRESS)

    def test_storekeeper_reachtruck_dashboard_uses_storekeeper_identity_and_source_link(self):
        self.client.force_login(self.storekeeper_user)
        self._create_placement(
            pallet_code="PAL-MOBILE-SK-1",
            boxes=[("BOX-MOV-SK-1", "200000000031", 10)],
            zone="PR",
        )
        move_id = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Перемещение по приемке для кладовщика",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "receiving_order_id": "17",
                "pallet_code": "PAL-MOBILE-SK-1",
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 3},
                "from_label": "PR · Зона приемки",
                "to_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 3",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
            },
        )

        response = self.client.get(
            f"/reachtruck/?mobile_category=movement&mobile_request=receiving:17&mobile_task={move_id}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Кабинет кладовщика")
        self.assertNotContains(response, "Кабинет водителя ричтрака")
        self.assertContains(response, "Задания на перемещение паллет")
        self.assertContains(response, 'href="/stockmap/visual/"', html=False)
        self.assertContains(response, "Изменить заявку ричтраку")
        self.assertNotContains(response, "Редактировать приемку")
        self.assertNotContains(response, "Открыть приемку")
        self.assertNotContains(response, "Взять в работу")
        self.assertContains(response, "Удалить задание")

    def test_storekeeper_reachtruck_dashboard_uses_request_context_for_receiving_link(self):
        self.client.force_login(self.storekeeper_user)
        self._create_placement(
            pallet_code="PAL-MOBILE-SK-CTX-1",
            boxes=[("BOX-MOV-SK-CTX-1", "200000000041", 10)],
            zone="PR",
        )
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_RECEIVING,
            context_id="27",
            agency=self.agency,
            requested_by=self.manager_user,
            requested_by_role="manager",
            requested_by_name="Менеджер",
            destination_zone="OS",
            status=MoveRequest.STATUS_PLANNED,
        )
        move_id = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Перемещение по приемке через context_id",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            move_request=move_request,
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "pallet_code": "PAL-MOBILE-SK-CTX-1",
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 5},
                "from_label": "PR · Зона приемки",
                "to_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 5",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
            },
        )

        response = self.client.get(
            f"/reachtruck/?mobile_category=movement&mobile_request=receiving:27&mobile_task={move_id}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Приемка №27")
        self.assertContains(response, "Изменить заявку ричтраку")
        self.assertNotContains(response, "Редактировать приемку")

    def test_storekeeper_reachtruck_dashboard_uses_stockmap_source_for_receiving_link(self):
        self.client.force_login(self.storekeeper_user)
        self._create_placement(
            pallet_code="PAL-MOBILE-SK-STOCKMAP-1",
            boxes=[("BOX-MOV-SK-STOCKMAP-1", "200000000042", 10)],
            zone="PR",
        )
        move_id = create_stock_move_task(
            user=self.storekeeper_user,
            agency=self.agency,
            description="Перемещение из карты склада",
            requested_by_name="Кладовщиков Мобильный",
            requested_by_role="storekeeper",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "pallet_code": "PAL-MOBILE-SK-STOCKMAP-1",
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 6},
                "from_label": "PR · Зона приемки",
                "to_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 6",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
                "stockmap_source_order_type": "receiving",
                "stockmap_source_order_id": "31",
            },
        )

        response = self.client.get(
            f"/reachtruck/?mobile_category=movement&mobile_request=receiving:31&mobile_task={move_id}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Приемка №31")
        self.assertContains(response, "Изменить заявку ричтраку")
        self.assertNotContains(response, "Редактировать приемку")

    def test_storekeeper_can_cancel_move_before_driver_takes_it(self):
        self.client.force_login(self.storekeeper_user)
        self._create_placement(
            pallet_code="PAL-MOBILE-SK-2",
            boxes=[("BOX-MOV-SK-2", "200000000032", 10)],
            zone="PR",
        )
        move_id = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Перемещение по приемке для отмены",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "receiving_order_id": "18",
                "pallet_code": "PAL-MOBILE-SK-2",
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 4},
                "from_label": "PR · Зона приемки",
                "to_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 4",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
            },
        )

        response = self.client.post(
            "/reachtruck/",
            data={
                "action": "cancel_move",
                "order_id": move_id,
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        task = MoveTask.objects.get(legacy_order_id=move_id)
        self.assertEqual(task.status, MoveTask.STATUS_CANCELED)
        self.assertEqual((task.payload or {}).get("status_label"), "Отменено до начала выполнения")
        self.assertNotContains(response, "Ожидает перевозки")
        self.assertNotContains(response, "Удалить задание")

    def test_storekeeper_can_edit_move_destination_before_driver_takes_it(self):
        self.client.force_login(self.storekeeper_user)
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_RECEIVING,
            context_id="55",
            agency=self.agency,
            requested_by=self.manager_user,
            requested_by_role="manager",
            requested_by_name="Менеджер",
            destination_zone="OS",
            destination_row=1,
            destination_section=1,
            destination_tier=1,
            destination_cell=1,
            status=MoveRequest.STATUS_PLANNED,
        )
        operation = WarehouseOperation.objects.create(
            agency=self.agency,
            operation_type=WarehouseOperation.TYPE_PUTAWAY,
            context_type="receiving",
            context_id="55",
            destination_zone_code="OS",
            destination_location=WarehouseLocation.objects.create(
                warehouse_code="MSK",
                zone_code="OS",
                zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
                row_no=1,
                section_no=1,
                tier_no=1,
                cell_no=1,
                location_code="OS-1-1-1-1",
                display_name="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 1",
            ),
            status=WarehouseOperation.STATUS_CREATED,
        )
        warehouse_task = WarehouseOperationTask.objects.create(
            operation=operation,
            task_type=WarehouseOperationTask.TYPE_PALLET_MOVE,
            to_location=operation.destination_location,
            to_zone_code="OS",
            status=WarehouseOperationTask.STATUS_CREATED,
        )
        move_id = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Перемещение по приемке для изменения",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            move_request=move_request,
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "receiving_order_id": "55",
                "pallet_code": "PAL-MOBILE-SK-EDIT-1",
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
                "from_label": "PR · Зона приемки",
                "to_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 1",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
                "warehouse_operation_id": operation.id,
                "warehouse_operation_task_id": warehouse_task.id,
            },
        )

        response = self.client.post(
            "/reachtruck/",
            data={
                "action": "edit_move_destination",
                "order_id": move_id,
                "to_zone": "OS",
                "to_row": "2",
                "to_section": "3",
                "to_tier": "1",
                "to_cell": "4",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        task = MoveTask.objects.get(legacy_order_id=move_id)
        self.assertEqual(task.status, MoveTask.STATUS_CREATED)
        self.assertEqual(task.to_zone, "OS")
        self.assertEqual(task.to_row, 2)
        self.assertEqual(task.to_section, 3)
        self.assertEqual(task.to_tier, 1)
        self.assertEqual(task.to_cell, 4)
        self.assertEqual((task.payload or {}).get("to_label"), "OS · Линия B · Стеллаж 2 · Этаж 1 · Ячейка 4")
        move_request.refresh_from_db()
        self.assertEqual(move_request.destination_row, 2)
        self.assertEqual(move_request.destination_section, 3)
        warehouse_task.refresh_from_db()
        self.assertEqual(warehouse_task.to_zone_code, "OS")
        self.assertEqual(warehouse_task.to_location.row_no, 2)
        self.assertEqual(warehouse_task.to_location.section_no, 3)

    def test_storekeeper_cannot_edit_move_destination_to_reserved_os_cell(self):
        self.client.force_login(self.storekeeper_user)
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_RECEIVING,
            context_id="56",
            agency=self.agency,
            requested_by=self.manager_user,
            requested_by_role="manager",
            requested_by_name="Менеджер",
            destination_zone="OS",
            destination_row=1,
            destination_section=1,
            destination_tier=1,
            destination_cell=1,
            status=MoveRequest.STATUS_PLANNED,
        )
        operation = WarehouseOperation.objects.create(
            agency=self.agency,
            operation_type=WarehouseOperation.TYPE_PUTAWAY,
            context_type="receiving",
            context_id="56",
            destination_zone_code="OS",
            destination_location=WarehouseLocation.objects.create(
                warehouse_code="MSK",
                zone_code="OS",
                zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
                row_no=1,
                section_no=1,
                tier_no=1,
                cell_no=1,
                location_code="OS-1-1-1-1",
                display_name="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 1",
            ),
            status=WarehouseOperation.STATUS_CREATED,
        )
        warehouse_task = WarehouseOperationTask.objects.create(
            operation=operation,
            task_type=WarehouseOperationTask.TYPE_PALLET_MOVE,
            to_location=operation.destination_location,
            to_zone_code="OS",
            status=WarehouseOperationTask.STATUS_CREATED,
        )
        conflicting_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=2,
            section_no=3,
            tier_no=1,
            cell_no=4,
            location_code="OS-2-3-1-4",
            display_name="OS · Ряд 2 · Секция 3 · Ярус 1 · Ячейка 4",
        )
        WarehouseOperation.objects.create(
            agency=self.agency,
            operation_type=WarehouseOperation.TYPE_PUTAWAY,
            context_type="receiving",
            context_id="57",
            destination_zone_code="OS",
            destination_location=conflicting_location,
            status=WarehouseOperation.STATUS_PLANNED,
        )
        move_id = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Перемещение по приемке с конфликтом ячейки",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            move_request=move_request,
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "receiving_order_id": "56",
                "pallet_code": "PAL-MOBILE-SK-EDIT-2",
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
                "from_label": "PR · Зона приемки",
                "to_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 1",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
                "warehouse_operation_id": operation.id,
                "warehouse_operation_task_id": warehouse_task.id,
            },
        )

        response = self.client.post(
            "/reachtruck/",
            data={
                "action": "edit_move_destination",
                "order_id": move_id,
                "to_zone": "OS",
                "to_row": "2",
                "to_section": "3",
                "to_tier": "1",
                "to_cell": "4",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertContains(
            response,
            "уже зарезервировано другой заявкой ричтрака",
            status_code=400,
        )
        task = MoveTask.objects.get(legacy_order_id=move_id)
        self.assertEqual(task.to_row, 1)
        self.assertEqual(task.to_section, 1)
        warehouse_task.refresh_from_db()
        self.assertEqual(warehouse_task.to_location.row_no, 1)
        self.assertEqual(warehouse_task.to_location.section_no, 1)


    def test_mobile_scan_flow_completes_full_pallet_task(self):
        self._create_placement(
            pallet_code="PAL-MOBILE-2",
            boxes=[("BOX-A", "200000000010", 10), ("BOX-B", "200000000011", 8)],
        )
        move_id = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Полная паллета в OTG",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "shipping_order_id": "7_OTG",
                "pallet_code": "PAL-MOBILE-2",
                "from_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
                "to_location": {"zone": "OTG"},
                "from_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 1",
                "to_label": "OTG · Зона отгрузки",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
            },
        )
        take_result = take_move_task(
            legacy_order_id=move_id,
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)

        snapshot = build_mobile_execution_snapshot(move_id)
        self.assertEqual(snapshot["current_step"], "source")
        self.assertEqual(snapshot["source_code"], "0-1/1-1")

        result = scan_move_task_step(
            legacy_order_id=move_id,
            scan_value="0-1/1-1",
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(result.ok, result.error)
        result = scan_move_task_step(
            legacy_order_id=move_id,
            scan_value="PAL-MOBILE-2",
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(result.ok, result.error)
        result = scan_move_task_step(
            legacy_order_id=move_id,
            scan_value="BOX-A",
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(result.ok, result.error)
        result = scan_move_task_step(
            legacy_order_id=move_id,
            scan_value="BOX-B",
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(result.ok, result.error)

        snapshot = build_mobile_execution_snapshot(move_id)
        self.assertTrue(snapshot["all_boxes_complete"])
        self.assertEqual(snapshot["current_step"], "destination")

        result = scan_move_task_step(
            legacy_order_id=move_id,
            scan_value="OTG",
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.completed)
        task = MoveTask.objects.get(legacy_order_id=move_id)
        self.assertEqual(task.status, MoveTask.STATUS_DONE)

    def test_mobile_request_flow_completes_all_full_pallets_in_request(self):
        self._create_placement(
            pallet_code="PAL-REQ-1",
            boxes=[("BOX-REQ-1", "200000000201", 10)],
            zone="PR",
        )
        self._create_placement(
            pallet_code="PAL-REQ-2",
            boxes=[("BOX-REQ-2", "200000000202", 10)],
            zone="PR",
        )
        move_id_1 = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Первая паллета заявки",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "receiving_order_id": "81",
                "pallet_code": "PAL-REQ-1",
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
                "from_label": "PR · Зона приемки",
                "to_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 1",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
            },
        )
        move_id_2 = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Вторая паллета заявки",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "receiving_order_id": "81",
                "pallet_code": "PAL-REQ-2",
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 2},
                "from_label": "PR · Зона приемки",
                "to_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 2",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
            },
        )
        request_ids = [move_id_1, move_id_2]

        take_result = take_move_request(
            legacy_order_ids=request_ids,
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)

        snapshot = build_mobile_request_execution_snapshot(request_ids, employee_id=self.driver_employee.id)
        self.assertTrue(snapshot["can_scan"])
        self.assertEqual(snapshot["current_step"], "pallet")
        self.assertEqual(snapshot["remaining_count"], 2)

        result = scan_move_request_step(
            legacy_order_ids=request_ids,
            scan_value="PAL-REQ-1",
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(result.ok, result.error)
        self.assertIn("Отвези -> 0-1/1-1.", result.message)
        snapshot = build_mobile_request_execution_snapshot(request_ids, employee_id=self.driver_employee.id)
        self.assertEqual(snapshot["current_step"], "destination")
        self.assertEqual(snapshot["active_order_id"], move_id_1)
        self.assertEqual(snapshot["active_destination_code"], "0-1/1-1")
        self.assertEqual(snapshot["prompt"], "Отвези -> 0-1/1-1")

        result = scan_move_request_step(
            legacy_order_ids=request_ids,
            scan_value="0-1/1-1",
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(result.ok, result.error)
        self.assertFalse(result.completed)
        snapshot = build_mobile_request_execution_snapshot(request_ids, employee_id=self.driver_employee.id)
        self.assertEqual(snapshot["remaining_count"], 1)
        self.assertEqual(snapshot["current_step"], "pallet")

        result = scan_move_request_step(
            legacy_order_ids=request_ids,
            scan_value="PAL-REQ-2",
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(result.ok, result.error)
        snapshot = build_mobile_request_execution_snapshot(request_ids, employee_id=self.driver_employee.id)
        self.assertEqual(snapshot["active_order_id"], move_id_2)

        result = scan_move_request_step(
            legacy_order_ids=request_ids,
            scan_value="OS-1-1-1-2",
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.completed)
        self.assertEqual(MoveTask.objects.get(legacy_order_id=move_id_1).status, MoveTask.STATUS_DONE)
        self.assertEqual(MoveTask.objects.get(legacy_order_id=move_id_2).status, MoveTask.STATUS_DONE)

    def test_mobile_request_flow_accepts_mojibake_for_cyrillic_pallet_code(self):
        self._create_placement(
            pallet_code="ТДТ-REQ-1-gv",
            boxes=[("BOX-REQ-TDT-1", "200000000291", 10)],
            zone="PR",
        )
        move_id = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Кириллическая паллета в заявке",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "receiving_order_id": "84",
                "pallet_code": "ТДТ-REQ-1-gv",
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 7},
                "from_label": "PR · Зона приемки",
                "to_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 7",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
            },
        )
        take_result = take_move_request(
            legacy_order_ids=[move_id],
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)

        garbled_scan = "ТДТ-REQ-1-gv".encode("utf-8").decode("cp1251")
        result = scan_move_request_step(
            legacy_order_ids=[move_id],
            scan_value=garbled_scan,
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )

        self.assertTrue(result.ok, result.error)
        self.assertIn("Паллета ТДТ-REQ-1-gv подтверждена.", result.message)
        self.assertNotIn(garbled_scan, result.message)
        self.assertIn("Отвези -> 0-1/1-7.", result.message)

    def test_mobile_request_flow_accepts_gbk_mojibake_for_cyrillic_pallet_code(self):
        self._create_placement(
            pallet_code="ТДТ-REQ-2-gv",
            boxes=[("BOX-REQ-TDT-2", "200000000292", 10)],
            zone="PR",
        )
        move_id = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="GBK-крякозябры паллеты в заявке",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "receiving_order_id": "85-GBK",
                "pallet_code": "ТДТ-REQ-2-gv",
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 8},
                "from_label": "PR · Зона приемки",
                "to_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 8",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
            },
        )
        take_result = take_move_request(
            legacy_order_ids=[move_id],
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)

        garbled_scan = "ТДТ-REQ-2-gv".encode("utf-8").decode("gb18030")
        result = scan_move_request_step(
            legacy_order_ids=[move_id],
            scan_value=garbled_scan,
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )

        self.assertTrue(result.ok, result.error)
        self.assertIn("Паллета ТДТ-REQ-2-gv подтверждена.", result.message)
        self.assertNotIn(garbled_scan, result.message)

    def test_mobile_request_flow_returns_detailed_error_for_unknown_pallet(self):
        self._create_placement(
            pallet_code="PAL-REQ-DET-1",
            boxes=[("BOX-REQ-DET-1", "200000000301", 10)],
            zone="PR",
        )
        self._create_placement(
            pallet_code="PAL-REQ-DET-2",
            boxes=[("BOX-REQ-DET-2", "200000000302", 10)],
            zone="PR",
        )
        move_id_1 = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Детальная ошибка по чужой паллете 1",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "receiving_order_id": "86",
                "pallet_code": "PAL-REQ-DET-1",
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
                "from_label": "PR · Зона приемки",
                "to_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 1",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
            },
        )
        move_id_2 = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Детальная ошибка по чужой паллете 2",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "receiving_order_id": "86",
                "pallet_code": "PAL-REQ-DET-2",
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 2},
                "from_label": "PR · Зона приемки",
                "to_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 2",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
            },
        )
        take_result = take_move_request(
            legacy_order_ids=[move_id_1, move_id_2],
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)

        result = scan_move_request_step(
            legacy_order_ids=[move_id_1, move_id_2],
            scan_value="PAL-NOT-IN-REQUEST",
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )

        self.assertFalse(result.ok)
        self.assertIn("PAL-NOT-IN-REQUEST", result.error)
        self.assertIn("PAL-REQ-DET-1", result.error)
        self.assertIn("PAL-REQ-DET-2", result.error)

    def test_mobile_request_flow_returns_detailed_error_for_wrong_destination(self):
        self._create_placement(
            pallet_code="PAL-REQ-WRONG-DEST-1",
            boxes=[("BOX-REQ-WRONG-DEST-1", "200000000311", 10)],
            zone="PR",
        )
        self._create_placement(
            pallet_code="PAL-REQ-WRONG-DEST-2",
            boxes=[("BOX-REQ-WRONG-DEST-2", "200000000312", 10)],
            zone="PR",
        )
        move_id_1 = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Ошибка по неверному месту 1",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "receiving_order_id": "87",
                "pallet_code": "PAL-REQ-WRONG-DEST-1",
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 3},
                "from_label": "PR · Зона приемки",
                "to_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 3",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
            },
        )
        move_id_2 = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Ошибка по неверному месту 2",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "receiving_order_id": "87",
                "pallet_code": "PAL-REQ-WRONG-DEST-2",
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 4},
                "from_label": "PR · Зона приемки",
                "to_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 4",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
            },
        )
        take_result = take_move_request(
            legacy_order_ids=[move_id_1, move_id_2],
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)

        first_step = scan_move_request_step(
            legacy_order_ids=[move_id_1, move_id_2],
            scan_value="PAL-REQ-WRONG-DEST-1",
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(first_step.ok, first_step.error)

        result = scan_move_request_step(
            legacy_order_ids=[move_id_1, move_id_2],
            scan_value="0-1/1-4",
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )

        self.assertFalse(result.ok)
        self.assertIn("PAL-REQ-WRONG-DEST-1", result.error)
        self.assertIn("0-1/1-3", result.error)
        self.assertIn("0-1/1-4", result.error)

    def test_mobile_request_flow_reports_delivered_pallet_explicitly(self):
        self._create_placement(
            pallet_code="PAL-REQ-DONE-1",
            boxes=[("BOX-REQ-DONE-1", "200000000321", 10)],
            zone="PR",
        )
        self._create_placement(
            pallet_code="PAL-REQ-DONE-2",
            boxes=[("BOX-REQ-DONE-2", "200000000322", 10)],
            zone="PR",
        )
        move_id_1 = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Паллета уже доставлена 1",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "receiving_order_id": "88",
                "pallet_code": "PAL-REQ-DONE-1",
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 5},
                "from_label": "PR · Зона приемки",
                "to_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 5",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
            },
        )
        move_id_2 = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Паллета уже доставлена 2",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "receiving_order_id": "88",
                "pallet_code": "PAL-REQ-DONE-2",
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 6},
                "from_label": "PR · Зона приемки",
                "to_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 6",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
            },
        )
        take_result = take_move_request(
            legacy_order_ids=[move_id_1, move_id_2],
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)

        self.assertTrue(
            scan_move_request_step(
                legacy_order_ids=[move_id_1, move_id_2],
                scan_value="PAL-REQ-DONE-1",
                user=self.driver_user,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            ).ok
        )
        self.assertTrue(
            scan_move_request_step(
                legacy_order_ids=[move_id_1, move_id_2],
                scan_value="0-1/1-5",
                user=self.driver_user,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            ).ok
        )

        result = scan_move_request_step(
            legacy_order_ids=[move_id_1, move_id_2],
            scan_value="PAL-REQ-DONE-1",
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )

        self.assertFalse(result.ok)
        self.assertIn("PAL-REQ-DONE-1", result.error)
        self.assertIn("0-1/1-5", result.error)
        self.assertIn("уже доставлена", result.error)

    def test_mobile_request_flow_duplicate_destination_scan_stays_positive(self):
        self._create_placement(
            pallet_code="PAL-REQ-DUP-1",
            boxes=[("BOX-REQ-DUP-1", "200000000331", 10)],
            zone="PR",
        )
        self._create_placement(
            pallet_code="PAL-REQ-DUP-2",
            boxes=[("BOX-REQ-DUP-2", "200000000332", 10)],
            zone="PR",
        )
        move_id_1 = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Дубль места 1",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "receiving_order_id": "89",
                "pallet_code": "PAL-REQ-DUP-1",
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 7},
                "from_label": "PR · Зона приемки",
                "to_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 7",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
            },
        )
        move_id_2 = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Дубль места 2",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "receiving_order_id": "89",
                "pallet_code": "PAL-REQ-DUP-2",
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 8},
                "from_label": "PR · Зона приемки",
                "to_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 8",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
            },
        )
        take_result = take_move_request(
            legacy_order_ids=[move_id_1, move_id_2],
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)

        self.assertTrue(
            scan_move_request_step(
                legacy_order_ids=[move_id_1, move_id_2],
                scan_value="PAL-REQ-DUP-1",
                user=self.driver_user,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            ).ok
        )
        first_destination = scan_move_request_step(
            legacy_order_ids=[move_id_1, move_id_2],
            scan_value="0-1/1-7",
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(first_destination.ok, first_destination.error)

        duplicate_destination = scan_move_request_step(
            legacy_order_ids=[move_id_1, move_id_2],
            scan_value="0-1/1-7",
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )

        self.assertTrue(duplicate_destination.ok, duplicate_destination.error)
        self.assertFalse(duplicate_destination.completed)
        self.assertIn("уже подтверждено", duplicate_destination.message)
        self.assertIn("следующую паллету", duplicate_destination.message)

    def test_mobile_home_shows_current_request_and_in_progress_count_after_take_request(self):
        self._create_placement(
            pallet_code="PAL-REQ-HOME-1",
            boxes=[("BOX-REQ-HOME-1", "200000000211", 10)],
            zone="PR",
        )
        self._create_placement(
            pallet_code="PAL-REQ-HOME-2",
            boxes=[("BOX-REQ-HOME-2", "200000000212", 10)],
            zone="PR",
        )
        move_id_1 = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Первая паллета заявки для возврата",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "receiving_order_id": "82",
                "pallet_code": "PAL-REQ-HOME-1",
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 3},
                "from_label": "PR · Зона приемки",
                "to_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 3",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
            },
        )
        move_id_2 = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Вторая паллета заявки для возврата",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "receiving_order_id": "82",
                "pallet_code": "PAL-REQ-HOME-2",
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 4},
                "from_label": "PR · Зона приемки",
                "to_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 4",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
            },
        )

        take_result = take_move_request(
            legacy_order_ids=[move_id_1, move_id_2],
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)

        response = self.client.get("/reachtruck/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Продолжить текущую работу")
        self.assertContains(response, "Приемка №82_PR")
        self.assertContains(response, "В работе: 2")
        self.assertContains(response, "/reachtruck/?mobile_category=movement&amp;mobile_request=receiving%3A82")

    def test_mobile_category_shows_continue_current_request_after_take_request(self):
        self._create_placement(
            pallet_code="PAL-REQ-CONT-1",
            boxes=[("BOX-REQ-CONT-1", "200000000221", 10)],
            zone="PR",
        )
        self._create_placement(
            pallet_code="PAL-REQ-CONT-2",
            boxes=[("BOX-REQ-CONT-2", "200000000222", 10)],
            zone="PR",
        )
        move_id_1 = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Первая паллета заявки для продолжения",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "receiving_order_id": "83",
                "pallet_code": "PAL-REQ-CONT-1",
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 5},
                "from_label": "PR · Зона приемки",
                "to_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 5",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
            },
        )
        move_id_2 = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Вторая паллета заявки для продолжения",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "receiving_order_id": "83",
                "pallet_code": "PAL-REQ-CONT-2",
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 6},
                "from_label": "PR · Зона приемки",
                "to_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 6",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
            },
        )

        take_result = take_move_request(
            legacy_order_ids=[move_id_1, move_id_2],
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)

        response = self.client.get("/reachtruck/?mobile_category=movement")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Приемка №83_PR")
        self.assertContains(response, "ИП Талеев ПП")
        self.assertContains(response, "Отсканируй паллету")
        self.assertContains(response, 'name="scan_value"')
        self.assertContains(response, 'autocomplete="off"')
        self.assertContains(response, 'class="mobile-pallet-chip">PAL-REQ-CONT-1')
        self.assertContains(response, 'class="mobile-pallet-chip">PAL-REQ-CONT-2')
        self.assertContains(response, "scanInput.addEventListener('blur'")
        self.assertContains(response, "document.addEventListener('visibilitychange'")
        self.assertNotContains(response, 'scanInput.readOnly = true;')
        self.assertNotContains(response, "Продолжить приемка №83_PR")

    def test_mobile_request_screen_highlights_active_destination_and_pallet(self):
        self._create_placement(
            pallet_code="PAL-REQ-HL-1",
            boxes=[("BOX-REQ-HL-1", "200000000241", 10)],
            zone="PR",
        )
        self._create_placement(
            pallet_code="PAL-REQ-HL-2",
            boxes=[("BOX-REQ-HL-2", "200000000242", 10)],
            zone="PR",
        )
        move_id_1 = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Подсветка активного адреса 1",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "receiving_order_id": "84",
                "pallet_code": "PAL-REQ-HL-1",
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 7},
                "from_label": "PR · Зона приемки",
                "to_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 7",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
            },
        )
        move_id_2 = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Подсветка активного адреса 2",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "receiving_order_id": "84",
                "pallet_code": "PAL-REQ-HL-2",
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 8},
                "from_label": "PR · Зона приемки",
                "to_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 8",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
            },
        )

        take_result = take_move_request(
            legacy_order_ids=[move_id_1, move_id_2],
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)

        first_scan = scan_move_request_step(
            legacy_order_ids=[move_id_1, move_id_2],
            scan_value="PAL-REQ-HL-1",
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(first_scan.ok, first_scan.error)

        response = self.client.get("/reachtruck/?mobile_category=movement&mobile_request=receiving:84")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Место назначения")
        self.assertContains(response, "scan-priority-code destination")
        self.assertContains(response, "0-1/1-7")
        self.assertContains(response, "scan-priority-code pallet")
        self.assertContains(response, "PAL-REQ-HL-1")
        self.assertContains(response, 'class="mobile-pallet-chip">PAL-REQ-HL-2')

    def test_mobile_category_show_list_flag_opens_request_list_instead_of_auto_open(self):
        self._create_placement(
            pallet_code="PAL-REQ-LIST-1",
            boxes=[("BOX-REQ-LIST-1", "200000000231", 10)],
            zone="PR",
        )
        self._create_placement(
            pallet_code="PAL-REQ-LIST-2",
            boxes=[("BOX-REQ-LIST-2", "200000000232", 10)],
            zone="PR",
        )
        move_id_1 = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Первая паллета заявки для списка",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "receiving_order_id": "85",
                "pallet_code": "PAL-REQ-LIST-1",
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 8},
                "from_label": "PR · Зона приемки",
                "to_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 8",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
            },
        )
        move_id_2 = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Вторая паллета заявки для списка",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает перевозки",
                "receiving_order_id": "85",
                "pallet_code": "PAL-REQ-LIST-2",
                "from_location": {"zone": "PR"},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 9},
                "from_label": "PR · Зона приемки",
                "to_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 9",
                "move_mode": MOVE_MODE_PALLET_FULL,
                "pick_mode": "full",
            },
        )

        take_result = take_move_request(
            legacy_order_ids=[move_id_1, move_id_2],
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)

        response = self.client.get("/reachtruck/?mobile_category=movement&mobile_show_list=1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Приемка №85_PR")
        self.assertContains(response, "/reachtruck/?mobile_category=movement&amp;mobile_request=receiving%3A85")
        self.assertNotContains(response, "Отсканируй паллету")

    def test_mobile_partial_pick_generates_box_plan_and_tracks_unit_scans(self):
        self._create_placement(
            pallet_code="PAL-MOBILE-3",
            boxes=[
                ("BOX-S", "200000000101", 6),
                ("BOX-M", "200000000101", 7),
                ("BOX-L", "200000000101", 10),
            ],
        )
        move_id = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Частичный подбор поштучно",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает отбора по потребности",
                "pallet_code": "PAL-MOBILE-3",
                "from_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
                "to_location": {"zone": "OBR"},
                "from_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 1",
                "to_label": "OBR · Зона обработки",
                "move_mode": MOVE_MODE_BOX_PARTIAL,
                "pick_mode": "partial",
                "requested_qty": 15,
                "requested_sku": "SKU-1",
                "requested_barcodes": ["200000000101"],
                "requested_goods_type": "",
            },
        )
        take_result = take_move_task(
            legacy_order_id=move_id,
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)

        scan_move_task_step(
            legacy_order_id=move_id,
            scan_value="OS-1-1-1-1",
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        task = MoveTask.objects.get(legacy_order_id=move_id)
        planned_rows = task.payload.get("requested_rows") or []
        self.assertEqual(
            [(row["box_code"], row["qty"]) for row in planned_rows],
            [("BOX-S", 6), ("BOX-M", 7), ("BOX-L", 2)],
        )

        scan_move_task_step(
            legacy_order_id=move_id,
            scan_value="PAL-MOBILE-3",
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        for box_code, unit_count in [("BOX-S", 6), ("BOX-M", 7), ("BOX-L", 2)]:
            result = scan_move_task_step(
                legacy_order_id=move_id,
                scan_value=box_code,
                user=self.driver_user,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            )
            self.assertTrue(result.ok, result.error)
            for _index in range(unit_count):
                result = scan_move_task_step(
                    legacy_order_id=move_id,
                    scan_value="200000000101",
                    user=self.driver_user,
                    employee_id=self.driver_employee.id,
                    employee_name=self.driver_employee.full_name,
                )
                self.assertTrue(result.ok, result.error)

        snapshot = build_mobile_execution_snapshot(move_id)
        self.assertTrue(snapshot["all_boxes_complete"])
        self.assertEqual(snapshot["scanned_units_total"], 15)

        result = scan_move_task_step(
            legacy_order_id=move_id,
            scan_value="OBR",
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.completed)

    def test_mobile_partial_pick_accepts_confirmed_manual_unit_quantity(self):
        self._create_placement(
            pallet_code="PAL-MOBILE-MANUAL-QTY",
            boxes=[("BOX-MANUAL-QTY", "200000000151", 10)],
        )
        move_id = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Частичный подбор с ручным количеством",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает отбора по потребности",
                "pallet_code": "PAL-MOBILE-MANUAL-QTY",
                "from_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
                "to_location": {"zone": "OBR"},
                "from_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 1",
                "to_label": "OBR · Зона обработки",
                "move_mode": MOVE_MODE_BOX_PARTIAL,
                "pick_mode": "partial",
                "requested_qty": 5,
                "requested_sku": "SKU-1",
                "requested_barcodes": ["200000000151"],
                "requested_goods_type": "",
            },
        )
        take_result = take_move_task(
            legacy_order_id=move_id,
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)
        for scan_value in ("OS-1-1-1-1", "PAL-MOBILE-MANUAL-QTY", "BOX-MANUAL-QTY"):
            result = scan_move_task_step(
                legacy_order_id=move_id,
                scan_value=scan_value,
                user=self.driver_user,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            )
            self.assertTrue(result.ok, result.error)

        snapshot = build_mobile_execution_snapshot(move_id)
        self.assertFalse(snapshot["unit_quantity_entry"]["available"])
        result = scan_move_task_step(
            legacy_order_id=move_id,
            scan_value="200000000151",
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(result.ok, result.error)
        snapshot = build_mobile_execution_snapshot(move_id)
        self.assertEqual(
            snapshot["unit_quantity_entry"],
            {
                "available": True,
                "box_code": "BOX-MANUAL-QTY",
                "current_qty": 1,
                "required_qty": 5,
            },
        )

        rejected = confirm_move_task_unit_quantity(
            legacy_order_id=move_id,
            unit_quantity=6,
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertFalse(rejected.ok)
        self.assertIn("больше потребности", rejected.error)
        self.assertEqual(build_mobile_execution_snapshot(move_id)["scanned_units_total"], 1)

        confirmed = confirm_move_task_unit_quantity(
            legacy_order_id=move_id,
            unit_quantity=5,
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(confirmed.ok, confirmed.error)
        snapshot = build_mobile_execution_snapshot(move_id)
        self.assertTrue(snapshot["all_boxes_complete"])
        self.assertEqual(snapshot["scanned_units_total"], 5)

        duplicate = confirm_move_task_unit_quantity(
            legacy_order_id=move_id,
            unit_quantity=5,
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertFalse(duplicate.ok)
        self.assertEqual(build_mobile_execution_snapshot(move_id)["scanned_units_total"], 5)

    def test_mobile_box_pick_accepts_box_scan_with_garbled_prefix_and_matching_tail(self):
        self._create_placement(
            pallet_code="PAL-MOBILE-GARBLED",
            boxes=[("ТДТ-0405-020070-op", "200000000131", 10)],
        )
        move_id = create_stock_move_task(
            user=self.manager_user,
            agency=self.agency,
            description="Отбор короба с битым префиксом скана",
            requested_by_name="Менеджер",
            requested_by_role="manager",
            payload={
                "status": "created",
                "status_label": "Ожидает отбора коробов",
                "pallet_code": "PAL-MOBILE-GARBLED",
                "from_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
                "to_location": {"zone": "OBR"},
                "from_label": "OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 1",
                "to_label": "OBR · Зона обработки",
                "move_mode": MOVE_MODE_BOX_FULL,
                "pick_mode": "box_full",
                "requested_boxes": ["ТДТ-0405-020070-op"],
            },
        )
        take_result = take_move_task(
            legacy_order_id=move_id,
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)

        self.assertTrue(
            scan_move_task_step(
                legacy_order_id=move_id,
                scan_value="OS-1-1-1-1",
                user=self.driver_user,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            ).ok
        )
        self.assertTrue(
            scan_move_task_step(
                legacy_order_id=move_id,
                scan_value="PAL-MOBILE-GARBLED",
                user=self.driver_user,
                employee_id=self.driver_employee.id,
                employee_name=self.driver_employee.full_name,
            ).ok
        )

        result = scan_move_task_step(
            legacy_order_id=move_id,
            scan_value="BAD�-0405-020070-op",
            user=self.driver_user,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )

        self.assertTrue(result.ok, result.error)
        self.assertIn("Короб ТДТ-0405-020070-op подтвержден.", result.message)
        task = MoveTask.objects.get(legacy_order_id=move_id)
        self.assertEqual((task.payload or {}).get("mobile_execution", {}).get("last_scan"), "ТДТ-0405-020070-op")
