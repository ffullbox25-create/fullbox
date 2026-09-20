from django.dispatch import Signal


movement_warehouse_confirmed = Signal()
movement_completed = Signal()
order_billing_ready = Signal()
storage_usage_captured = Signal()
delivery_accepted = Signal()
