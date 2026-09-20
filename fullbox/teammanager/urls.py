from django.urls import include, path

from .chats_views import (
    TeamManagerChatAIFeedbackApi,
    TeamManagerChatAISuggestApi,
    TeamManagerChatCreateTaskApi,
    TeamManagerChatMarkAllReadApi,
    TeamManagerChatMentionCandidatesApi,
    TeamManagerChatMessageDeleteApi,
    TeamManagerChatMessageEditApi,
    TeamManagerChatMessagesApi,
    TeamManagerChatPinApi,
    TeamManagerChatResolveApi,
    TeamManagerChatTelegramView,
    TeamManagerChatsSlaView,
    TeamManagerChatsView,
)
from .other_requests_views import (
    TeamManagerOtherRequestDetailView,
    TeamManagerOtherRequestsListView,
)
from .reports_views import (
    TeamManagerReportCatalogApi,
    TeamManagerReportDetailView,
    TeamManagerReportExportView,
    TeamManagerReportFavoriteToggleView,
    TeamManagerReportMetadataApi,
    TeamManagerProductArticlesApi,
    TeamManagerReportSectionApi,
    TeamManagerReportSectionView,
    TeamManagerReportsHistoryView,
    TeamManagerReportsView,
    TeamManagerSavedReportCreateView,
    TeamManagerSavedReportsView,
)
from .fbs_movements import (
    TeamManagerFbsMovementDetailView,
    TeamManagerFbsMovementsView,
)
from .fbs_clients import (
    TeamManagerFbsClientsView,
    TeamManagerFbsClientSettingsUpdateView,
    TeamManagerFbsClientSettingsView,
)
from .views import (
    TeamManagerClientsView,
    TeamManagerDashboard,
    TeamManagerInventoryView,
    TeamManagerKnowledgeArticleView,
    TeamManagerKnowledgeDocumentView,
    TeamManagerKnowledgeView,
    TeamManagerLogisticsReferenceView,
    TeamManagerNotificationReadApi,
    TeamManagerOrdersView,
    TeamManagerSettingsView,
    TeamManagerTaskClaimApi,
    TeamManagerTaskDeadlineApi,
    TeamManagerWarehouseDeadlineApi,
    TeamManagerTaskEventsApi,
    TeamManagerTaskTransferApi,
)
from .queue_views import TeamManagerQueueView

urlpatterns = [
    path("", TeamManagerDashboard.as_view(), name="team-manager-dashboard"),
    path("queue/", TeamManagerQueueView.as_view(), name="team-manager-queue"),
    path("api/task-events/", TeamManagerTaskEventsApi.as_view(), name="team-manager-task-events"),
    path("api/notifications/read/", TeamManagerNotificationReadApi.as_view(), name="team-manager-notification-read"),
    path("api/tasks/<int:pk>/claim/", TeamManagerTaskClaimApi.as_view(), name="team-manager-task-claim"),
    path(
        "api/tasks/<int:pk>/deadline/",
        TeamManagerTaskDeadlineApi.as_view(),
        name="team-manager-task-deadline",
    ),
    path(
        "api/tasks/<int:pk>/warehouse-deadline/",
        TeamManagerWarehouseDeadlineApi.as_view(),
        name="team-manager-warehouse-deadline",
    ),
    path("api/tasks/<int:pk>/transfer/", TeamManagerTaskTransferApi.as_view(), name="team-manager-task-transfer"),
    path("billing/", include("billing.urls")),
    path("chats/", TeamManagerChatsView.as_view(), name="team-manager-chats"),
    path("chats/sla/", TeamManagerChatsSlaView.as_view(), name="team-manager-chats-sla"),
    path("chats/telegram/", TeamManagerChatTelegramView.as_view(), name="team-manager-chats-telegram"),
    path(
        "api/chats/<int:thread_id>/messages/",
        TeamManagerChatMessagesApi.as_view(),
        name="team-manager-chat-messages",
    ),
    path(
        "api/chats/<int:thread_id>/resolve/",
        TeamManagerChatResolveApi.as_view(),
        name="team-manager-chat-resolve",
    ),
    path(
        "api/chats/messages/<int:message_id>/create-task/",
        TeamManagerChatCreateTaskApi.as_view(),
        name="team-manager-chat-create-task",
    ),
    path(
        "api/chats/messages/<int:message_id>/edit/",
        TeamManagerChatMessageEditApi.as_view(),
        name="team-manager-chat-message-edit",
    ),
    path(
        "api/chats/messages/<int:message_id>/delete/",
        TeamManagerChatMessageDeleteApi.as_view(),
        name="team-manager-chat-message-delete",
    ),
    path(
        "api/chats/<int:thread_id>/pin/",
        TeamManagerChatPinApi.as_view(),
        name="team-manager-chat-pin",
    ),
    path(
        "api/chats/mention-candidates/",
        TeamManagerChatMentionCandidatesApi.as_view(),
        name="team-manager-chat-mention-candidates",
    ),
    path(
        "api/chats/mark-all-read/",
        TeamManagerChatMarkAllReadApi.as_view(),
        name="team-manager-chat-mark-all-read",
    ),
    path(
        "api/chats/<int:thread_id>/ai/suggest/",
        TeamManagerChatAISuggestApi.as_view(),
        name="team-manager-chat-ai-suggest",
    ),
    path(
        "api/chats/ai/suggestions/<int:suggestion_id>/feedback/",
        TeamManagerChatAIFeedbackApi.as_view(),
        name="team-manager-chat-ai-feedback",
    ),
    path("orders/", TeamManagerOrdersView.as_view(), name="team-manager-orders"),
    path("fbs/movements/", TeamManagerFbsMovementsView.as_view(), name="team-manager-fbs-movements"),
    path(
        "fbs/movements/<int:request_id>/",
        TeamManagerFbsMovementDetailView.as_view(),
        name="team-manager-fbs-movement-detail",
    ),
    path("fbs/clients/", TeamManagerFbsClientsView.as_view(), name="team-manager-fbs-clients"),
    path(
        "fbs/clients/<int:agency_id>/settings/",
        TeamManagerFbsClientSettingsView.as_view(),
        name="team-manager-fbs-client-settings",
    ),
    path(
        "fbs/clients/<int:agency_id>/settings/update/",
        TeamManagerFbsClientSettingsUpdateView.as_view(),
        name="team-manager-fbs-client-settings-update",
    ),
    path("reports/", TeamManagerReportsView.as_view(), name="team-manager-reports"),
    path("reports/history/", TeamManagerReportsHistoryView.as_view(), name="team-manager-reports-history"),
    path("reports/saved/", TeamManagerSavedReportsView.as_view(), name="team-manager-saved-reports"),
    path(
        "api/reports/product-articles/",
        TeamManagerProductArticlesApi.as_view(),
        name="team-manager-product-articles-api",
    ),
    path("reports/<slug:section>/", TeamManagerReportSectionView.as_view(), name="team-manager-report-section"),
    path(
        "reports/<slug:section>/<slug:report_code>/",
        TeamManagerReportDetailView.as_view(),
        name="team-manager-report-detail",
    ),
    path(
        "reports/<slug:section>/<slug:report_code>/export/<str:export_format>/",
        TeamManagerReportExportView.as_view(),
        name="team-manager-report-export",
    ),
    path(
        "reports/<slug:section>/<slug:report_code>/favorite/",
        TeamManagerReportFavoriteToggleView.as_view(),
        name="team-manager-report-favorite",
    ),
    path(
        "reports/<slug:section>/<slug:report_code>/save/",
        TeamManagerSavedReportCreateView.as_view(),
        name="team-manager-report-save",
    ),
    path("api/reports/catalog/", TeamManagerReportCatalogApi.as_view(), name="team-manager-report-catalog-api"),
    path("api/reports/catalog/<slug:section>/", TeamManagerReportSectionApi.as_view(), name="team-manager-report-section-api"),
    path(
        "api/reports/<slug:section>/<slug:report_code>/metadata/",
        TeamManagerReportMetadataApi.as_view(),
        name="team-manager-report-metadata-api",
    ),
    path("inventory/", TeamManagerInventoryView.as_view(), name="team-manager-inventory"),
    path("other-requests/", TeamManagerOtherRequestsListView.as_view(), name="team-manager-other-requests"),
    path(
        "other-requests/<str:number>/",
        TeamManagerOtherRequestDetailView.as_view(),
        name="team-manager-other-request-detail",
    ),
    path("clients/", TeamManagerClientsView.as_view(), name="team-manager-clients"),
    path("references/", TeamManagerLogisticsReferenceView.as_view(), name="team-manager-references"),
    path(
        "references/logistics/",
        TeamManagerLogisticsReferenceView.as_view(),
        name="team-manager-reference-logistics",
    ),
    path("knowledge/", TeamManagerKnowledgeView.as_view(), name="team-manager-knowledge"),
    path(
        "knowledge/documents/<slug:document>/",
        TeamManagerKnowledgeDocumentView.as_view(),
        name="team-manager-knowledge-document",
    ),
    path(
        "knowledge/<slug:slug>/",
        TeamManagerKnowledgeArticleView.as_view(),
        name="team-manager-knowledge-article",
    ),
    path("settings/", TeamManagerSettingsView.as_view(), name="team-manager-settings"),
]
