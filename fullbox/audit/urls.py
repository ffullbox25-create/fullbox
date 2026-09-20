from django.urls import path

from .views import (
    AuditListView,
    ClientAuditListView,
    OrderAuditListView,
    StaffOveractionsListView,
    StockMoveAuditListView,
)

urlpatterns = [
    path('', AuditListView.as_view(), name='audit-list'),
    path('orders/', OrderAuditListView.as_view(), name='audit-orders'),
    path('moves/', StockMoveAuditListView.as_view(), name='audit-moves'),
    path('clients/', ClientAuditListView.as_view(), name='audit-clients'),
    path('overactions/', StaffOveractionsListView.as_view(), name='audit-overactions'),
]
