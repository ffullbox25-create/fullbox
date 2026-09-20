import json

from django.db import transaction
from django.http import JsonResponse, HttpResponseRedirect
from django.views.generic import ListView, CreateView, UpdateView
from django.shortcuts import redirect, get_object_or_404
from django.views.decorators.http import require_POST

from employees.access import RoleRequiredMixin, STAFF_ROLES, role_required

from market_sync.sync_services import run_ozon_sync_request, run_wb_sync_request

from .audit_history import (
    build_sku_audit_snapshot,
    sku_audit_description,
    sku_audit_snapshot,
)
from .models import Agency, MarketplaceBinding, SKU, MarketCredential
from .forms import SKUBarcodeFormSet, SKUForm
from audit.models import log_sku_change
from .services import (
    get_catalog_mode,
    DEFAULT_SORT,
    FILTER_FIELDS,
    SORT_FIELDS,
    VIEW_MODES,
    build_sku_duplicate_initial,
    build_sku_form_context,
    build_sku_list_context,
    build_sku_list_queryset,
    build_temporary_nomenclature_queryset,
    build_sku_sort_url,
    clone_sku_to_admin,
    mark_sku_deleted,
    suggest_sku_payload,
)


class SKUListView(RoleRequiredMixin, ListView):
    allowed_roles = tuple(STAFF_ROLES)
    model = SKU
    paginate_by = 20
    template_name = 'sku/sku_list.html'
    context_object_name = 'items'
    view_modes = VIEW_MODES
    sort_fields = SORT_FIELDS
    filter_fields = FILTER_FIELDS
    default_sort = DEFAULT_SORT

    def get_queryset(self):
        if get_catalog_mode(self.request) == "temporary":
            from sklad.models import WarehouseTemporaryNomenclature

            return build_temporary_nomenclature_queryset(
                self.request,
                base_qs=WarehouseTemporaryNomenclature.objects.all(),
            )
        return build_sku_list_queryset(self.request, base_qs=super().get_queryset())

    def build_sort_url(self, field: str, direction: str) -> str:
        return build_sku_sort_url(self.request, field, direction)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(build_sku_list_context(self.request, items=ctx["items"]))
        return ctx


@role_required(*STAFF_ROLES)
def suggest_sku(request):
    """Возвращает подсказки для поля поиска SKU."""
    return JsonResponse(
        suggest_sku_payload(
            request.GET.get("q"),
            catalog_mode=get_catalog_mode(request),
        )
    )


@role_required(*STAFF_ROLES)
@require_POST
def clone_sku(request, pk: int):
    """Создает копию SKU и отправляет в админку для редактирования."""
    return redirect(clone_sku_to_admin(pk=pk, user=request.user))


def _normalize_marketplace_code(value: str | None) -> str:
    text = str(value or "").strip().upper()
    if text in {"WB", "WILDBERRIES", "WILDBERRIES (WB)"}:
        return "WB"
    if text in {"OZON", "O-ZON"}:
        return "OZON"
    return ""


def _configured_marketplace_codes(agency) -> list[str]:
    codes = []
    credentials = MarketCredential.objects.filter(agency=agency).select_related("market")
    for credential in credentials:
        code = _normalize_marketplace_code(getattr(credential.market, "name", ""))
        if code == "WB" and (credential.market_key or "").strip():
            codes.append(code)
        elif code == "OZON" and (credential.market_key or "").strip() and (credential.client_id or "").strip():
            codes.append(code)
    return list(dict.fromkeys(codes))


@role_required(*STAFF_ROLES)
@require_POST
def sync_marketplaces(request, pk: int):
    sku = get_object_or_404(
        SKU.objects.select_related("agency", "market").prefetch_related("marketplace_bindings"),
        pk=pk,
        deleted=False,
    )
    if not sku.agency_id or sku.agency is None:
        return JsonResponse({"ok": False, "errors": ["У товара не указан клиент."]}, status=400)

    binding_by_market = {}
    preferred_markets = []
    for binding in sku.marketplace_bindings.all():
        code = _normalize_marketplace_code(binding.marketplace)
        if not code:
            continue
        binding_by_market[code] = binding
        preferred_markets.append(code)
    market_code = _normalize_marketplace_code(getattr(sku.market, "name", ""))
    if market_code:
        preferred_markets.append(market_code)
    configured_markets = _configured_marketplace_codes(sku.agency)
    target_markets = list(dict.fromkeys(preferred_markets or configured_markets))
    if not target_markets:
        return JsonResponse(
            {
                "ok": False,
                "errors": ["Для клиента не настроены поддерживаемые маркетплейсы WB или Ozon."],
            },
            status=400,
        )

    results = {}
    errors = []
    processed_total = 0
    created_total = 0
    updated_total = 0
    barcodes_total = 0

    for code in target_markets:
        payload = {
            "client": sku.agency_id,
            "sku_code": sku.sku_code,
        }
        binding = binding_by_market.get(code)
        if binding and (binding.external_id or "").strip():
            payload["external_id"] = str(binding.external_id).strip()
        body = json.dumps(payload).encode("utf-8")
        response = run_wb_sync_request(body=body) if code == "WB" else run_ozon_sync_request(body=body)
        try:
            result_payload = json.loads(response.content.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            result_payload = {}
        if not isinstance(result_payload, dict):
            result_payload = {}
        results[code] = result_payload
        processed_total += int(result_payload.get("processed") or 0)
        created_total += int(result_payload.get("created") or 0)
        updated_total += int(result_payload.get("updated") or 0)
        barcodes_total += int(result_payload.get("barcodes_created") or 0)
        if response.status_code >= 400 or (result_payload.get("ok") is False and int(result_payload.get("processed") or 0) <= 0):
            market_errors = result_payload.get("errors") or [f"Ошибка синхронизации {code.lower()}."]
            errors.extend([f"{code}: {str(error)}" for error in market_errors if str(error).strip()])

    if processed_total <= 0 and not errors:
        return JsonResponse(
            {
                "ok": False,
                "errors": [f"Товар {sku.sku_code} не найден в подключенных маркетплейсах."],
                "results": results,
            },
            status=404,
        )

    partial = processed_total > 0 and bool(errors)
    ok = processed_total > 0 and not errors
    message_parts = []
    for code, result_payload in results.items():
        message_parts.append(
            f"{code}: обработано {int(result_payload.get('processed') or 0)}, "
            f"создано {int(result_payload.get('created') or 0)}, "
            f"обновлено {int(result_payload.get('updated') or 0)}"
        )

    return JsonResponse(
        {
            "ok": ok,
            "partial": partial,
            "sku_id": sku.id,
            "sku_code": sku.sku_code,
            "processed_total": processed_total,
            "created_total": created_total,
            "updated_total": updated_total,
            "barcodes_created_total": barcodes_total,
            "message": "; ".join(message_parts),
            "results": results,
            "errors": errors,
        },
        status=200 if (ok or partial) else 400,
    )


class SKUFormMixin(RoleRequiredMixin):
    allowed_roles = tuple(STAFF_ROLES)
    model = SKU
    form_class = SKUForm
    template_name = "sku/sku_form.html"
    success_url = "/sku/"
    barcode_formset_prefix = "barcodes"

    def get_barcode_formset(self, *, instance=None):
        kwargs = {
            "instance": instance if instance is not None else getattr(self, "object", None),
            "prefix": self.barcode_formset_prefix,
        }
        if self.request.method == "POST":
            kwargs["data"] = self.request.POST
            kwargs["files"] = self.request.FILES
        return SKUBarcodeFormSet(**kwargs)

    def form_valid(self, form):
        barcode_formset = self.get_barcode_formset(instance=form.instance)
        if not barcode_formset.is_valid():
            return self.render_to_response(
                self.get_context_data(form=form, barcode_formset=barcode_formset)
            )

        with transaction.atomic():
            before_snapshot = None
            if form.instance.pk:
                persisted = (
                    SKU.objects.select_related("agency", "market", "color_ref", "stor_unit")
                    .prefetch_related("barcodes")
                    .filter(pk=form.instance.pk)
                    .first()
                )
                before_snapshot = sku_audit_snapshot(persisted)

            self.object = form.save()
            barcode_formset.instance = self.object
            barcode_formset.save()

            primary_barcode = self.object.barcodes.filter(is_primary=True).first()
            primary_value = primary_barcode.value if primary_barcode else None
            if self.object.code != primary_value:
                self.object.code = primary_value
                self.object.save(update_fields=["code", "updated_at"])

            action = getattr(self, "audit_action", "update")
            snapshot = build_sku_audit_snapshot(
                self.object,
                before=before_snapshot,
                source="ui",
            )
            log_sku_change(
                action,
                self.object,
                user=self.request.user if self.request.user.is_authenticated else None,
                description=sku_audit_description(
                    action,
                    snapshot,
                    fallback="Сохранение карточки SKU без изменения полей.",
                ),
                snapshot=snapshot,
            )

        return HttpResponseRedirect(self.get_success_url())

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        form = ctx.get("form")
        instance = getattr(form, "instance", None) or getattr(self, "object", None)
        if "barcode_formset" not in ctx:
            ctx["barcode_formset"] = self.get_barcode_formset(instance=instance)
        ctx["ozon_sku_values"] = (
            list(
                instance.marketplace_bindings.filter(
                    marketplace=MarketplaceBinding.MARKETPLACE_OZON_SKU,
                ).values_list("external_id", flat=True)
            )
            if instance is not None and instance.pk
            else []
        )
        ctx.update(
            build_sku_form_context(
                mode=getattr(self, "mode", "edit"),
                title=getattr(self, "title", "SKU"),
                submit_label=getattr(self, "submit_label", "Сохранить"),
            )
        )
        return ctx


class SKUCreateView(SKUFormMixin, CreateView):
    mode = "create"
    title = "Создание SKU"
    submit_label = "Создать"
    audit_action = "create"


class SKUUpdateView(SKUFormMixin, UpdateView):
    mode = "edit"
    title = "Редактирование SKU"
    submit_label = "Сохранить"
    audit_action = "update"


class SKUDuplicateView(SKUFormMixin, CreateView):
    mode = "duplicate"
    title = "Копирование SKU"
    submit_label = "Создать копию"
    audit_action = "clone"

    def get_initial(self):
        return build_sku_duplicate_initial(pk=self.kwargs["pk"])


@role_required(*STAFF_ROLES)
@require_POST
def mark_deleted(request, pk: int):
    return mark_sku_deleted(pk=pk, user=request.user)
