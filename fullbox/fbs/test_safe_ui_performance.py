from django.contrib.auth import get_user_model
from django.db import connection
from django.test import RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from employees.models import Employee
from sklad.models import WarehouseContainer, WarehouseLocation
from sku.models import Agency, SKU

from .controller_views import _decorate_check_tote_dashboard
from .models import (
    FbsBox,
    FbsControllerCheckTote,
    FbsControllerPickTote,
    FbsControllerSession,
    FbsControllerToteOrder,
    FbsIntegrationProfile,
    FbsOrder,
    FbsOrderLabel,
    FbsPallet,
    FbsPickBatch,
    FbsPickingCart,
    FbsStockBalance,
    FbsStorageCell,
    FbsToteZone,
    FbsWorkstation,
)
from .operator_views import _orders_context
from .tsd_views import (
    _fbs_stock_apply_filters,
    _fbs_stock_box_groups,
    _fbs_stock_grouped_sort,
)


@override_settings(FBS_MODULE_ENABLED=True)
class FbsOperatorActiveArchiveTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="fbs-safe-ui-storekeeper")
        Employee.objects.create(
            user=self.user,
            full_name="FBS safe UI storekeeper",
            role="storekeeper",
        )
        self.agency = Agency.objects.create(agn_name="FBS safe UI client")
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="FBS safe UI profile",
            external_account_id="fbs-safe-ui-account",
            external_warehouse_id="fbs-safe-ui-warehouse",
        )
        self.request_factory = RequestFactory()

    def _order(self, suffix, status):
        return FbsOrder.objects.create(
            profile=self.profile,
            external_order_id=f"FBS-SAFE-UI-{suffix}",
            internal_status=status,
        )

    def _request(self, query=""):
        request = self.request_factory.get(f"/fbs/operator/orders/{query}")
        request.user = self.user
        return request

    def test_handed_over_orders_are_visible_only_in_archive(self):
        active = self._order("ACTIVE", FbsOrder.STATUS_RECEIVED)
        archived = [
            self._order("HANDED", FbsOrder.STATUS_HANDED_OVER),
            self._order("DELIVERED", FbsOrder.STATUS_DELIVERED),
            self._order("RETURNED", FbsOrder.STATUS_RETURNED),
            self._order("CANCELLED", FbsOrder.STATUS_CANCELLED),
        ]

        active_context = _orders_context(self._request())
        archive_context = _orders_context(self._request("?scope=archive"))

        self.assertEqual(active_context["summary"]["total"], 1)
        self.assertEqual(active_context["summary"]["archive"], 4)
        self.assertEqual(
            [order.id for order in active_context["page"].object_list],
            [active.id],
        )
        self.assertEqual(archive_context["orders_scope"], "archive")
        self.assertCountEqual(
            [order.id for order in archive_context["page"].object_list],
            [order.id for order in archived],
        )


@override_settings(FBS_MODULE_ENABLED=True)
class FbsControllerDashboardAggregationTests(TestCase):
    def setUp(self):
        self.controller = get_user_model().objects.create_user(
            username="fbs-safe-ui-controller"
        )
        self.agency = Agency.objects.create(agn_name="FBS dashboard aggregation client")
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="FBS dashboard aggregation profile",
            external_account_id="fbs-dashboard-account",
            external_warehouse_id="fbs-dashboard-warehouse",
        )
        self.workstation = FbsWorkstation.objects.create(
            barcode="FBS-WS-990101",
            name="FBS dashboard workstation",
        )
        self.zone = FbsToteZone.objects.create(
            barcode="FBS-ZONE-SAFE-UI",
            name="FBS safe UI free zone",
            kind=FbsToteZone.KIND_FREE,
        )
        self.unknown_tote = self._cart("000101")
        self.session = FbsControllerSession.objects.create(
            workstation=self.workstation,
            controller=self.controller,
            unknown_tote=self.unknown_tote,
            free_zone=self.zone,
        )

    def _cart(self, suffix):
        return FbsPickingCart.objects.create(
            barcode=f"FBS-CART-{suffix}",
            name=f"FBS safe UI tote {suffix}",
        )

    def _check_tote(self, suffix, status):
        return FbsControllerCheckTote.objects.create(
            session=self.session,
            tote=self._cart(suffix),
            agency=self.agency,
            profile=self.profile,
            status=status,
            opened_by=self.controller,
        )

    def _tote_order(self, check_tote, suffix, *, units, status):
        order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id=f"FBS-DASHBOARD-{suffix}",
        )
        label = FbsOrderLabel.objects.create(
            order=order,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            external_label_id=f"FBS-DASHBOARD-LABEL-{suffix}",
            barcode=f"FBS-DASHBOARD-BARCODE-{suffix}",
            status=FbsOrderLabel.STATUS_APPLIED,
        )
        batch = FbsPickBatch.objects.create(
            agency=self.agency,
            planned_qty=units,
            picked_qty=units,
            assigned_to=self.controller,
        )
        pick_tote = FbsControllerPickTote.objects.create(
            session=self.session,
            check_tote=check_tote,
            pick_batch=batch,
            tote=self._cart(suffix),
            planned_qty=units,
            processed_qty=units,
        )
        return FbsControllerToteOrder.objects.create(
            check_tote=check_tote,
            pick_tote=pick_tote,
            order=order,
            label=label,
            status=status,
            units=units,
            label_confirmed_by=self.controller,
        )

    def test_active_tote_totals_are_decorated_with_one_query(self):
        first = self._check_tote("000102", FbsControllerCheckTote.STATUS_READY)
        second = self._check_tote(
            "000103", FbsControllerCheckTote.STATUS_WAITING_KIZ
        )
        self._tote_order(
            first,
            "000104",
            units=2,
            status=FbsControllerToteOrder.STATUS_LABELED,
        )
        self._tote_order(
            first,
            "000105",
            units=9,
            status=FbsControllerToteOrder.STATUS_REMOVED,
        )
        self._tote_order(
            second,
            "000106",
            units=3,
            status=FbsControllerToteOrder.STATUS_COMPOSITION,
        )

        with CaptureQueriesContext(connection) as captured:
            _decorate_check_tote_dashboard([first, second])

        self.assertEqual(len(captured), 1)
        self.assertEqual((first.order_count, first.active_unit_count), (1, 2))
        self.assertTrue(first.readiness.ready)
        self.assertEqual((second.order_count, second.active_unit_count), (1, 3))
        self.assertFalse(second.readiness.ready)
        self.assertEqual(second.readiness.display_label, "Ожидает КИЗ/marketplace")


@override_settings(FBS_MODULE_ENABLED=True)
class FbsStockExactSearchTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="fbs-stock-grouping-storekeeper"
        )
        Employee.objects.create(
            user=self.user,
            full_name="FBS stock grouping storekeeper",
            role="storekeeper",
        )
        self.agency = Agency.objects.create(agn_name="FBS exact stock client")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="FBS-EXACT-SKU",
            name="FBS exact stock product",
        )
        location = WarehouseLocation.objects.create(
            zone_code="FBS",
            location_code="FBS-SAFE-SEARCH",
            display_name="FBS safe search cell",
            is_storage=True,
        )
        cell = FbsStorageCell.objects.create(
            cell_code="FBS-SAFE-SEARCH",
            location=location,
        )
        pallet = FbsPallet.objects.create(
            agency=self.agency,
            pallet_code="FBS-SAFE-SEARCH-PALLET",
            cell=cell,
            status=FbsPallet.STATUS_ACTIVE,
        )
        box = FbsBox.objects.create(
            agency=self.agency,
            pallet=pallet,
            box_code="FBS-SAFE-SEARCH-BOX",
            status=FbsBox.STATUS_ACTIVE,
        )
        self.exact = self._balance(box, "exact", "4600000001001")
        self.partial = self._balance(box, "partial", "X4600000001001X")
        self.request_factory = RequestFactory()

    def _balance(
        self,
        box,
        identity_key,
        barcode,
        *,
        marking_code="",
        qty=1,
        sku_code="",
        name="",
    ):
        return FbsStockBalance.objects.create(
            agency=self.agency,
            box=box,
            sku_ref=self.sku,
            identity_key=identity_key,
            sku_code=sku_code or self.sku.sku_code,
            name=name or self.sku.name,
            barcode=barcode,
            marking_code=marking_code,
            qty=qty,
            available_qty=qty,
        )

    def test_exact_scanned_code_does_not_include_partial_matches(self):
        request = self.request_factory.get(
            "/fbs/tsd/storekeeper/stock/",
            {"q": self.exact.barcode},
        )

        queryset, filters = _fbs_stock_apply_filters(
            request,
            FbsStockBalance.objects.all(),
        )

        self.assertEqual(filters["q"], self.exact.barcode)
        self.assertEqual(list(queryset.values_list("id", flat=True)), [self.exact.id])

    def test_free_text_keeps_partial_search_fallback(self):
        request = self.request_factory.get(
            "/fbs/tsd/storekeeper/stock/",
            {"q": "exact stock product"},
        )

        queryset, _ = _fbs_stock_apply_filters(
            request,
            FbsStockBalance.objects.all(),
        )

        self.assertCountEqual(
            list(queryset.values_list("id", flat=True)),
            [self.exact.id, self.partial.id],
        )

    def test_cell_filter_uses_physical_container_location_instead_of_plan_cell(self):
        physical_location = WarehouseLocation.objects.create(
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            location_code="BS-1/1-1",
            display_name="Стеллаж белый",
            is_storage=True,
        )
        source_container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="FBS-SAFE-FILTER-SOURCE",
            current_location=physical_location,
        )
        box = self.exact.box
        box.source_container = source_container
        box.save(update_fields=["source_container"])

        physical_request = self.request_factory.get(
            "/fbs/tsd/storekeeper/stock/",
            {"cell": "BS-1/1-1"},
        )
        physical_queryset, _ = _fbs_stock_apply_filters(
            physical_request,
            FbsStockBalance.objects.all(),
        )
        stale_plan_request = self.request_factory.get(
            "/fbs/tsd/storekeeper/stock/",
            {"cell": "FBS-SAFE-SEARCH"},
        )
        stale_plan_queryset, _ = _fbs_stock_apply_filters(
            stale_plan_request,
            FbsStockBalance.objects.all(),
        )

        self.assertCountEqual(
            list(physical_queryset.values_list("id", flat=True)),
            [self.exact.id, self.partial.id],
        )
        self.assertFalse(stale_plan_queryset.exists())

    def test_stock_rows_are_grouped_by_box_and_product(self):
        box = FbsBox.objects.create(
            agency=self.agency,
            pallet=self.exact.box.pallet,
            box_code="FBS-SAFE-GROUP-BOX",
            status=FbsBox.STATUS_ACTIVE,
        )
        self._balance(
            box,
            "marked-a",
            "4600000002001",
            marking_code="010460000000200121MARK-A",
        )
        self._balance(
            box,
            "marked-b",
            "4600000002001",
            marking_code="010460000000200121MARK-B",
        )
        self._balance(box, "bulk", "4600000002001", qty=3)
        self._balance(
            box,
            "other-product",
            "4600000003001",
            sku_code="FBS-OTHER-SKU",
            name="FBS other stock product",
        )

        balances = list(
            FbsStockBalance.objects.filter(box=box).select_related(
                "agency",
                "box__pallet__cell__location",
            )
        )
        groups = _fbs_stock_box_groups([{"box_id": box.id}], balances)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["box_id"], box.id)
        self.assertEqual(groups[0]["balance_count"], 4)
        self.assertEqual(groups[0]["item_count"], 2)
        self.assertEqual(groups[0]["marking_count"], 2)
        self.assertEqual(groups[0]["qty"], 6)
        grouped_product = next(
            item
            for item in groups[0]["items"]
            if item["balance"].barcode == "4600000002001"
        )
        self.assertEqual(grouped_product["qty"], 5)
        self.assertEqual(
            grouped_product["marking_codes"],
            ["010460000000200121MARK-A", "010460000000200121MARK-B"],
        )

    def test_stock_pagination_queryset_contains_one_row_per_box(self):
        second_box = FbsBox.objects.create(
            agency=self.agency,
            pallet=self.exact.box.pallet,
            box_code="FBS-SAFE-SECOND-BOX",
            status=FbsBox.STATUS_ACTIVE,
        )
        self._balance(second_box, "second-box-item", "4600000004001", qty=4)
        request = self.request_factory.get(
            "/fbs/tsd/storekeeper/stock/",
            {"sort": "box", "dir": "asc"},
        )

        grouped_queryset, sort_key, sort_dir = _fbs_stock_grouped_sort(
            request,
            FbsStockBalance.objects.all(),
        )

        self.assertEqual(sort_key, "box")
        self.assertEqual(sort_dir, "asc")
        self.assertEqual(grouped_queryset.count(), 2)
        self.assertEqual(
            list(grouped_queryset.values_list("box__box_code", flat=True)),
            ["FBS-SAFE-SEARCH-BOX", "FBS-SAFE-SECOND-BOX"],
        )
        first_group = grouped_queryset.first()
        self.assertEqual(first_group["group_qty"], 2)

    def test_stock_page_renders_one_summary_row_per_box(self):
        self.client.force_login(self.user)

        response = self.client.get(
            "/fbs/tsd/storekeeper/stock/",
            {"sort": "box", "dir": "asc"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page_obj"].paginator.count, 1)
        self.assertEqual(len(response.context["box_groups"]), 1)
        self.assertEqual(response.context["box_groups"][0]["balance_count"], 2)
        self.assertContains(response, 'class="stock-box-summary"', count=1)
        self.assertContains(response, 'class="stock-box-detail-row" hidden', count=1)

    def test_stock_page_renders_physical_goods_cell_instead_of_plan_cell(self):
        physical_location = WarehouseLocation.objects.create(
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=3,
            section_no=5,
            tier_no=2,
            cell_no=2,
            location_code="OS-3-5-2-2",
            display_name="OS physical goods cell",
            is_storage=True,
        )
        source_container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="FBS-SAFE-PHYSICAL-SOURCE",
            current_location=physical_location,
        )
        box = self.exact.box
        box.source_container = source_container
        box.save(update_fields=["source_container"])
        self.client.force_login(self.user)

        response = self.client.get(
            "/fbs/tsd/storekeeper/stock/",
            {"sort": "box", "dir": "asc"},
        )

        self.assertEqual(response.status_code, 200)
        group = response.context["box_groups"][0]
        self.assertEqual(group["physical_location_code"], "D-3/2-2")
        self.assertEqual(
            group["physical_location_label"],
            "OS · Линия D · Стеллаж 3 · Этаж 2 · Ячейка 2",
        )
        self.assertContains(response, "Ячейка товара: D-3/2-2")
        self.assertContains(response, "Фактическое место товара: D-3/2-2")
