from django.template.loader import render_to_string

from .services import user_can_use_ai_support


ANNOUNCEMENT_ID = "ai-support-launch-2026-09"
EXCLUDED_PATH_PREFIXES = (
    "/ai-support/",
    "/login/",
    "/logout/",
    "/static/",
    "/media/",
)


class EmployeeAiSupportAnnouncementMiddleware:
    """Add a dismissible launch notice to employee HTML pages only."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not self._should_inject(request, response):
            return response

        snippet = render_to_string(
            "ai_support/_launch_announcement.html",
            {"announcement_id": ANNOUNCEMENT_ID},
            request=request,
        )
        charset = response.charset or "utf-8"
        content = response.content.decode(charset)
        closing_body = content.lower().rfind("</body>")
        if closing_body < 0:
            return response
        response.content = content[:closing_body] + snippet + content[closing_body:]
        if response.has_header("Content-Length"):
            response["Content-Length"] = str(len(response.content))
        return response

    @staticmethod
    def _should_inject(request, response) -> bool:
        if request.path.startswith(EXCLUDED_PATH_PREFIXES):
            return False
        if response.status_code != 200 or getattr(response, "streaming", False):
            return False
        if "text/html" not in str(response.get("Content-Type") or "").lower():
            return False
        if response.get("Content-Encoding"):
            return False
        return user_can_use_ai_support(request)
