from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("todo", "0007_alter_task_status"),
    ]

    operations = [
        migrations.CreateModel(
            name="TaskPanelSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("role_key", models.CharField(blank=True, db_index=True, max_length=64)),
                ("task_route", models.CharField(blank=True, max_length=255)),
                ("task_status", models.CharField(blank=True, max_length=32)),
                ("filter_type", models.CharField(blank=True, max_length=32)),
                ("panel_title", models.TextField(blank=True)),
                ("panel_url", models.CharField(blank=True, max_length=255)),
                ("order_client_id", models.IntegerField(blank=True, null=True)),
                ("order_client_label", models.TextField(blank=True)),
                ("order_status_label", models.TextField(blank=True)),
                ("order_status_tone", models.CharField(blank=True, max_length=32)),
                ("order_notice_label", models.TextField(blank=True)),
                ("order_notice_tone", models.CharField(blank=True, max_length=32)),
                ("executor_label", models.TextField(blank=True)),
                ("processing_packers_label", models.TextField(blank=True)),
                ("worker_title", models.TextField(blank=True)),
                ("panel_updated_at_label", models.CharField(blank=True, max_length=32)),
                ("is_hidden", models.BooleanField(default=False)),
                ("snapshot_updated_at", models.DateTimeField(auto_now=True)),
                (
                    "task",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="panel_snapshots",
                        to="todo.task",
                    ),
                ),
            ],
            options={
                "db_table": "todo_task_panel_snapshot",
            },
        ),
        migrations.AddConstraint(
            model_name="taskpanelsnapshot",
            constraint=models.UniqueConstraint(fields=("task", "role_key"), name="uniq_todo_task_panel_snapshot"),
        ),
    ]
