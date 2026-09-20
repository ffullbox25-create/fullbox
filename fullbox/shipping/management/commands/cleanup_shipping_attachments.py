from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from shipping.models import ShippingOrderAttachment


class Command(BaseCommand):
    help = "Удаляет файлы заявок на отгрузку, срок хранения которых истек."

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=ShippingOrderAttachment.RETENTION_DAYS)
        expired = list(
            ShippingOrderAttachment.objects.filter(uploaded_at__lt=cutoff).only("id", "file")
        )
        deleted_count = 0
        for attachment in expired:
            if attachment.file:
                attachment.file.delete(save=False)
            attachment.delete()
            deleted_count += 1
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted_count} expired shipping attachment(s)."))
