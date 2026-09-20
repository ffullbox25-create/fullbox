from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from employees.models import Employee
from sklad.models import WarehouseContainer, WarehouseLocation
from sku.models import Agency, SKU, SKUBarcode

from .models import FbsBox, FbsPallet, FbsStockBalance, FbsStorageCell
from .tsd_views import (
    _picker_agency_short_label,
    _picker_location_balance_queryset,
    _picker_product_balance_queryset,
)


class PickerLocationScannerTemplateTests(SimpleTestCase):
    def test_lookup_field_is_connected_to_tsd_scanner_runtime(self):
        template_path = (
            Path(__file__).resolve().parents[1]
            / "templates"
            / "fbs"
            / "tsd_picker_location.html"
        )
        source = template_path.read_text(encoding="utf-8")

        self.assertIn("data-scan-feedback", source)
        self.assertEqual(source.count("data-scan-focus-lock"), 2)
        self.assertIn("data-scanner-only", source)
        self.assertIn('data-scan-autosubmit="250"', source)
        self.assertIn('enterkeyhint="done"', source)
        self.assertIn('inputmode="text"', source)


@override_settings(FBS_MODULE_ENABLED=True)
class PickerLocationLookupTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="fbs-location-lookup-picker"
        )
        Employee.objects.create(
            user=self.user,
            full_name="Сборщик поиска FBS",
            role="picker",
            is_active=True,
        )
        self.agency = Agency.objects.create(
            agn_name="Индивидуальный предприниматель Чекунов Валентин Игорьевич",
            short_name="ИП Чекунов Валентин Игорьевич",
        )
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="LOOKUP-SKU",
            name="Товар поиска",
        )
        self.lookup_barcode = "4600000004101"
        SKUBarcode.objects.create(
            sku=self.sku,
            value=self.lookup_barcode,
            is_primary=True,
        )
        self.first_pallet, self.first_box = self._container(
            suffix="1",
            location_code="A-4/1-1",
            pallet_code="LOOKUP-PALLET-1",
            box_code="LOOKUP-BOX-1",
        )
        self.second_pallet, self.second_box = self._container(
            suffix="2",
            location_code="B-5/2-2",
            pallet_code="LOOKUP-PALLET-2",
            box_code="LOOKUP-BOX-2",
        )
        FbsStockBalance.objects.create(
            agency=self.agency,
            box=self.first_box,
            sku_ref=self.sku,
            identity_key="lookup-first",
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode="4600000004194",
            qty=5,
            available_qty=3,
            reserved_qty=1,
            external_reserved_qty=1,
        )
        FbsStockBalance.objects.create(
            agency=self.agency,
            box=self.second_box,
            sku_ref=self.sku,
            identity_key="lookup-second",
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode="4600000004194",
            qty=2,
            available_qty=2,
        )
        self.client.force_login(self.user)

    def _container(self, *, suffix, location_code, pallet_code, box_code):
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="FBS",
            row_no=int(suffix),
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code=location_code,
            display_name=f"Место {location_code}",
            is_storage=True,
            is_pickable=True,
        )
        cell = FbsStorageCell.objects.create(
            cell_code=f"LOOKUP-CELL-{suffix}",
            location=location,
        )
        pallet = FbsPallet.objects.create(
            agency=self.agency,
            pallet_code=pallet_code,
            cell=cell,
            status=FbsPallet.STATUS_ACTIVE,
        )
        box = FbsBox.objects.create(
            agency=self.agency,
            pallet=pallet,
            box_code=box_code,
            status=FbsBox.STATUS_ACTIVE,
        )
        return pallet, box

    def _scan(self, value):
        return self.client.post(reverse("fbs:tsd_picker_location"), {"scan": value})

    def test_pallet_scan_shows_its_location(self):
        response = self._scan(self.first_pallet.pallet_code)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["result_kind"], "pallet")
        self.assertEqual(response.context["pallet"], self.first_pallet)
        self.assertContains(response, "Место A-4/1-1")
        self.assertContains(response, self.first_box.box_code)

    def test_box_scan_shows_box_pallet_and_location(self):
        response = self._scan(self.first_box.box_code)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["result_kind"], "box")
        self.assertContains(response, self.first_box.box_code)
        self.assertContains(response, self.first_pallet.pallet_code)
        self.assertContains(response, "Место A-4/1-1")

    def test_product_alias_barcode_shows_every_fbs_placement(self):
        response = self._scan(self.lookup_barcode)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["result_kind"], "product")
        self.assertEqual(len(response.context["product_placements"]), 2)
        self.assertEqual(response.context["totals"]["qty"], 7)
        self.assertEqual(response.context["totals"]["available"], 5)
        self.assertEqual(response.context["totals"]["reserved"], 2)
        self.assertEqual(response.context["totals"]["locations"], 2)
        self.assertContains(response, self.first_box.box_code)
        self.assertContains(response, self.second_box.box_code)
        self.assertContains(response, self.first_pallet.pallet_code)
        self.assertContains(response, self.second_pallet.pallet_code)
        self.assertContains(response, "A-4/1-1")
        self.assertContains(response, "B-5/2-2")
        self.assertContains(response, "ИП Чекунов В. И.", count=2)
        self.assertNotContains(response, "ИП Чекунов Валентин Игорьевич")
        self.assertContains(response, 'class="picker-location-placement-meta"', count=2)
        self.assertContains(response, 'class="picker-location-product-meta"', count=2)
        self.assertContains(response, 'style="font-weight: 650;"', count=4)

    def test_picker_agency_label_keeps_non_ip_short_name(self):
        agency = Agency(
            agn_name='Общество с ограниченной ответственностью "Кейзи"',
            short_name='ООО "Кейзи"',
        )
        self.assertEqual(_picker_agency_short_label(agency), 'ООО "Кейзи"')

    def test_unknown_scan_reports_all_supported_types(self):
        response = self._scan("UNKNOWN-FBS-CODE")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "место, палета, короб или остаток товара")

    def _size_stock(self, size, barcode, *, box=None):
        return FbsStockBalance.objects.create(
            agency=self.agency, box=box or self.first_box, sku_ref=self.sku,
            identity_key=f"size-{size}-{barcode}", sku_code=self.sku.sku_code,
            name=self.sku.name, size=size, barcode=barcode,
            qty=1, available_qty=1,
        )

    def test_exact_barcode_does_not_expand_to_other_sizes(self):
        wanted = self._size_stock("44", "2042642547121")
        second = self._size_stock("44", "2042642547121", box=self.second_box)
        self._size_stock("42", "2042642547114")
        self._size_stock("36", "2042642547084")
        SKUBarcode.objects.create(sku=self.sku, value=wanted.barcode, size="44")
        self.assertSetEqual(
            set(_picker_product_balance_queryset(wanted.barcode).values_list("pk", flat=True)),
            {wanted.pk, second.pk},
        )

    def test_uncatalogued_barcode_does_not_expand_by_sku(self):
        wanted = self._size_stock("44", "2042642547121")
        self._size_stock("42", "2042642547114")
        self.assertEqual(list(_picker_product_balance_queryset(wanted.barcode)), [wanted])

    def test_sized_alias_only_matches_same_size(self):
        wanted = self._size_stock("44", "alternate-44")
        self._size_stock("42", "alternate-42")
        SKUBarcode.objects.create(sku=self.sku, value="size-44-alias", size="44")
        self.assertEqual(list(_picker_product_balance_queryset("size-44-alias")), [wanted])

    def test_unsized_alias_does_not_match_sized_variants(self):
        self._size_stock("44", "size-44-barcode")
        results = _picker_product_balance_queryset(self.lookup_barcode)
        self.assertEqual(results.count(), 2)
        self.assertSetEqual(set(results.values_list("size", flat=True)), {""})

    def test_barcode_takes_precedence_over_matching_article(self):
        wanted = self._size_stock("44", "2042642547121")
        other = self._size_stock("42", "another-barcode")
        other.sku_code = wanted.barcode
        other.save(update_fields=["sku_code"])
        self.assertEqual(list(_picker_product_balance_queryset(wanted.barcode)), [wanted])

    def test_article_search_still_includes_all_sizes(self):
        self._size_stock("44", "size-44-barcode")
        self.assertEqual(_picker_product_balance_queryset(self.sku.sku_code).count(), 3)

    def test_barcode_with_scanner_prefix_keeps_size(self):
        wanted = self._size_stock("44", "2042642547121")
        self._size_stock("42", "2042642547114")
        self.assertEqual(list(_picker_product_balance_queryset("]E0" + wanted.barcode + "\r\n")), [wanted])

    def _set_physical_location(self, location, *, box=None):
        box = box or self.first_box
        container = WarehouseContainer.objects.create(
            agency=self.agency, container_type="box", container_code=box.box_code,
            current_location=location, status="active",
        )
        box.source_container = container
        box.save(update_fields=["source_container"])
        return container

    def test_location_scan_follows_individually_moved_box(self):
        self._set_physical_location(self.second_pallet.cell.location)
        response = self._scan("B-5/2-2")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["totals"]["qty"], 7)
        self.assertEqual(response.context["totals"]["available"], 5)
        self.assertEqual(response.context["totals"]["reserved"], 2)
        self.assertEqual(response.context["totals"]["boxes"], 2)
        self.assertContains(response, self.first_box.box_code)
        self.first_box.refresh_from_db()
        self.assertEqual(self.first_box.pallet_id, self.first_pallet.pk)

    def test_old_location_excludes_moved_box_but_keeps_other_boxes(self):
        remaining = FbsBox.objects.create(
            agency=self.agency, pallet=self.first_pallet,
            box_code="LOOKUP-REMAINING", status=FbsBox.STATUS_ACTIVE,
        )
        self._size_stock("44", "remaining-barcode", box=remaining)
        self._set_physical_location(self.second_pallet.cell.location)
        response = self._scan("A-4/1-1")
        self.assertEqual(response.context["totals"]["qty"], 1)
        self.assertEqual(response.context["totals"]["boxes"], 1)
        self.assertNotContains(response, self.first_box.box_code)
        self.assertContains(response, remaining.box_code)

    def test_location_scan_without_source_container_keeps_legacy_stock(self):
        response = self._scan("A-4/1-1")
        self.assertEqual(response.context["totals"]["qty"], 5)
        self.assertContains(response, self.first_box.box_code)

    def test_location_scan_with_unlocated_source_falls_back_to_pallet(self):
        self._set_physical_location(None)
        self.assertEqual(self._scan("A-4/1-1").context["totals"]["qty"], 5)

    def test_location_scan_with_virtual_source_falls_back_to_pallet(self):
        virtual = WarehouseLocation.objects.create(
            warehouse_code="MSK", zone_code="FBS", zone_kind="virtual",
            location_code="FBS-PLAN-LOOKUP", display_name="Virtual plan",
        )
        self._set_physical_location(virtual)
        self.assertEqual(self._scan("A-4/1-1").context["totals"]["qty"], 5)

    def test_same_physical_and_logical_location_does_not_double_count(self):
        self._set_physical_location(self.first_pallet.cell.location)
        self.assertEqual(self._scan("A-4/1-1").context["totals"]["qty"], 5)

    def test_second_move_removes_box_from_previous_physical_location(self):
        container = self._set_physical_location(self.second_pallet.cell.location)
        third_pallet, _ = self._container(
            suffix="3", location_code="C-6/1-1",
            pallet_code="LOOKUP-PALLET-3", box_code="LOOKUP-BOX-3",
        )
        container.current_location = third_pallet.cell.location
        container.save(update_fields=["current_location"])
        self.assertEqual(self._scan("A-4/1-1").context["totals"]["qty"], 0)
        self.assertEqual(self._scan("B-5/2-2").context["totals"]["qty"], 2)
        self.assertEqual(self._scan("C-6/1-1").context["totals"]["qty"], 5)

    def test_location_scan_excludes_archived_boxes_and_zero_balances(self):
        self._set_physical_location(self.second_pallet.cell.location)
        self.first_box.status = "archived"
        self.first_box.save(update_fields=["status"])
        FbsStockBalance.objects.filter(box=self.second_box).update(qty=0, available_qty=0)
        self.assertEqual(self._scan("B-5/2-2").context["totals"]["qty"], 0)

    def test_physical_location_filter_uses_bounded_queries(self):
        self._set_physical_location(self.second_pallet.cell.location)
        cell = self.second_pallet.cell
        with self.assertNumQueries(2):
            self.assertEqual(len(list(_picker_location_balance_queryset(cell))), 2)
