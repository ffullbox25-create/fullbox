"""
URL configuration for fullbox project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from django.urls import path
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include
from django.views.generic import RedirectView, TemplateView

from fullbox import views
from employees.qr_login import employee_qr_login, receiving_tsd_qr_login, tsd_qr_login
from billing.warehouse_api import warehouse_service_catalog, warehouse_service_facts
from client_cabinet.director_telegram_views import DirectorTelegramSettingsView

urlpatterns = [
    path('favicon.ico', views.favicon, name='favicon'),
    path('', TemplateView.as_view(template_name='landing.html'), name='home'),
    path('cases', TemplateView.as_view(template_name='landing_cases.html'), name='landing-cases'),
    path('policy', TemplateView.as_view(template_name='landing_policy.html'), name='landing-policy'),
    path('spasibo', TemplateView.as_view(template_name='landing_spasibo.html'), name='landing-spasibo'),
    path('kviz', TemplateView.as_view(template_name='landing_kviz.html'), name='landing-kviz'),
    path('error404', TemplateView.as_view(template_name='landing_error404.html'), name='landing-error404'),
    path('404.html', TemplateView.as_view(template_name='landing_error404.html'), name='landing-404-html'),
    path('page65541991.html', TemplateView.as_view(template_name='landing_page65541991.html'), name='landing-page65541991'),
    path('landing-submit/', views.landing_submit, name='landing-submit'),
    path('dev/', include('developer_cabinet.urls')),
    path('login-menu/', views.login_menu, name='login-menu'),
    path('dev-login/<str:username>/', views.dev_login, name='dev-login'),
    path('login/', views.sign_in, name='login'),
    path('login/qr/', employee_qr_login, name='employee-qr-login'),
    path('tsd/', tsd_qr_login, name='pfbs-tsd-login'),
    path('tsd/receiving/', receiving_tsd_qr_login, name='receiving-tsd-login'),
    path('logout/', views.sign_out, name='logout'),
    path(
        'cabinet/director/integrations/telegram/',
        DirectorTelegramSettingsView.as_view(),
        name='director-telegram-settings',
    ),
    path('cabinet/director/inventory/', views.director_inventory, name='director-inventory'),
    path(
        'cabinet/picker/',
        RedirectView.as_view(url='/fbs/tsd/picking/', permanent=False),
        name='picker-cabinet',
    ),
    path('cabinet/<str:role>/', views.role_cabinet, name='role-cabinet'),
    path('project-description/', views.project_description, name='project-description'),
    path('project-structure/', views.project_structure, name='project-structure'),
    path('project-structure/diagram/', views.project_block_diagram, name='project-block-diagram'),
    path('project-description/file/', views.project_description_file, name='project-description-file'),
    path('development-journal/', views.development_journal, name='development-journal'),
    path('development-journal/file/', views.development_journal_file, name='development-journal-file'),
    path('admin/audit/', RedirectView.as_view(pattern_name='admin:audit_auditjournal_changelist', permanent=False)),
    path('admin/', admin.site.urls),
    path('scanner-test/', views.scanner_test, name='scanner-test'),
    path('scanner-settings/', views.scanner_settings, name='scanner-settings'),
    path('scanner/ims-2290hd/', views.scanner_ims_2290hd, name='scanner-ims-2290hd'),
    path('orders/', include('orders.urls')),
    path('shipping/', include('shipping.urls')),
    path('logistics/', include('logistics.urls')),
    path('head-manager/goods/', include('warehouse_goods.urls')),
    path('head-manager/', include('head_manager.urls')),
    path('processing-head/', include('processing_head.urls')),
    path('processing-worker/', include('processing_worker.urls')),
    path('reachtruck/', include('reachtruck.urls')),
    path('otg-reachtruck/', include('otg_reachtruck.urls')),
    path('reachtruck-free/', include('reachtruck_free.urls')),
    path('reachtruck-box-move/', include('reachtruck_box_move.urls')),
    path('inventory/', include('inventory.urls')),
    path('reachtruck-inventory/', include('reachtruck_inventory.urls')),
    path('super-car/', include('super_car.urls')),
    path('client/', include('client_cabinet.urls')),
    path('client/', include('receiving_distribution.urls')),
    path('sku/', include('sku.urls')),
    path('audit/', include('audit.urls')),
    path('revisor/', include('revisor.urls')),
    path('todo/', include('todo.urls')),
    path('employees/', include('employees.urls')),
    path('hr/', include('hr.urls')),
    path('market-sync/', include('market_sync.urls')),
    path('fbs/', include('fbs.urls')),
    path('team-manager/', include('teammanager.urls')),
    # Алиас из ТЗ: /manager/billing/… → единый контур /team-manager/billing/…
    path('manager/billing/', views.manager_billing_alias, name='manager-billing-alias'),
    path('manager/billing/<path:subpath>', views.manager_billing_alias, name='manager-billing-alias-sub'),
    # Кладовщик: факты услуг без цен (доступно вне team-manager оболочки)
    path('api/warehouse-billing/catalog/', warehouse_service_catalog, name='warehouse-billing-catalog'),
    path('api/warehouse-billing/facts/', warehouse_service_facts, name='warehouse-billing-facts'),
    path('accountant/', include('accountant.urls')),
    path('sklad/', include('sklad.urls')),
    path('stockmap/', include('stockmap.urls')),
    path('labels/', include('labels.urls')),
    path('marking/', include('marking.urls')),
    path('agent/', include('agent.urls')),
    path('ai-support/', include('ai_support.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
