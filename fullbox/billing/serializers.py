from __future__ import annotations


def money(value):
    return str(value or "0.00")


def dt(value):
    return value.isoformat() if value else None


def d(value):
    return value.isoformat() if value else None


def agency_dict(agency):
    if not agency:
        return None
    return {
        "id": agency.id,
        "name": agency.agn_name or agency.short_name or str(agency),
        "short_name": agency.short_name or "",
        "inn": agency.inn or "",
        "kpp": agency.kpp or "",
    }


def employee_dict(employee):
    if not employee:
        return None
    return {"id": employee.id, "name": employee.full_name, "role": employee.role}


def own_company_dict(company):
    if not company:
        return None
    return {
        "id": company.id,
        "name": company.name,
        "short_name": company.short_name or company.name,
        "inn": company.inn or "",
        "tax_mode": getattr(company, "tax_mode", ""),
        "tax_mode_label": company.get_tax_mode_display() if hasattr(company, "get_tax_mode_display") else "",
        "vat_rate": getattr(company, "vat_rate", ""),
    }


def billing_service_dict(service):
    return {
        "id": service.id,
        "code": service.code,
        "name": service.name,
        "unit": service.unit,
        "vat_rate": service.vat_rate,
        "is_active": service.is_active,
    }


def standard_price_dict(price):
    return {
        "id": price.id,
        "service": billing_service_dict(price.service),
        "section_code": price.section_code,
        "section_name": price.section_name,
        "line_no": price.line_no,
        "name": price.name,
        "unit": price.unit,
        "base_price": money(price.base_price) if price.base_price is not None else None,
        "price_note": price.price_note,
        "is_active": price.is_active,
    }


def client_contract_dict(contract):
    return {
        "id": contract.id,
        "client": agency_dict(contract.client),
        "own_company": own_company_dict(contract.own_company),
        "pricing_mode": contract.pricing_mode,
        "pricing_mode_label": contract.get_pricing_mode_display(),
        "valid_from": d(contract.valid_from),
        "valid_to": d(contract.valid_to),
        "is_active": contract.is_active,
        "comment": contract.comment,
    }


def client_tariff_dict(tariff):
    return {
        "id": tariff.id,
        "client": agency_dict(tariff.client),
        "legal_entity": agency_dict(tariff.legal_entity),
        "service": billing_service_dict(tariff.service),
        "unit": tariff.unit,
        "tariff": str(tariff.tariff),
        "vat_rate": tariff.vat_rate,
        "valid_from": d(tariff.valid_from),
        "valid_to": d(tariff.valid_to),
        "is_active": tariff.is_active,
    }


def tariff_category_dict(category):
    return {
        "id": category.id,
        "code": category.code,
        "name": category.name,
        "description": category.description,
        "sort_order": category.sort_order,
        "is_active": category.is_active,
    }


def tariff_unit_dict(unit):
    if not unit:
        return None
    return {
        "id": unit.id,
        "code": unit.code,
        "name": unit.name,
        "short_name": unit.short_name,
        "is_active": unit.is_active,
    }


def client_tariff_item_dict(item):
    return {
        "id": item.id,
        "category": tariff_category_dict(item.category),
        "service": billing_service_dict(item.service),
        "service_name": item.service_name,
        "description": item.description,
        "unit": tariff_unit_dict(item.unit),
        "price": str(item.price),
        "minimum_amount": money(item.minimum_amount) if item.minimum_amount is not None else None,
        "minimum_quantity": str(item.minimum_quantity) if item.minimum_quantity is not None else None,
        "included_materials": item.included_materials,
        "conditions": item.conditions,
        "calculation_type": item.calculation_type,
        "calculation_type_label": item.get_calculation_type_display(),
        "coefficient": str(item.coefficient),
        "sort_order": item.sort_order,
        "is_active": item.is_active,
    }


def client_tariff_condition_dict(condition):
    return {
        "id": condition.id,
        "condition_type": condition.condition_type,
        "condition_type_label": condition.get_condition_type_display(),
        "name": condition.name,
        "description": condition.description,
        "value": condition.value,
        "unit": tariff_unit_dict(condition.unit),
        "valid_from": d(condition.valid_from),
        "valid_to": d(condition.valid_to),
        "sort_order": condition.sort_order,
        "is_active": condition.is_active,
    }


def client_tariff_version_dict(version, *, include_children=False):
    data = {
        "id": version.id,
        "client": agency_dict(version.client),
        "contract": client_contract_dict(version.contract) if version.contract else None,
        "name": version.name,
        "version_number": version.version_number,
        "status": version.status,
        "status_label": version.get_status_display(),
        "effective_status": version.effective_status,
        "valid_from": d(version.valid_from),
        "valid_to": d(version.valid_to),
        "vat_type": version.vat_type,
        "vat_type_label": version.get_vat_type_display(),
        "currency": version.currency,
        "contract_number": version.contract_number,
        "contract_date": d(version.contract_date),
        "additional_agreement_number": version.additional_agreement_number,
        "additional_agreement_date": d(version.additional_agreement_date),
        "manager": employee_dict(version.manager),
        "general_comment": version.general_comment,
        "approved_at": dt(version.approved_at),
        "created_at": dt(version.created_at),
        "updated_at": dt(version.updated_at),
    }
    if include_children:
        data["items"] = [client_tariff_item_dict(item) for item in version.items.select_related("category", "service", "unit").all()]
        data["conditions"] = [client_tariff_condition_dict(condition) for condition in version.conditions.select_related("unit").all()]
    return data


def billing_application_dict(application, *, include_children=False):
    data = {
        "id": application.id,
        "application_type": application.application_type,
        "application_type_label": application.get_application_type_display(),
        "application_id": application.application_id,
        "client": agency_dict(application.client),
        "legal_entity": agency_dict(application.legal_entity),
        "manager": employee_dict(application.manager),
        "warehouse_label": application.warehouse_label,
        "operational_status": application.operational_status,
        "operational_status_label": application.operational_status_label,
        "billing_status": application.billing_status,
        "billing_status_label": application.get_billing_status_display(),
        "created_at_source": dt(application.created_at_source),
        "operations_completed_at": dt(application.operations_completed_at),
        "financially_closed_at": dt(application.financially_closed_at),
        "is_operations_completed": application.is_operations_completed,
        "is_financially_closed": application.is_financially_closed,
        "charges_total": money(application.charges_total),
        "act_total": money(application.act_total),
        "invoice_total": money(application.invoice_total),
        "paid_total": money(application.paid_total),
        "debt_total": money(application.debt_total),
    }
    if include_children:
        data["charges"] = [application_charge_dict(charge) for charge in application.charges.select_related("service").all()]
        data["acts"] = [billing_act_dict(act) for act in application.acts.all()]
        data["invoices"] = [client_invoice_dict(invoice) for invoice in application.invoices.all()]
        from .warehouse_services import facts_payload, list_facts

        warehouse_facts = list_facts(
            client=application.client,
            order_type=application.application_type,
            order_id=str(application.application_id),
        )
        data["warehouse_service_facts"] = facts_payload(warehouse_facts)
        data["has_warehouse_service_facts"] = bool(warehouse_facts)
    return data


def application_charge_dict(charge):
    from .charge_status import NO_PRICE_MESSAGE, charge_manager_status, charge_missing_price, charge_source_label
    from .application_detail_ui import charge_tariff_display_label

    status_code, status_label, status_badge = charge_manager_status(charge)
    missing_price = charge_missing_price(charge)
    return {
        "id": charge.id,
        "application_id": charge.application_id,
        "service": {
            "id": charge.service_id,
            "name": charge.service_name_snapshot or charge.service.name,
            "code": charge.service.code,
        },
        "quantity": str(charge.quantity),
        "original_quantity": str(charge.original_quantity) if charge.original_quantity is not None else None,
        "unit": charge.unit,
        "tariff": str(charge.tariff),
        "tariff_price": str(charge.tariff_price) if charge.tariff_price is not None else None,
        "coefficient": str(charge.coefficient),
        "minimum_amount": money(charge.minimum_amount) if charge.minimum_amount is not None else None,
        "amount": money(charge.amount),
        "vat_rate": charge.vat_rate,
        "vat_amount": money(charge.vat_amount),
        "total_amount": money(charge.total_amount),
        "client_tariff_version_id": charge.client_tariff_version_id,
        "client_tariff_item_id": charge.client_tariff_item_id,
        "tariff_source_label": charge_tariff_display_label(charge),
        "tariff_basis": charge.tariff_basis or "",
        "is_manual_override": charge.is_manual_override,
        "override_reason": charge.override_reason or "",
        "source_type": charge.source_type,
        "source_id": charge.source_id,
        "source_label": charge_source_label(charge),
        "performed_at": dt(charge.performed_at),
        "billing_period": d(charge.billing_period),
        "is_confirmed": charge.is_confirmed,
        "is_included_in_act": charge.is_included_in_act,
        "is_included_in_invoice": charge.is_included_in_invoice,
        "is_disputed": charge.is_disputed,
        "is_excluded": bool(charge.is_excluded),
        "exclude_reason": charge.exclude_reason or "",
        "exclude_comment": charge.exclude_comment or "",
        "edit_version": int(charge.edit_version or 1),
        "qty_change_basis": charge.qty_change_basis or "",
        "qty_change_comment": charge.qty_change_comment or "",
        "previous_service_name": charge.previous_service_name or "",
        "service_changed_at": dt(charge.service_changed_at) if charge.service_changed_at else None,
        "service_changed": bool(charge.service_changed_at),
        "comment": charge.comment,
        "price_locked": True,
        "missing_price": missing_price,
        "missing_price_message": NO_PRICE_MESSAGE if missing_price else "",
        "manager_status": status_code,
        "manager_status_label": status_label,
        "manager_status_badge": status_badge,
    }


def application_charge_history_dict(entry):
    return {
        "id": entry.id,
        "charge_id": entry.charge_id,
        "change_type": entry.change_type,
        "change_type_label": entry.get_change_type_display(),
        "old_service_name": entry.old_service_name or "",
        "new_service_name": entry.new_service_name or "",
        "old_quantity": str(entry.old_quantity) if entry.old_quantity is not None else None,
        "new_quantity": str(entry.new_quantity) if entry.new_quantity is not None else None,
        "old_tariff": str(entry.old_tariff) if entry.old_tariff is not None else None,
        "new_tariff": str(entry.new_tariff) if entry.new_tariff is not None else None,
        "old_total": money(entry.old_total) if entry.old_total is not None else None,
        "new_total": money(entry.new_total) if entry.new_total is not None else None,
        "reason": entry.reason or "",
        "comment": entry.comment or "",
        "user": entry.user.get_username() if entry.user_id else "",
        "created_at": dt(entry.created_at),
    }


def billing_act_dict(act, *, include_lines=False):
    data = {
        "id": act.id,
        "application_id": act.application_id,
        "number": act.number,
        "version": act.version,
        "act_date": d(act.act_date),
        "status": act.status,
        "status_label": act.get_status_display(),
        "review_status": getattr(act, "review_status", "") or "",
        "review_status_label": act.get_review_status_display() if hasattr(act, "get_review_status_display") else "",
        "accountant_comment": getattr(act, "accountant_comment", "") or "",
        "subtotal": money(act.subtotal),
        "vat_amount": money(act.vat_amount),
        "total_amount": money(act.total_amount),
        "sent_at": dt(act.sent_at),
        "confirmed_at": dt(act.confirmed_at),
        "confirmed_amount": money(act.confirmed_amount),
        "client_comment": act.client_comment,
    }
    if include_lines:
        data["lines"] = [
            {
                "id": line.id,
                "charge_id": line.charge_id,
                "service_name": line.service_name,
                "quantity": str(line.quantity),
                "unit": line.unit,
                "tariff": str(line.tariff),
                "amount": money(line.amount),
                "vat_amount": money(line.vat_amount),
                "total_amount": money(line.total_amount),
            }
            for line in act.lines.all()
        ]
    return data


def client_invoice_dict(invoice):
    linked = list(invoice.get_linked_acts()) if hasattr(invoice, "get_linked_acts") else []
    return {
        "id": invoice.id,
        "application_id": invoice.application_id,
        "act_id": invoice.act_id,
        "act_ids": [a.id for a in linked] or ([invoice.act_id] if invoice.act_id else []),
        "acts_count": len(linked) or (1 if invoice.act_id else 0),
        "number": invoice.number,
        "invoice_type": invoice.invoice_type,
        "billing_period": d(invoice.billing_period),
        "invoice_date": d(invoice.invoice_date),
        "due_date": d(invoice.due_date),
        "status": invoice.status,
        "status_label": invoice.get_status_display(),
        "review_status": getattr(invoice, "review_status", "") or "",
        "review_status_label": invoice.get_review_status_display() if hasattr(invoice, "get_review_status_display") else "",
        "accountant_comment": getattr(invoice, "accountant_comment", "") or "",
        "client": agency_dict(invoice.client),
        "subtotal": money(invoice.subtotal),
        "vat_amount": money(invoice.vat_amount),
        "total_amount": money(invoice.total_amount),
        "paid_amount": money(invoice.paid_amount),
        "debt_amount": money(invoice.debt_amount),
        "sent_at": dt(invoice.sent_at),
        "paid_at": dt(invoice.paid_at),
        "external_system": invoice.external_system,
        "external_id": invoice.external_id,
        "external_url": invoice.external_url,
        "detail_url": f"/team-manager/billing/invoices/{invoice.id}/",
        "print_url": f"/team-manager/billing/invoices/{invoice.id}/print/",
    }
