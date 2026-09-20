import json
from urllib.parse import unquote

from django.contrib.auth import get_user_model
from django.test import RequestFactory, SimpleTestCase, TestCase

from employees.models import Employee
from reachtruck.models import MoveTask
from sku.models import Agency
from sklad.models import WarehouseContainer, WarehouseLocation, WarehouseOperation, WarehouseStockSnapshot
from sklad.services.putaway_draft_reservations import PutawayDraftReservationService
from sklad.services.stock_availability import StockAvailabilityService
from sklad.services.warehouse_transitions import WarehouseStateCode
from stockmap.services import (
    build_stock_map_context,
    build_stock_map_visual_context,
    parse_pr_destinations_json,
    submit_stock_map_pr_moves,
)
from stockmap.views import _os_row_badge_style, _os_row_cell_details


def _zone_kind(zone: str) -> str:
    return {
        "PR": WarehouseLocation.ZONE_KIND_RECEIVING,
        "OS": WarehouseLocation.ZONE_KIND_STORAGE,
        "OBR": WarehouseLocation.ZONE_KIND_PROCESSING,
        "OTG": WarehouseLocation.ZONE_KIND_SHIPPING,
    }.get(str(zone or "").strip().upper(), WarehouseLocation.ZONE_KIND_STORAGE)


def _warehouse_state(zone: str) -> str:
    return {
        "PR": WarehouseStateCode.PLACED_IN_RECEIVING.value,
        "OS": WarehouseStateCode.STORED.value,
        "OBR": WarehouseStateCode.IN_PROCESSING_ZONE.value,
        "OTG": WarehouseStateCode.IN_OTG.value,
    }.get(str(zone or "").strip().upper(), WarehouseStateCode.STORED.value)


def _location_label(zone: str, row: int = 0, section: int = 0, tier: int = 0, cell: int = 0, location: str = "") -> str:
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
    name: str = "Товар",
    size: str = "",
    barcode: str = "",
    goods_type: str = "Оптовый",
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
            "zone_kind": _zone_kind(zone_code),
            "location_code": "",
            "display_name": _location_label(zone_code, row, section, tier, cell, location),
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
        warehouse_state_code=_warehouse_state(zone_code),
    )


class StockMapRowBadgeStyleTests(SimpleTestCase):
    def test_empty_os_row_starts_neutral(self):
        style = _os_row_badge_style(occupied=0, total=120)

        self.assertEqual(style["fill_percent"], 0)
        self.assertEqual(style["badge_bg"], "rgb(226, 222, 216)")
        self.assertEqual(style["badge_border"], "rgb(197, 191, 184)")
        self.assertEqual(style["badge_ink"], "rgb(104, 99, 92)")

    def test_full_os_row_is_dark_red(self):
        style = _os_row_badge_style(occupied=120, total=120)

        self.assertEqual(style["fill_percent"], 100)
        self.assertEqual(style["badge_bg"], "rgb(120, 22, 22)")
        self.assertEqual(style["badge_border"], "rgb(92, 14, 14)")
        self.assertEqual(style["badge_ink"], "rgb(255, 241, 241)")


class StockMapRowCellDetailsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="stockmap_user", password="x")
        Employee.objects.create(
            full_name="Stockmap Storekeeper",
            user=self.user,
            role="storekeeper",
            is_active=True,
        )
        self.client.force_login(self.user)
        self.agency = Agency.objects.create(agn_name="Клиент карты склада", pref="КЗИ")
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="501",
            sku="SKU-1",
            name="Джинсы",
            size="42",
            barcode="2000000000001",
            goods_type="Оптовый",
            qty=60,
            processing_reserved_qty=10,
            shipping_reserved_qty=5,
            available_qty=45,
            box_code="BOX-1",
            pallet_code="ТДТ-1",
            zone="OS",
            row=1,
            section=2,
            tier=1,
            cell=3,
            location="OS · A-1/1-3",
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="501",
            sku="SKU-2",
            name="Джинсы",
            size="44",
            barcode="2000000000002",
            goods_type="Оптовый",
            qty=40,
            processing_reserved_qty=0,
            shipping_reserved_qty=0,
            available_qty=40,
            box_code="BOX-2",
            pallet_code="ТДТ-1",
            zone="OS",
            row=1,
            section=2,
            tier=1,
            cell=3,
            location="OS · A-1/1-3",
        )

    def test_os_row_view_exposes_modal_details_for_occupied_cell(self):
        response = self.client.get("/stockmap/os/1/")

        self.assertEqual(response.status_code, 200)
        details = response.context["cell_details"]
        self.assertIn("2:1:3", details)
        cell = details["2:1:3"]
        self.assertEqual(cell["cell_label"], "OS · A-1/1-3")
        self.assertEqual(cell["pallet_count"], 1)
        self.assertEqual(cell["summary"], "Клиент карты склада")
        self.assertEqual(cell["display_marker"], "ТДТ")
        pallet = cell["pallets"][0]
        self.assertEqual(pallet["pallet_code"], "ТДТ-1")
        self.assertEqual(pallet["display_marker"], "ТДТ")
        self.assertEqual(pallet["client_name"], "Клиент карты склада")
        self.assertEqual(pallet["box_count"], 2)
        self.assertEqual(pallet["total_qty"], 100)
        self.assertContains(response, 'data-detail-key="2:1:3"')
        page_text = response.content.decode("utf-8")
        self.assertIn("ТДТ", page_text)
        self.assertContains(response, "Клиент карты склада")

    def test_visual_stockmap_page_renders_os_and_support_zones(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="502",
            sku="SKU-PR",
            name="Куртка",
            size="48",
            barcode="2000000000999",
            goods_type="Оптовый",
            qty=20,
            available_qty=20,
            box_code="BOX-PR",
            pallet_code="PAL-PR",
            zone="PR",
            location="PR · Зона приемки",
        )

        response = self.client.get("/stockmap/visual/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "PR")
        self.assertContains(response, 'data-support-detail-key="PR"', html=False)
        self.assertContains(response, "stockmap-support-zone-details")
        page_text = response.content.decode("utf-8")
        self.assertIn("Клиент карты склада", page_text)
        self.assertIn("502_PR", page_text)
        self.assertContains(response, "Стеллаж 1")
        self.assertContains(response, ">КЗИ<", html=False)

    def test_visual_context_marks_draft_planned_and_placed_cells_with_client_prefix(self):
        draft_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=1,
            display_name="OS · 0-1/1-1",
        )
        planned_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=2,
            display_name="OS · 0-1/1-2",
        )
        WarehouseOperation.objects.create(
            agency=self.agency,
            operation_type=WarehouseOperation.TYPE_PUTAWAY,
            context_type=PutawayDraftReservationService.DRAFT_CONTEXT_TYPE,
            context_id="draft-stockmap-1",
            source_document_type="receiving",
            source_document_id="501",
            destination_location=draft_location,
            destination_zone_code="OS",
            status=WarehouseOperation.STATUS_CREATED,
            comment="PAL-DRAFT-1",
        )
        WarehouseOperation.objects.create(
            agency=self.agency,
            operation_type=WarehouseOperation.TYPE_PUTAWAY,
            context_type="receiving",
            context_id="501",
            source_document_type="stock_move",
            source_document_id="MOVE-1",
            destination_location=planned_location,
            destination_zone_code="OS",
            status=WarehouseOperation.STATUS_PLANNED,
            comment="PAL-PLAN-1",
        )

        response = self.client.get("/stockmap/visual/?picker=1&draft_token=draft-stockmap-1")

        self.assertEqual(response.status_code, 200)
        context = response.context
        first_section_row = next(
            row for row in context["os_matrix_rows"] if row["section"] == 1 and row["tier"] == 1
        )
        first_section_cells = {
            cell["detail_key"]: cell
            for cell in first_section_row["cells"]
            if cell["row"] == 1 and cell["detail_key"]
        }
        second_section_row = next(
            row for row in context["os_matrix_rows"] if row["section"] == 2 and row["tier"] == 1
        )
        second_section_cells = {
            cell["detail_key"]: cell
            for cell in second_section_row["cells"]
            if cell["row"] == 1 and cell["detail_key"]
        }
        self.assertEqual(first_section_cells["1:1:1"]["state"], "draft_owned")
        self.assertEqual(first_section_cells["1:1:1"]["display_marker"], "КЗИ")
        self.assertEqual(first_section_cells["1:1:2"]["state"], "planned")
        self.assertEqual(first_section_cells["1:1:2"]["display_marker"], "КЗИ")
        self.assertEqual(second_section_cells["2:1:3"]["state"], "placed")
        self.assertEqual(second_section_cells["2:1:3"]["display_marker"], "КЗИ")

    def test_visual_stockmap_picker_mode_exposes_pick_buttons(self):
        response = self.client.get("/stockmap/visual/?picker=1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="picker-support-zone"', html=False)
        self.assertContains(response, '<option value="PR">PR</option>', html=False)
        self.assertContains(response, 'data-pick-zone="OS"', html=False)
        self.assertContains(response, "draftPickerState.client_marker || params.get('client_marker') || ''", html=False)

    def test_visual_inventory_picker_exposes_occupied_cells(self):
        for zone, row in (("PR", 0), ("OTG", 0), ("OBR", 0), ("MR", 1)):
            WarehouseLocation.objects.get_or_create(
                warehouse_code="MSK",
                zone_code=zone,
                row_no=row,
                section_no=0,
                tier_no=0,
                cell_no=0,
                defaults={
                    "zone_kind": _zone_kind(zone),
                    "display_name": _location_label(zone, row=row),
                },
            )

        response = self.client.get("/stockmap/visual/?picker=1&purpose=inventory")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["inventory_picker_mode"])
        self.assertTrue(response.context["editor_picker_mode"])
        self.assertContains(response, "Отметьте одну или несколько ячеек")
        self.assertContains(response, 'data-pick-row="1"', html=False)
        self.assertContains(response, 'data-pick-section="2"', html=False)
        self.assertContains(response, "const inventoryPickerMode = true;", html=False)
        self.assertContains(response, 'id="inventory-picker-submit"', html=False)
        self.assertContains(response, "Вернуться к инвентаризации", html=False)
        self.assertContains(response, "stockmap-inventory-locations-picked", html=False)
        self.assertContains(response, "inventory_create_stockmap_selection", html=False)
        self.assertContains(response, "window.location.assign(inventoryReturnUrl)", html=False)
        self.assertContains(response, "Добавить зону целиком")
        self.assertContains(response, 'data-inventory-zone="PR"', html=False)
        self.assertContains(response, 'data-inventory-zone="OTG"', html=False)
        self.assertContains(response, 'data-inventory-zone="OBR"', html=False)
        self.assertContains(response, 'data-inventory-zone="MR"', html=False)
        self.assertContains(response, 'data-inventory-row="1"', html=False)
        self.assertEqual(len(response.context["inventory_zone_locations"]), 4)
        self.assertContains(response, "inventoryZoneButtons.forEach", html=False)
        self.assertNotContains(response, 'id="picker-support-apply"', html=False)

    def test_picker_draft_api_sync_and_release_reserves_os_cell(self):
        response = self.client.post(
            "/stockmap/picker/draft/",
            data=json.dumps(
                {
                    "action": "sync",
                    "draft_token": "draft-api-1",
                    "order_id": "501",
                    "agency_id": self.agency.id,
                    "rows": [
                        {
                            "pallet_code": "PAL-DRAFT-API-1",
                            "destination": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
                        }
                    ],
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertIn((1, 1, 1, 1), StockAvailabilityService.occupied_os_cell_keys())

        heartbeat = self.client.post(
            "/stockmap/picker/draft/",
            data=json.dumps({"action": "heartbeat", "draft_token": "draft-api-1"}),
            content_type="application/json",
        )

        self.assertEqual(heartbeat.status_code, 200)
        self.assertTrue(heartbeat.json()["ok"])

        release = self.client.post(
            "/stockmap/picker/draft/",
            data=json.dumps({"action": "release", "draft_token": "draft-api-1"}),
            content_type="application/json",
        )

        self.assertEqual(release.status_code, 200)
        self.assertEqual(release.json()["released_count"], 1)
        self.assertNotIn((1, 1, 1, 1), StockAvailabilityService.occupied_os_cell_keys())


class StockMapPrZoneTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="stockmap_pr_user", password="x")
        Employee.objects.create(
            full_name="PR Storekeeper",
            user=self.user,
            role="storekeeper",
            is_active=True,
        )
        self.client.force_login(self.user)
        self.agency = Agency.objects.create(agn_name="Клиент PR")
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="700",
            sku="SKU-PR-1",
            name="Куртка",
            size="46",
            barcode="2000000000100",
            goods_type="Оптовый",
            qty=80,
            available_qty=80,
            box_code="BOX-PR-1",
            pallet_code="PAL-PR-1",
            zone="PR",
            location="PR · Зона приемки",
        )

    def test_stockmap_main_redirects_to_visual_scheme(self):
        response = self.client.get("/stockmap/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/stockmap/visual/")

    def test_stockmap_main_picker_redirects_to_visual_scheme_with_query(self):
        response = self.client.get("/stockmap/?picker=1&near_zone=OS&near_row=4")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/stockmap/visual/?picker=1&near_zone=OS&near_row=4")

    def test_pr_zone_page_creates_reachtruck_task_to_os(self):
        page = self.client.get("/stockmap/pr/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "PAL-PR-1")
        self.assertContains(page, "OS · 0-1/1-1")

        response = self.client.post(
            "/stockmap/pr/",
            {
                "selected_pallets": ["PAL-PR-1"],
                "destinations_json": page.context["destinations_json"],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/stockmap/pr/?created=1&skipped=0", response.url)
        task = MoveTask.objects.get()
        self.assertEqual(task.pallet_code, "PAL-PR-1")
        self.assertEqual(task.from_zone, "PR")
        self.assertEqual(task.to_zone, "OS")
        self.assertEqual(task.to_row, 1)
        self.assertEqual(task.to_section, 1)
        self.assertEqual(task.to_tier, 1)
        self.assertEqual(task.to_cell, 1)


class StockMapServiceTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = get_user_model().objects.create_user(username="stockmap_service_user", password="x")
        Employee.objects.create(
            full_name="Service Storekeeper",
            user=self.user,
            role="storekeeper",
            is_active=True,
        )
        self.client.force_login(self.user)
        self.agency = Agency.objects.create(agn_name="Клиент service stockmap")

    def test_build_stock_map_context_counts_pr_occupancy(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="900",
            sku="SKU-SVC-1",
            name="Пальто",
            size="48",
            barcode="2000000000900",
            goods_type="Оптовый",
            qty=20,
            available_qty=20,
            box_code="BOX-SVC-1",
            pallet_code="PAL-SVC-1",
            zone="PR",
            location="PR · Зона приемки",
        )
        request = self.factory.get("/stockmap/")
        request.user = self.user

        context = build_stock_map_context(request=request)

        pr_cell = next(cell for cell in context["cells"] if cell["zone"] == "PR")
        self.assertEqual(pr_cell["occupied"], 1)
        self.assertEqual(pr_cell["free"], 149)

    def test_build_stock_map_context_uses_warehouse_snapshot_when_available(self):
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="PR",
            zone_kind=WarehouseLocation.ZONE_KIND_RECEIVING,
            display_name="PR · Зона приемки",
        )
        container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="PAL-SNAP-1",
            current_location=location,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id="901",
            sku_code="SKU-SNAP-1",
            name="Пальто snapshot",
            qty=20,
            available_qty=20,
            container=container,
            container_code=container.container_code,
            location=location,
            zone_code="PR",
            zone_kind=location.zone_kind,
            warehouse_state_code="placed_in_receiving",
        )
        request = self.factory.get("/stockmap/")
        request.user = self.user

        context = build_stock_map_context(request=request)

        pr_cell = next(cell for cell in context["cells"] if cell["zone"] == "PR")
        self.assertEqual(pr_cell["occupied"], 1)
        self.assertEqual(pr_cell["free"], 149)

    def test_os_row_cell_details_restore_box_counts_when_snapshot_has_no_box_codes(self):
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=1,
            section_no=2,
            tier_no=1,
            cell_no=3,
            location_code="A-1/1-3",
            display_name="OS · A-1/1-3",
        )
        container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="PAL-SNAP-BOXES",
            current_location=location,
        )
        for _ in range(2):
            WarehouseStockSnapshot.objects.create(
                agency=self.agency,
                source_context_type="receiving",
                source_context_id="905",
                sku_code="SKU-SNAP-BOX",
                name="Пальто snapshot box",
                size="50",
                barcode="2000000001905",
                goods_type="Оптовый",
                qty=50,
                available_qty=50,
                container=container,
                container_code=container.container_code,
                location=location,
                zone_code="OS",
                zone_kind=location.zone_kind,
                warehouse_state_code="placed_in_storage",
            )

        details = _os_row_cell_details(1)

        pallet = details["2:1:3"]["pallets"][0]
        self.assertEqual(pallet["box_count"], 2)
        self.assertEqual(pallet["items"][0]["box_count"], 2)

    def test_visual_context_supports_custom_i_line_geometry(self):
        request = self.factory.get("/stockmap/visual/")
        request.user = self.user

        context = build_stock_map_visual_context(request=request)

        self.assertEqual(context["os_row_numbers"], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])

        i_rows = [row for row in context["os_matrix_rows"] if row["section"] == 9]
        self.assertEqual(len(i_rows), 5)
        self.assertEqual(i_rows[0]["section_label"], "I")

        first_i_line_racks = [cell["row"] for cell in i_rows[0]["cells"] if cell["cell"] == 1 and cell["state"] != "unavailable"]
        self.assertEqual(first_i_line_racks, [1, 2, 3, 4, 5])

        fifth_i_line_racks = [cell["row"] for cell in i_rows[-1]["cells"] if cell["cell"] == 1 and cell["state"] != "unavailable"]
        self.assertEqual(fifth_i_line_racks, [4, 5])

        g_rows = [row for row in context["os_matrix_rows"] if row["section"] == 8]
        first_g_line_racks = [cell["row"] for cell in g_rows[0]["cells"] if cell["cell"] == 1 and cell["state"] != "unavailable"]
        self.assertEqual(first_g_line_racks, [1, 2, 3, 4, 5, 6])
        first_g_visual_columns = [cell["visual_row"] for cell in g_rows[0]["cells"] if cell["cell"] == 1 and cell["state"] != "unavailable"]
        self.assertEqual(first_g_visual_columns, [5, 6, 7, 8, 9, 10])
        self.assertEqual(context["os_lower_columns"][4]["actual_row"], 1)
        self.assertEqual(context["os_lower_columns"][9]["actual_row"], 6)

        e_rows = [row for row in context["os_matrix_rows"] if row["section"] == 6]
        first_e_line_racks = [cell["row"] for cell in e_rows[0]["cells"] if cell["cell"] == 1 and cell["state"] != "unavailable"]
        self.assertEqual(first_e_line_racks, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])

        zero_line_first_floor_stellazh_four = [
            cell for cell in context["os_matrix_rows"]
            if cell["section"] == 1 and cell["tier"] == 1
        ][0]["cells"][9:12]
        self.assertEqual([cell["state"] for cell in zero_line_first_floor_stellazh_four], ["passage", "passage", "passage"])

        a_line_second_floor_stellazh_four = [
            cell for cell in context["os_matrix_rows"]
            if cell["section"] == 2 and cell["tier"] == 2
        ][0]["cells"][9:12]
        self.assertEqual([cell["state"] for cell in a_line_second_floor_stellazh_four], ["passage", "passage", "passage"])
        self.assertEqual(context["os_rows"][3]["total_slots"], 75)

    def test_parse_pr_destinations_json_keeps_only_valid_os_locations(self):
        result = parse_pr_destinations_json(
            json.dumps(
                [
                    {
                        "pallet_code": "PAL-1",
                        "destination": {"zone": "OS", "row": 1, "section": 2, "tier": 3, "cell": 1},
                    },
                    {
                        "pallet_code": "PAL-2",
                        "destination": {"zone": "MR", "row": 1},
                    },
                    {
                        "pallet_code": "PAL-3",
                        "destination": {"zone": "OS", "row": 4, "section": 1, "tier": 1, "cell": 1},
                    },
                ]
            )
        )

        self.assertEqual(
            result,
            {
                "PAL-1": {
                    "zone": "OS",
                    "row": 1,
                    "section": 2,
                    "tier": 3,
                    "cell": 1,
                }
            },
        )

    def test_submit_stock_map_pr_moves_returns_error_when_no_selection(self):
        request = self.factory.post("/stockmap/pr/", {"destinations_json": "[]"})
        request.user = self.user

        response = submit_stock_map_pr_moves(request=request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            unquote(response.url),
            "/stockmap/pr/?error=Выберите хотя бы одну паллету.",
        )
