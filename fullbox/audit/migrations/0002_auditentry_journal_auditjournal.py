from django.db import migrations, models
import django.db.models.deletion


def create_default_journal(apps, schema_editor):
    AuditJournal = apps.get_model("audit", "AuditJournal")
    AuditJournal.objects.get_or_create(
        code="sku",
        defaults={
            "name": "Изменения номенклатуры",
            "description": "Фиксация всех операций с номенклатурой (создание, изменение, удаление, клонирование).",
        },
    )


def set_existing_entries_journal(apps, schema_editor):
    AuditJournal = apps.get_model("audit", "AuditJournal")
    AuditEntry = apps.get_model("audit", "AuditEntry")
    journal, _ = AuditJournal.objects.get_or_create(
        code="sku",
        defaults={
            "name": "Изменения номенклатуры",
            "description": "Фиксация всех операций с номенклатурой (создание, изменение, удаление, клонирование).",
        },
    )
    AuditEntry.objects.filter(journal__isnull=True).update(journal=journal)


class Migration(migrations.Migration):

    dependencies = [
        ('audit', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='AuditJournal',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(max_length=64, unique=True, verbose_name='Код')),
                ('name', models.CharField(max_length=255, verbose_name='Название')),
                ('description', models.TextField(blank=True, verbose_name='Описание')),
            ],
            options={
                'verbose_name': 'Журнал',
                'verbose_name_plural': 'Журналы',
                'ordering': ['code'],
            },
        ),
        migrations.RunPython(create_default_journal, migrations.RunPython.noop),
        migrations.AddField(
            model_name='auditentry',
            name='journal',
            field=models.ForeignKey(default=None, on_delete=django.db.models.deletion.CASCADE, related_name='entries', to='audit.auditjournal', verbose_name='Журнал'),
            preserve_default=False,
        ),
        migrations.RunPython(set_existing_entries_journal, migrations.RunPython.noop),
    ]
