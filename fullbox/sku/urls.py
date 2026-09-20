from django.urls import path

from .views import (
    SKUListView,
    SKUCreateView,
    SKUUpdateView,
    SKUDuplicateView,
    suggest_sku,
    clone_sku,
    mark_deleted,
    sync_marketplaces,
)

urlpatterns = [
    path('', SKUListView.as_view(), name='sku-list'),
    path('new/', SKUCreateView.as_view(), name='sku-create'),
    path('<int:pk>/edit/', SKUUpdateView.as_view(), name='sku-edit'),
    path('<int:pk>/duplicate/', SKUDuplicateView.as_view(), name='sku-duplicate'),
    path('<int:pk>/sync-marketplaces/', sync_marketplaces, name='sku-sync-marketplaces'),
    path('<int:pk>/delete/', mark_deleted, name='sku-delete'),
    path('suggest/', suggest_sku, name='sku-suggest'),
    path('clone/<int:pk>/', clone_sku, name='sku-clone'),
]
