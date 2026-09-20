from django import template
from reachtruck.models import MoveRequest
from processing_reachtruck.quantity_queue import waiting_quantity

register = template.Library()


@register.simple_tag
def obr_queue_message(order_number):
    if not order_number:
        return ""
    for request in MoveRequest.objects.filter(context_type="processing", context_id=str(order_number),
            destination_zone="OBR").exclude(status__in=["canceled", "done"]).order_by("created_at"):
        pending = waiting_quantity(request)
        if pending:
            planned = sum(row.qty_planned for row in request.items.all())
            return f"Подача товара принята: спланировано {planned} шт., в очереди {pending} шт. Остаток будет запланирован после освобождения подходящих коробов."
    return ""
