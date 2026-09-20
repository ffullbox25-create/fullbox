from __future__ import annotations

from django.db.models import Max
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect

from sku.models import Agency, Market, MarketCredential

from .forms import OzonSettingsForm, WBSettingsForm
from .models import MarketSyncReport


def build_dashboard_context(*, client_id) -> dict:
    selected_client = None
    credentials = {}
    agency_options = (
        Agency.objects.filter(archived=False)
        .only("id", "agn_name", "fio_agn", "inn")
        .order_by("agn_name", "fio_agn", "id")
    )
    if client_id:
        selected_client = Agency.objects.filter(pk=client_id).first()
    if selected_client:
        credentials = {
            item.market_id: item
            for item in MarketCredential.objects.filter(agency=selected_client).select_related("market")
        }
    wb_market = Market.objects.filter(name__iexact="WB").first()
    wb_credential = credentials.get(wb_market.id) if wb_market else None
    wb_configured = bool(wb_credential and (wb_credential.market_key or "").strip())
    ozon_market = Market.objects.filter(name__iexact="OZON").first()
    ozon_credential = credentials.get(ozon_market.id) if ozon_market else None
    ozon_configured = bool(
        ozon_credential
        and (ozon_credential.market_key or "").strip()
        and (ozon_credential.client_id or "").strip()
    )
    wb_report = None
    ozon_report = None
    if selected_client:
        wb_report = (
            MarketSyncReport.objects.filter(
                agency=selected_client, marketplace="WB"
            ).order_by("-finished_at").first()
        )
        ozon_report = (
            MarketSyncReport.objects.filter(
                agency=selected_client, marketplace="OZON"
            ).order_by("-finished_at").first()
        )
    marketplaces = [
        {
            "name": "Wildberries",
            "status_class": "green" if wb_configured else "red",
            "status_text": "Настроено" if wb_configured else "Не настроено",
            "settings_url": f"/market-sync/wb/?client={selected_client.id}"
            if selected_client
            else None,
        },
        {
            "name": "Ozon",
            "status_class": "green" if ozon_configured else "red",
            "status_text": "Настроено" if ozon_configured else "Не настроено",
            "settings_url": f"/market-sync/ozon/?client={selected_client.id}"
            if selected_client
            else None,
        },
        {
            "name": "Яблоко",
            "status_class": "red",
            "status_text": "Не настроено",
            "settings_url": None,
        },
        {
            "name": "Яндекс Маркет",
            "status_class": "red",
            "status_text": "Не настроено",
            "settings_url": None,
        },
        {
            "name": "Lamoda",
            "status_class": "red",
            "status_text": "Не настроено",
            "settings_url": None,
        },
    ]
    return {
        "selected_client": selected_client,
        "agency_options": agency_options,
        "selected_client_id": str(getattr(selected_client, "id", "") or ""),
        "marketplaces": marketplaces,
        "wb_configured": wb_configured,
        "ozon_configured": ozon_configured,
        "wb_report": wb_report,
        "ozon_report": ozon_report,
    }


def _market_settings_context(*, selected_client, marketplace_name: str, form, market_missing: bool) -> dict:
    last_report = (
        MarketSyncReport.objects.filter(
            agency=selected_client, marketplace=marketplace_name
        ).order_by("-finished_at").first()
        if selected_client
        else None
    )
    return {
        "selected_client": selected_client,
        "form": form,
        "market_missing": market_missing,
        "last_report": last_report,
    }


def _next_market_credential_id() -> int:
    return (MarketCredential.objects.aggregate(max_id=Max("id"))["max_id"] or 0) + 1


def prepare_wb_settings_page(*, client_id):
    if not client_id:
        return redirect("/market-sync/"), None
    selected_client = get_object_or_404(Agency, pk=client_id)
    wb_market = Market.objects.filter(name__iexact="WB").first()
    if not wb_market:
        context = _market_settings_context(
            selected_client=selected_client,
            marketplace_name="WB",
            form=WBSettingsForm(),
            market_missing=True,
        )
        return None, context
    credential = MarketCredential.objects.filter(
        agency=selected_client, market=wb_market
    ).first()
    context = _market_settings_context(
        selected_client=selected_client,
        marketplace_name="WB",
        form=WBSettingsForm(instance=credential),
        market_missing=False,
    )
    return None, context


def submit_wb_settings(*, client_id, post_data):
    if not client_id:
        return redirect("/market-sync/"), None
    selected_client = get_object_or_404(Agency, pk=client_id)
    wb_market = Market.objects.filter(name__iexact="WB").first()
    if not wb_market:
        context = _market_settings_context(
            selected_client=selected_client,
            marketplace_name="WB",
            form=WBSettingsForm(post_data),
            market_missing=True,
        )
        return None, context
    credential = MarketCredential.objects.filter(
        agency=selected_client, market=wb_market
    ).first()
    form = WBSettingsForm(post_data, instance=credential)
    if form.is_valid():
        record = form.save(commit=False)
        record.agency = selected_client
        record.market = wb_market
        if record.pk is None:
            record.id = _next_market_credential_id()
        record.save()
        return redirect(f"/market-sync/?client={selected_client.id}"), None
    context = _market_settings_context(
        selected_client=selected_client,
        marketplace_name="WB",
        form=form,
        market_missing=False,
    )
    return None, context


def prepare_ozon_settings_page(*, client_id):
    if not client_id:
        return redirect("/market-sync/"), None
    selected_client = get_object_or_404(Agency, pk=client_id)
    ozon_market = Market.objects.filter(name__iexact="OZON").first()
    if not ozon_market:
        context = _market_settings_context(
            selected_client=selected_client,
            marketplace_name="OZON",
            form=OzonSettingsForm(),
            market_missing=True,
        )
        return None, context
    credential = MarketCredential.objects.filter(
        agency=selected_client, market=ozon_market
    ).first()
    context = _market_settings_context(
        selected_client=selected_client,
        marketplace_name="OZON",
        form=OzonSettingsForm(instance=credential),
        market_missing=False,
    )
    return None, context


def submit_ozon_settings(*, client_id, post_data):
    if not client_id:
        return redirect("/market-sync/"), None
    selected_client = get_object_or_404(Agency, pk=client_id)
    ozon_market = Market.objects.filter(name__iexact="OZON").first()
    if not ozon_market:
        context = _market_settings_context(
            selected_client=selected_client,
            marketplace_name="OZON",
            form=OzonSettingsForm(post_data),
            market_missing=True,
        )
        return None, context
    credential = MarketCredential.objects.filter(
        agency=selected_client, market=ozon_market
    ).first()
    form = OzonSettingsForm(post_data, instance=credential)
    if form.is_valid():
        record = form.save(commit=False)
        record.agency = selected_client
        record.market = ozon_market
        if record.pk is None:
            record.id = _next_market_credential_id()
        record.save()
        return redirect(f"/market-sync/?client={selected_client.id}"), None
    context = _market_settings_context(
        selected_client=selected_client,
        marketplace_name="OZON",
        form=form,
        market_missing=False,
    )
    return None, context


def build_report_detail_response(*, report_id: int):
    report = get_object_or_404(
        MarketSyncReport.objects.select_related("agency"), pk=report_id
    )
    return JsonResponse(
        {
            "id": report.id,
            "marketplace": report.marketplace,
            "status": report.status,
            "agency": {
                "id": report.agency_id,
                "name": report.agency.agn_name,
            },
            "started_at": report.started_at.isoformat() if report.started_at else None,
            "finished_at": report.finished_at.isoformat() if report.finished_at else None,
            "duration_sec": report.duration_sec,
            "processed": report.processed,
            "created": report.created,
            "updated": report.updated,
            "barcodes_created": report.barcodes_created,
            "errors": report.errors or [],
        }
    )
