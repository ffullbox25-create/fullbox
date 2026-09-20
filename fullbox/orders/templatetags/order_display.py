from django import template

from audit.models import get_order_external_number


register = template.Library()


@register.filter
def display_order_number(order_id, order_type):
    return get_order_external_number(order_type, order_id)
