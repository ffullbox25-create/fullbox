from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from billing.models import BillingApplication, ClientInvoice
from billing.services import BillingWorkflowService
from billing.statuses import BillingStatus, InvoiceStatus, SequenceKind


class Command(BaseCommand):
    help = "Сформировать черновики счетов по полностью рассчитанным заявкам биллинга"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10000, help="Максимум заявок за запуск")
        parser.add_argument(
            "--include-calculation-draft",
            action="store_true",
            help="Также включать заявки со статусом calculation_draft",
        )

    def handle(self, *args, **options):
        limit = max(int(options["limit"] or 10000), 1)
        statuses = [BillingStatus.CALCULATED]
        if options["include_calculation_draft"]:
            statuses.append(BillingStatus.CALCULATION_DRAFT)

        user = get_user_model().objects.filter(is_superuser=True).first()
        qs = (
            BillingApplication.objects.filter(
                billing_status__in=statuses,
                charges_total__gt=0,
                invoices__isnull=True,
            )
            .distinct()
            .order_by("created_at_source", "id")[:limit]
        )

        created_acts = 0
        created_invoices = 0
        skipped = 0

        for application in qs:
            try:
                with transaction.atomic():
                    act = application.acts.exclude(status="cancelled").order_by("-id").first()
                    if act is None:
                        # Пакетный прогон: подтверждаем объёмы с договорной ценой
                        BillingWorkflowService.confirm_application_charges(application, user=user)
                        act = BillingWorkflowService.generate_act(application, user=user)
                        created_acts += 1

                    invoice = ClientInvoice.objects.filter(application=application).exclude(status=InvoiceStatus.CANCELLED).first()
                    if invoice is not None:
                        skipped += 1
                        continue

                    billing_period = act.act_date.replace(day=1)
                    ClientInvoice.objects.create(
                        application=application,
                        client=application.client,
                        legal_entity=application.legal_entity,
                        act=act,
                        number=BillingWorkflowService.next_number(SequenceKind.INVOICE, date=act.act_date),
                        invoice_type=ClientInvoice.TYPE_REGULAR,
                        billing_period=billing_period,
                        invoice_date=act.act_date,
                        due_date=act.act_date + timedelta(days=7),
                        subtotal=act.subtotal,
                        vat_amount=act.vat_amount,
                        total_amount=act.total_amount,
                        debt_amount=act.total_amount,
                        status=InvoiceStatus.DRAFT,
                        created_by=user,
                    )
                    application.invoice_total = act.total_amount
                    application.debt_total = act.total_amount
                    application.billing_status = BillingStatus.INVOICE_DRAFT
                    application.save(update_fields=["invoice_total", "debt_total", "billing_status", "updated_at"])
                    BillingWorkflowService.audit(action="invoice_created", application=application, user=user, obj=application)
                    created_invoices += 1
            except Exception as exc:
                skipped += 1
                self.stderr.write(f"Пропущена заявка {application.pk}: {exc}")

        self.stdout.write(
            self.style.SUCCESS(
                f"Готово: актов создано {created_acts}, счетов создано {created_invoices}, пропущено {skipped}"
            )
        )
