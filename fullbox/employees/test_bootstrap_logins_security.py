from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from .management.commands.bootstrap_logins import Command


class BootstrapLoginsSecurityTests(TestCase):
    strong_password = "N7!vQ2@pL9#xR4$k"

    def test_password_argument_is_required(self):
        parser = Command().create_parser("manage.py", "bootstrap_logins")

        with self.assertRaises(CommandError):
            parser.parse_args([])

    def test_weak_password_is_rejected_before_accounts_are_changed(self):
        with self.assertRaises(CommandError):
            call_command("bootstrap_logins", password="1", dry_run=True)

    def test_dry_run_does_not_echo_password(self):
        output = StringIO()

        call_command(
            "bootstrap_logins",
            password=self.strong_password,
            dry_run=True,
            stdout=output,
        )

        self.assertIn("Dry-run mode", output.getvalue())
        self.assertNotIn(self.strong_password, output.getvalue())

    def test_success_output_does_not_echo_password(self):
        output = StringIO()

        call_command(
            "bootstrap_logins",
            password=self.strong_password,
            stdout=output,
        )

        self.assertIn("Done. Created:", output.getvalue())
        self.assertNotIn("Password:", output.getvalue())
        self.assertNotIn(self.strong_password, output.getvalue())
