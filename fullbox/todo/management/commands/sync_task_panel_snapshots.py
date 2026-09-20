from django.core.management.base import BaseCommand

from todo.templatetags.todo_panel import (
    ALL_ROLES_KEY,
    sync_task_panel_snapshots_for_roles,
    task_panel_snapshot_role_keys,
)


class Command(BaseCommand):
    help = "Rebuild task panel snapshots for dashboard roles."

    def add_arguments(self, parser):
        parser.add_argument(
            "roles",
            nargs="*",
            help="Optional role keys to rebuild. Use '__all__' for the all-roles panel.",
        )

    def handle(self, *args, **options):
        requested_roles = [str(role).strip() for role in options["roles"] if str(role or "").strip()]
        if not requested_roles:
            requested_roles = task_panel_snapshot_role_keys()
        if "all" in requested_roles and ALL_ROLES_KEY not in requested_roles:
            requested_roles = [ALL_ROLES_KEY if role == "all" else role for role in requested_roles]
        self.stdout.write("Rebuilding task panel snapshots...")
        sync_task_panel_snapshots_for_roles(requested_roles)
        self.stdout.write(self.style.SUCCESS(f"Done: {', '.join(requested_roles)}"))
