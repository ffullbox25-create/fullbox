from collections import Counter
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from sklad.services import fbs_quantity_reserves as service
from sklad.services.stock_availability import normalize_goods_type
from sklad.services.warehouse_write_path import WarehouseTransitionError


class EmptyDemandGuardTests(SimpleTestCase):
    key = (2951, 'reserved-sku', '0', '4600000000001', normalize_goods_type('gv'))

    def test_empty_counter_does_not_read_capacity(self):
        with patch.object(service, 'capacity') as capacity:
            service.assert_capacity(2951, Counter())
        capacity.assert_not_called()

    def test_empty_dict_does_not_read_capacity(self):
        with patch.object(service, 'capacity') as capacity:
            service.assert_capacity(2951, {}, exclude_request_id=17)
        capacity.assert_not_called()

    def test_nonempty_demand_still_checks_current_capacity(self):
        with patch.object(service, 'capacity', return_value=Counter({self.key: 5})) as capacity:
            service.assert_capacity(2951, {self.key: 5}, exclude_request_id=17)
        capacity.assert_called_once_with(2951, 17)

    def test_shortage_still_blocks(self):
        with patch.object(service, 'capacity', return_value=Counter({self.key: 4})):
            with self.assertRaises(WarehouseTransitionError):
                service.assert_capacity(2951, {self.key: 5})

    def test_missing_stock_identity_still_blocks(self):
        with patch.object(service, 'capacity', return_value=Counter()):
            with self.assertRaises(WarehouseTransitionError):
                service.assert_capacity(2951, {self.key: 1})

    def test_nonempty_zero_demand_retains_existing_semantics(self):
        with patch.object(service, 'capacity', return_value=Counter({self.key: -1})) as capacity:
            with self.assertRaises(WarehouseTransitionError):
                service.assert_capacity(2951, {self.key: 0})
        capacity.assert_called_once()

    def check_claim(self, items, free, blocked=False):
        owner = SimpleNamespace(id=2951)
        with patch.object(service.Agency.objects, 'select_for_update') as lock, \
             patch.object(service, 'pool_reserves') as reserves, \
             patch.object(service, 'demand', return_value=Counter({self.key: 5})), \
             patch.object(service, 'capacity', return_value=Counter({self.key: free})) as capacity:
            reserves.return_value.exists.return_value = True
            if blocked:
                with self.assertRaises(WarehouseTransitionError):
                    service.protect_new_claims(owner, items)
            else:
                service.protect_new_claims(owner, items)
            lock.assert_called_once_with()
            lock.return_value.get.assert_called_once_with(pk=2951)
        return capacity

    def test_unrelated_shipping_item_skips_stock_reload_but_keeps_owner_lock(self):
        capacity = self.check_claim([{'sku_code': 'Упаковочный материал', 'size': '235*100*30',
                                     'barcode': 'нет', 'goods_type': 'rh', 'qty': 1}], 0)
        capacity.assert_not_called()

    def test_matching_shipping_or_processing_claim_still_blocks(self):
        item = {'sku_code': self.key[1], 'size': '0', 'barcode': self.key[3], 'goods_type': 'gv', 'qty': 1}
        capacity = self.check_claim([item], 0, blocked=True)
        capacity.assert_called_once_with(2951, None)

    def test_missing_optional_barcode_cannot_bypass_protection(self):
        item = {'sku': self.key[1], 'size': '0', 'qty': 1}
        capacity = self.check_claim([item], 0, blocked=True)
        capacity.assert_called_once()

    def test_duplicate_items_are_still_aggregated(self):
        item = {'sku_code': self.key[1], 'size': '0', 'qty': 3}
        self.check_claim([item, dict(item)], 5, blocked=True).assert_called_once()

    def test_mixed_related_and_unrelated_claims_still_checked(self):
        items = [{'sku_code': 'unrelated', 'size': '0', 'qty': 100},
                 {'sku_code': self.key[1], 'size': '0', 'qty': 1}]
        self.check_claim(items, 0, blocked=True).assert_called_once()
