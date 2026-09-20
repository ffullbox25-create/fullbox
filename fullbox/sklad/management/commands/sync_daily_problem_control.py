from __future__ import annotations

import json
from datetime import date, datetime, time

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from employees.models import Employee
from sklad.services.problem_boxes import (
    daily_problem_control_route,
    missing_box_rows,
    problem_box_rows,
    problem_pallet_rows,
)
from todo.models import Task


class Command(BaseCommand):
    help = "Создает ежедневную задачу склада по проблемным коробам и паллетам."

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            dest="target_date",
            help="Дата проверки в формате YYYY-MM-DD; по умолчанию текущая дата склада.",
        )

    def handle(self, *args, **options):
        target_day = self._target_day(options.get("target_date"))
        storekeeper = (
            Employee.objects.filter(role="storekeeper", is_active=True)
            .order_by("full_name", "id")
            .first()
        )
        if storekeeper is None:
            raise CommandError("Нет активного кладовщика для ежедневной задачи.")

        observer = (
            Employee.objects.filter(role="head_manager", is_active=True)
            .order_by("full_name", "id")
            .first()
        )
        boxes = problem_box_rows()
        pallets = problem_pallet_rows()
        missing_boxes = missing_box_rows()
        route = daily_problem_control_route(target_day)
        issue_count = len(boxes) + len(pallets) + len(missing_boxes)
        due_date = timezone.make_aware(
            datetime.combine(target_day, time(hour=18)),
            timezone.get_current_timezone(),
        )
        defaults = {
            "title": f"Ежедневная проверка проблемной тары · {target_day:%d.%m.%Y}",
            "description": self._description(
                boxes=boxes,
                pallets=pallets,
                missing_boxes=missing_boxes,
            ),
            "assigned_to": storekeeper,
            "observer": observer,
            "priority": "high" if issue_count else "normal",
            "due_date": due_date,
        }

        with transaction.atomic():
            Employee.objects.select_for_update().get(pk=storekeeper.pk)
            task, created = Task.objects.select_for_update().get_or_create(
                route=route,
                defaults={
                    **defaults,
                    "status": "in_progress" if issue_count else "done",
                },
            )
            if not created:
                for field, value in defaults.items():
                    setattr(task, field, value)
                if task.status != "done":
                    task.status = "in_progress" if issue_count else "done"
                task.save(
                    update_fields=[
                        "title",
                        "description",
                        "assigned_to",
                        "observer",
                        "priority",
                        "due_date",
                        "status",
                        "updated_at",
                    ]
                )

        result = {
            "date": target_day.isoformat(),
            "task_id": task.id,
            "created": created,
            "status": task.status,
            "problem_boxes": len(boxes),
            "problem_pallets": len(pallets),
            "missing_boxes": len(missing_boxes),
        }
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))

    @staticmethod
    def _target_day(raw_value: str | None) -> date:
        if not raw_value:
            return timezone.localdate()
        try:
            return date.fromisoformat(str(raw_value).strip())
        except ValueError as exc:
            raise CommandError("Дата должна быть в формате YYYY-MM-DD.") from exc

    @staticmethod
    def _description(
        *,
        boxes: list[dict],
        pallets: list[dict],
        missing_boxes: list[dict],
    ) -> str:
        box_codes = ", ".join(row["box_code"] for row in boxes[:10]) or "нет"
        pallet_codes = ", ".join(row["pallet_code"] for row in pallets[:10]) or "нет"
        missing_codes = ", ".join(row["box_code"] for row in missing_boxes[:10]) or "нет"
        return "\n".join(
            (
                "Автоматическая проверка только прочитала складские данные; остатки, резервы, зоны и движения не изменялись.",
                f"Проблемные короба: {len(boxes)}. ШК: {box_codes}.",
                f"Проблемные паллеты: {len(pallets)}. ШК: {pallet_codes}.",
                f"Нет на месте: {len(missing_boxes)}. ШК: {missing_codes}.",
                "Откройте складские остатки, проверьте тару физически, добавьте результат комментарием и завершите задачу.",
            )
        )
