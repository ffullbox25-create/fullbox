from __future__ import annotations

import json
import time
from typing import Any

from sku.models import MarketCredential

from .sync_services import run_ozon_sync_request, run_wb_sync_request


def marketplace_sync_code(value: str | None) -> str:
    text = str(value or "").strip().upper()
    if text in {"WB", "WILDBERRIES", "WILDBERRIES (WB)"}:
        return "WB"
    if text in {"OZON", "O-ZON"}:
        return "OZON"
    return ""


def configured_global_sync_targets() -> list[dict]:
    targets: dict[int, dict] = {}
    credentials = (
        MarketCredential.objects.select_related("agency", "market")
        .filter(agency__archived=False)
        .order_by("agency__agn_name", "agency_id", "market__name")
    )
    for credential in credentials:
        agency = getattr(credential, "agency", None)
        if not agency:
            continue
        code = marketplace_sync_code(getattr(getattr(credential, "market", None), "name", ""))
        if code == "WB":
            if not (credential.market_key or "").strip():
                continue
        elif code == "OZON":
            if not (credential.market_key or "").strip() or not (credential.client_id or "").strip():
                continue
        else:
            continue
        target = targets.setdefault(
            agency.id,
            {
                "agency": agency,
                "markets": [],
            },
        )
        if code not in target["markets"]:
            target["markets"].append(code)
    return list(targets.values())


def _decode_sync_response(response) -> dict:
    try:
        payload = json.loads(response.content.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    return payload if isinstance(payload, dict) else {}


def run_sync_all_clients(*, pause_seconds: float = 0.0) -> dict[str, Any]:
    """Sync marketplace cards (WB/Ozon) for every configured client."""
    targets = configured_global_sync_targets()
    summary: dict[str, Any] = {
        "clients_total": len(targets),
        "clients_processed": 0,
        "clients_with_errors": 0,
        "processed": 0,
        "created": 0,
        "updated": 0,
        "barcodes_created": 0,
        "errors": [],
        "results": [],
    }
    if not targets:
        summary["ok"] = False
        summary["partial"] = False
        summary["errors"] = ["Нет клиентов с настроенными маркетплейсами WB или Ozon."]
        return summary

    for index, target in enumerate(targets):
        if pause_seconds > 0 and index > 0:
            time.sleep(pause_seconds)
        agency = target["agency"]
        agency_name = str(getattr(agency, "agn_name", "") or getattr(agency, "fio_agn", "") or agency.id)
        client_result = {
            "agency_id": agency.id,
            "agency_name": agency_name,
            "markets": [],
            "ok": True,
        }
        for market_code in target["markets"]:
            payload = json.dumps({"client": agency.id}).encode("utf-8")
            response = run_wb_sync_request(body=payload) if market_code == "WB" else run_ozon_sync_request(body=payload)
            data = _decode_sync_response(response)
            market_errors = [str(item) for item in (data.get("errors") or []) if str(item).strip()]
            market_ok = bool(response.status_code < 400 and data.get("ok"))
            client_result["markets"].append(
                {
                    "marketplace": market_code,
                    "ok": market_ok,
                    "processed": int(data.get("processed") or 0),
                    "created": int(data.get("created") or 0),
                    "updated": int(data.get("updated") or 0),
                    "barcodes_created": int(data.get("barcodes_created") or 0),
                    "errors": market_errors,
                }
            )
            summary["processed"] += int(data.get("processed") or 0)
            summary["created"] += int(data.get("created") or 0)
            summary["updated"] += int(data.get("updated") or 0)
            summary["barcodes_created"] += int(data.get("barcodes_created") or 0)
            if market_errors:
                client_result["ok"] = False
                summary["errors"].extend(
                    [f"{agency_name} / {market_code}: {error}" for error in market_errors]
                )
            elif not market_ok:
                client_result["ok"] = False
        summary["clients_processed"] += 1
        if not client_result["ok"]:
            summary["clients_with_errors"] += 1
        summary["results"].append(client_result)

    summary["ok"] = summary["clients_with_errors"] == 0
    summary["partial"] = not summary["ok"]
    return summary
