from html.parser import HTMLParser
from types import SimpleNamespace

from django.template.loader import render_to_string
from django.test import SimpleTestCase


class FormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self.current = None
        self.nested = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form':
            self.nested |= self.current is not None
            self.current = {'attrs': attrs, 'inputs': []}
            self.forms.append(self.current)
        if tag == 'input' and self.current is not None:
            self.current['inputs'].append(attrs)

    def handle_endtag(self, tag):
        if tag == 'form':
            self.current = None


class WarehouseLocationsCompactUiTests(SimpleTestCase):
    def location(self, *, zone='PR', active=True, occupied=2, capacity=20):
        return SimpleNamespace(
            id=7, location_code=f'{zone}-F-07', display_name='Стеллаж <белый>',
            zone_code=zone, is_active=active, is_fbs_visible=True,
            allow_mixed_client_pallets=False,
            capacity_containers=capacity,
            occupancy=SimpleNamespace(occupied=occupied, capacity=capacity,
                current_containers=1, pending_containers=1, unregistered_container_codes=0),
            qr_value=f'{zone}-F-07' if zone == 'PR' else f'LOC:MSK:{zone}-F-07',
            fbs_rack_config=SimpleNamespace(rack_code=f'{zone}-F-07') if zone == 'PR' else None,
            fbs_rack_cell_count=1, fbs_rack_cells=[],
        )

    def render_row(self, **kwargs):
        return render_to_string('head_manager/_warehouse_location_row.html', {'location': self.location(**kwargs)})

    def test_compact_summary_keeps_editor_collapsed_and_escapes_names(self):
        html = self.render_row()
        self.assertIn('class="location-row-summary"', html)
        self.assertIn('id="location-editor-7" hidden', html)
        self.assertIn('aria-expanded="false"', html)
        self.assertIn('Стеллаж &lt;белый&gt;', html)
        self.assertIn('form="warehouse-location-print-selected"', html)
        self.assertIn('?print=7', html)

    def test_settings_and_rack_are_independent_post_forms(self):
        parser = FormParser()
        parser.feed(self.render_row())
        self.assertFalse(parser.nested)
        self.assertEqual(len(parser.forms), 2)
        self.assertEqual([form['attrs']['method'] for form in parser.forms], ['post', 'post'])
        self.assertTrue(parser.forms[0]['attrs']['action'].endswith('/7/update/'))
        self.assertTrue(parser.forms[1]['attrs']['action'].endswith('/7/fbs-rack/'))
        self.assertEqual(
            {x['name'] for x in parser.forms[0]['inputs']},
            {
                'display_name', 'capacity_containers', 'is_fbs_visible',
                'allow_mixed_client_pallets', 'is_active',
            },
        )
        self.assertEqual({x['name'] for x in parser.forms[1]['inputs']}, {'fbs_rack_cell_count'})

    def test_occupancy_states_and_unlimited_capacity(self):
        for params, status in [({}, 'Частично занято'), ({'occupied':0}, 'Не занято'),
            ({'occupied':20}, 'Заполнено'), ({'active':False}, 'Выключено'),
            ({'capacity':0}, 'без лимита')]:
            with self.subTest(params=params):
                self.assertIn(status, self.render_row(**params))

    def test_obr_has_no_fbs_toggle_or_rack_mutations(self):
        html = self.render_row(zone='OBR')
        self.assertNotIn('name="is_fbs_visible"', html)
        self.assertNotIn('name="allow_mixed_client_pallets"', html)
        self.assertNotIn('/fbs-rack/', html)
        self.assertIn('LOC:MSK:OBR-F-07', html)

    def test_page_preserves_print_payload_and_creation_controls(self):
        location = self.location()
        html = render_to_string('head_manager/warehouse_locations.html', {
            'request': SimpleNamespace(path='/head-manager/settings/warehouse-locations/', user=SimpleNamespace(username='Test', get_full_name=lambda:'Test')),
            'user_display_name':'Test',
            'locations':[location], 'pr_locations':[location], 'obr_locations':[], 'otg_locations':[],
            'selected_locations':[location], 'summary':{'total':1,'active':1,'fbs':1,'occupied':2},
        })
        self.assertIn('data-code="PR-F-07" aria-label="QR места PR-F-07"', html)
        self.assertIn('id="location-create-form"', html)
        self.assertIn('id="location-status-filter"', html)
        self.assertIn('id="location-group-filter"', html)
        self.assertIn('name="action" value="import"', html)
        self.assertIn('name="allow_mixed_client_pallets"', html)
        self.assertIn('?download_template=1', html)
        parser = FormParser()
        parser.feed(html)
        self.assertFalse(parser.nested)
