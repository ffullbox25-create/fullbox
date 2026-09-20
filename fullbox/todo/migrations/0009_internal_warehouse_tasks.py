from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("employees", "0013_employee_packer_role"),
        ("todo", "0008_taskpanelsnapshot"),
    ]

    operations = [
        migrations.AddField(
            model_name="task",
            name="kind",
            field=models.CharField(
                choices=[
                    ("system", "Системная задача"),
                    ("warehouse_internal", "Внутренняя задача склада"),
                ],
                db_index=True,
                default="system",
                max_length=32,
                verbose_name="Тип задачи",
            ),
        ),
        migrations.AddField(
            model_name="task",
            name="participants",
            field=models.ManyToManyField(
                blank=True,
                related_name="participating_tasks",
                to="employees.employee",
                verbose_name="Соисполнители",
            ),
        ),
        migrations.CreateModel(
            name="TaskChecklistItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(max_length=255, verbose_name="Пункт чек-листа")),
                ("position", models.PositiveIntegerField(default=0, verbose_name="Порядок")),
                ("is_completed", models.BooleanField(default=False, verbose_name="Выполнен")),
                ("completed_at", models.DateTimeField(blank=True, null=True, verbose_name="Когда выполнен")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "completed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="completed_task_checklist_items",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Кто выполнил",
                    ),
                ),
                (
                    "task",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="checklist_items",
                        to="todo.task",
                        verbose_name="Задача",
                    ),
                ),
            ],
            options={
                "verbose_name": "Пункт чек-листа",
                "verbose_name_plural": "Пункты чек-листа",
                "ordering": ["position", "id"],
            },
        ),
        migrations.AddIndex(
            model_name="taskchecklistitem",
            index=models.Index(fields=["task", "is_completed"], name="todo_ch_task_done_idx"),
        ),
    ]
