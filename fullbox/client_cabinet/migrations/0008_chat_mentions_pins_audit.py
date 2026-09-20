import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("client_cabinet", "0007_chat_trip_threads"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="chatthread",
            name="pinned_message",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="pinned_in_threads",
                to="client_cabinet.clientchatmessage",
                verbose_name="Закреплённое сообщение",
            ),
        ),
        migrations.AddField(
            model_name="clientchatmessage",
            name="deleted_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="deleted_chat_messages",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Удалил",
            ),
        ),
        migrations.AddField(
            model_name="clientchatmessage",
            name="edited_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="edited_chat_messages",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Редактировал",
            ),
        ),
        migrations.CreateModel(
            name="ChatMessageMention",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("mention_text", models.CharField(blank=True, max_length=64, verbose_name="Текст упоминания")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "message",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="mentions",
                        to="client_cabinet.clientchatmessage",
                        verbose_name="Сообщение",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="chat_mentions",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Упомянутый",
                    ),
                ),
            ],
            options={
                "verbose_name": "Упоминание в чате",
                "verbose_name_plural": "Упоминания в чате",
            },
        ),
        migrations.CreateModel(
            name="ChatMessageAudit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("edit", "Редактирование"),
                            ("delete", "Удаление"),
                            ("pin", "Закрепление"),
                            ("unpin", "Открепление"),
                        ],
                        db_index=True,
                        max_length=16,
                        verbose_name="Действие",
                    ),
                ),
                ("old_text", models.TextField(blank=True, verbose_name="Было")),
                ("new_text", models.TextField(blank=True, verbose_name="Стало")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="chat_message_audits",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Кто",
                    ),
                ),
                (
                    "message",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="audits",
                        to="client_cabinet.clientchatmessage",
                        verbose_name="Сообщение",
                    ),
                ),
                (
                    "thread",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="message_audits",
                        to="client_cabinet.chatthread",
                        verbose_name="Тред",
                    ),
                ),
            ],
            options={
                "verbose_name": "Аудит сообщения чата",
                "verbose_name_plural": "Аудит сообщений чата",
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.AddConstraint(
            model_name="chatmessagemention",
            constraint=models.UniqueConstraint(fields=("message", "user"), name="uniq_chat_mention"),
        ),
        migrations.AddIndex(
            model_name="chatmessagemention",
            index=models.Index(fields=["user", "created_at"], name="client_cabi_user_id_mention_idx"),
        ),
    ]
