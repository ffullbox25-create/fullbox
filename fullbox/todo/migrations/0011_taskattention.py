from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("employees", "0001_initial"),
        ("todo", "0010_warehouse_request_deadline_change"),
    ]

    operations = [
        migrations.CreateModel(
            name="TaskAttention",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("delivered_at", models.DateTimeField(default=django.utils.timezone.now, verbose_name="Когда доставлено")),
                ("viewed_at", models.DateTimeField(blank=True, null=True, verbose_name="Когда просмотрено")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "employee",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="task_attention_states",
                        to="employees.employee",
                        verbose_name="Получатель",
                    ),
                ),
                (
                    "task",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="attention_states",
                        to="todo.task",
                        verbose_name="Задача",
                    ),
                ),
            ],
            options={
                "verbose_name": "Уведомление о задаче",
                "verbose_name_plural": "Уведомления о задачах",
                "ordering": ["-delivered_at", "-id"],
                "indexes": [
                    models.Index(
                        fields=["employee", "viewed_at"],
                        name="todo_attn_employee_viewed_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("task", "employee"),
                        name="uniq_todo_task_attention_employee",
                    )
                ],
            },
        ),
    ]
