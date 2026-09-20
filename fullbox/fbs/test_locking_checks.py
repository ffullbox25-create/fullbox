from django.test import SimpleTestCase

from .checks import check_fbs_select_for_update_scope, find_unsafe_lock_joins


class FbsSelectForUpdateScopeCheckTests(SimpleTestCase):
    unsafe_source = """
def load_balance():
    return (
        FbsStockBalance.objects.select_for_update()
        .select_related(
            "box__pallet__cell__location",
            "box__source_container__current_location",
        )
        .get(pk=1)
    )
"""

    def test_nullable_join_without_explicit_lock_scope_is_rejected(self):
        issues = find_unsafe_lock_joins(self.unsafe_source)

        self.assertEqual(len(issues), 1)
        self.assertEqual(
            issues[0].relation_path,
            "box__source_container__current_location",
        )

    def test_explicit_lock_scope_allows_nullable_join(self):
        source = self.unsafe_source.replace(
            "select_for_update()",
            'select_for_update(of=("self",))',
        )

        self.assertEqual(find_unsafe_lock_joins(source), [])

    def test_current_fbs_services_pass_lock_scope_check(self):
        self.assertEqual(check_fbs_select_for_update_scope(None), [])
