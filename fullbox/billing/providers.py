from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings


@dataclass
class ProviderResult:
    ok: bool
    external_id: str = ""
    external_url: str = ""
    message: str = ""


class BillingProviderInterface:
    code = "interface"

    def create_invoice(self, invoice) -> ProviderResult:
        raise NotImplementedError

    def send_invoice(self, invoice) -> ProviderResult:
        raise NotImplementedError


class InternalBillingProvider(BillingProviderInterface):
    code = "internal"

    def create_invoice(self, invoice) -> ProviderResult:
        return ProviderResult(ok=True, message="Внутренний счет создан в WMS.")

    def send_invoice(self, invoice) -> ProviderResult:
        return ProviderResult(ok=True, message="Счет отправлен внутри WMS.")


class MoySkladBillingProvider(BillingProviderInterface):
    code = "moysklad"

    def create_invoice(self, invoice) -> ProviderResult:
        if not getattr(settings, "MOYSKLAD_BILLING_ENABLED", False):
            return ProviderResult(ok=False, message="Интеграция МойСклад отключена настройкой MOYSKLAD_BILLING_ENABLED.")
        return ProviderResult(ok=False, message="Провайдер МойСклад пока не подключен.")

    def send_invoice(self, invoice) -> ProviderResult:
        if not getattr(settings, "MOYSKLAD_BILLING_ENABLED", False):
            return ProviderResult(ok=False, message="Интеграция МойСклад отключена настройкой MOYSKLAD_BILLING_ENABLED.")
        return ProviderResult(ok=False, message="Провайдер МойСклад пока не подключен.")


def get_billing_provider() -> BillingProviderInterface:
    if getattr(settings, "MOYSKLAD_BILLING_ENABLED", False):
        return MoySkladBillingProvider()
    return InternalBillingProvider()
