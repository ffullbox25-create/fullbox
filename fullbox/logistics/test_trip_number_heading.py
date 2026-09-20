from django.template.loader import get_template, render_to_string
from django.test import SimpleTestCase


class TripNumberHeadingTests(SimpleTestCase):
    def test_trip_detail_passes_public_number_to_section_heading(self):
        source = get_template("logistics/trip_detail.html").template.source

        self.assertIn(
            'with trips_heading_number=trip_display_number',
            source,
        )

    def test_trips_heading_displays_public_trip_number(self):
        content = render_to_string(
            "logistics/_trips_subnav.html",
            {
                "trips_heading_number": "76_RS",
                "trips_subnav": "active",
            },
        )

        self.assertIn(
            '<span class="trips-page-number"> · 76_RS</span>',
            content,
        )

    def test_trips_heading_stays_generic_outside_trip_detail(self):
        content = render_to_string(
            "logistics/_trips_subnav.html",
            {"trips_subnav": "active"},
        )

        self.assertNotIn('class="trips-page-number"', content)
