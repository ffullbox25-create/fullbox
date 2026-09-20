from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("employees", "0013_employee_packer_role"),
        ("todo", "0009_internal_warehouse_tasks"),
    ]

    operations = [
        migrations.CreateModel(
            name="WarehouseRequestDeadlineChange",
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
                (
                    "request_route",
                    models.CharField(
                        db_index=True,
                        max_length=255,
                        verbose_name="Маршрут складской заявки",
                    ),
                ),
                ("previous_due_date", models.DateTimeField(verbose_name="Предыдущий срок")),
                ("due_date", models.DateTimeField(verbose_name="Новый срок")),
                ("reason", models.TextField(verbose_name="Причина переноса")),
                (
                    "created_at",
                    models.DateTimeField(
                        auto_now_add=True,
                        db_index=True,
                        verbose_name="Когда изменено",
                    ),
                ),
                (
                    "changed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="warehouse_deadline_changes",
                        to="employees.employee",
                        verbose_name="Кто изменил",
                    ),
                ),
            ],
            options={
                "verbose_name": "Перенос срока складской заявки",
                "verbose_name_plural": "Переносы сроков складских заявок",
                "ordering": ["-created_at", "-id"],
                "indexes": [
                    models.Index(
                        fields=["request_route", "-created_at"],
                        name="todo_wh_due_route_created_idx",
                    )
                ],
            },
        ),
    ]
