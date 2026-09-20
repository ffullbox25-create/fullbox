import hashlib
import logging
import time

from django.conf import settings
from django.contrib.auth import logout
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect


slow_request_logger = logging.getLogger("gunicorn.error")
login_security_logger = logging.getLogger("django.security.LoginRateLimit")


class LoginRateLimitMiddleware:
    """Limit repeated password failures across all application workers."""

    protected_paths = frozenset({"/login/", "/admin/login/"})

    def __init__(self, get_response):
        self.get_response = get_response

    @staticmethod
    def _client_ip(request) -> str:
        # Production nginx overwrites X-Real-IP and gunicorn is loopback-only.
        # Do not trust X-Forwarded-For supplied by the caller.
        return str(
            request.META.get("HTTP_X_REAL_IP")
            or request.META.get("REMOTE_ADDR")
            or ""
        ).strip()[:64]

    @staticmethod
    def _identifier(request) -> str:
        return str(request.POST.get("username") or "").strip().casefold()[:254]

    @staticmethod
    def _cache_key(scope: str, value: str) -> str:
        digest = hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()
        return f"security:login-failures:{scope}:{digest}"

    @staticmethod
    def _setting(name: str, default: int, *, minimum: int = 1) -> int:
        try:
            value = int(getattr(settings, name, default) or default)
        except (TypeError, ValueError):
            value = default
        return max(minimum, value)

    def _is_blocked(self, keys_and_limits) -> bool:
        try:
            counts = cache.get_many([key for key, _limit in keys_and_limits])
        except Exception:
            # Authentication must remain available if the rate-limit cache is
            # temporarily unavailable.  The failure is visible in gunicorn.
            login_security_logger.exception("login_rate_limit_cache_read_failed")
            return False
        for key, limit in keys_and_limits:
            try:
                if int(counts.get(key, 0) or 0) >= limit:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    @staticmethod
    def _increment(key: str, timeout: int) -> None:
        try:
            if cache.add(key, 1, timeout=timeout):
                return
            cache.incr(key)
        except ValueError:
            # The key may expire between add() and incr().
            cache.set(key, 1, timeout=timeout)
        except Exception:
            login_security_logger.exception("login_rate_limit_cache_write_failed")

    @staticmethod
    def _clear(key: str | None) -> None:
        if not key:
            return
        try:
            cache.delete(key)
        except Exception:
            login_security_logger.exception("login_rate_limit_cache_delete_failed")

    @staticmethod
    def _blocked_response(retry_after: int) -> HttpResponse:
        response = HttpResponse(
            "Слишком много попыток входа. Повторите позже.",
            status=429,
            content_type="text/plain; charset=utf-8",
        )
        response["Retry-After"] = str(retry_after)
        response["Cache-Control"] = "no-store, private"
        return response

    def __call__(self, request):
        if request.method != "POST" or request.path_info not in self.protected_paths:
            return self.get_response(request)

        was_authenticated = bool(
            getattr(getattr(request, "user", None), "is_authenticated", False)
        )
        window = self._setting("LOGIN_RATE_LIMIT_WINDOW_SECONDS", 600, minimum=60)
        ip_limit = self._setting("LOGIN_RATE_LIMIT_IP_FAILURES", 30)
        identifier_limit = self._setting("LOGIN_RATE_LIMIT_USERNAME_FAILURES", 10)
        ip_address = self._client_ip(request)
        identifier = self._identifier(request)
        ip_key = self._cache_key("ip", ip_address) if ip_address else None
        identifier_key = self._cache_key("username", identifier) if identifier else None
        keys_and_limits = []
        if ip_key:
            keys_and_limits.append((ip_key, ip_limit))
        if identifier_key:
            keys_and_limits.append((identifier_key, identifier_limit))

        if keys_and_limits and self._is_blocked(keys_and_limits):
            return self._blocked_response(window)

        response = self.get_response(request)
        status = int(getattr(response, "status_code", 0) or 0)
        authenticated_user = getattr(request, "user", None)
        login_succeeded = bool(
            300 <= status < 400
            and not was_authenticated
            and getattr(authenticated_user, "is_authenticated", False)
        )
        if login_succeeded:
            # A successful password login redirects.  Clear only the account
            # counter; failures from other accounts on the same IP remain.
            self._clear(identifier_key)
            authenticated_identifier = str(
                getattr(authenticated_user, "username", "") or ""
            ).strip().casefold()[:254]
            if authenticated_identifier and authenticated_identifier != identifier:
                self._clear(self._cache_key("username", authenticated_identifier))
        elif status in {200, 401}:
            if ip_key:
                self._increment(ip_key, window)
            if identifier_key:
                self._increment(identifier_key, window)
        return response


class AuthenticatedIdleTimeoutMiddleware:
    """End every authenticated session after the configured idle period."""

    session_key = "_fullbox_last_activity_at"
    passive_activity_paths = frozenset({"/fbs/desktop-print/next/"})
    pfbs_tsd_path_prefix = "/fbs/tsd/"
    pfbs_tsd_login_url = "/tsd/"

    def __init__(self, get_response):
        self.get_response = get_response

    def _is_passive_background_request(self, request) -> bool:
        return (
            request.method == "GET"
            and request.path_info in self.passive_activity_paths
            and str(request.headers.get("X-Fullbox-Desktop") or "").strip() == "1"
        )

    def __call__(self, request):
        user = getattr(request, "user", None)
        if not getattr(user, "is_authenticated", False):
            if request.path_info.startswith(self.pfbs_tsd_path_prefix):
                return redirect(self.pfbs_tsd_login_url)
            return self.get_response(request)

        now = int(time.time())
        timeout_seconds = max(
            60,
            int(getattr(settings, "AUTHENTICATED_IDLE_TIMEOUT_SECONDS", 1800) or 1800),
        )
        last_activity = request.session.get(self.session_key)
        try:
            last_activity = int(last_activity) if last_activity is not None else None
        except (TypeError, ValueError):
            last_activity = None

        if last_activity is not None and now - last_activity > timeout_seconds:
            return_to_tsd = request.path_info.startswith(self.pfbs_tsd_path_prefix)
            passive_background_request = self._is_passive_background_request(request)
            logout(request)
            if passive_background_request:
                response = JsonResponse(
                    {
                        "ok": False,
                        "error": "Сессия Fullbox Desktop завершена. Войдите в систему повторно.",
                        "reauth_required": True,
                        "retry_after_ms": 60000,
                    },
                    status=401,
                )
                response["Retry-After"] = "60"
                return response
            if return_to_tsd:
                return redirect(f"{self.pfbs_tsd_login_url}?session_expired=1")
            return redirect(f"{settings.LOGIN_URL}?session_expired=1")

        # Desktop asks for the next print job in the background.  The request
        # must still obey idle expiry, but it is not user activity and must not
        # rewrite the same django_session row every second from every window.
        if last_activity is not None and self._is_passive_background_request(request):
            return self.get_response(request)

        request.session[self.session_key] = now
        # Keep the backend session alive long enough for this middleware to
        # detect idle expiry itself.  If Django expires the session at exactly
        # the same boundary, AuthenticationMiddleware sees an anonymous user
        # first and role guards can answer with a confusing 403 instead of the
        # session-expired login redirect.
        request.session.set_expiry(None)
        return self.get_response(request)


class SlowRequestLoggingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started_at = time.perf_counter()
        response = None
        try:
            response = self.get_response(request)
            return response
        finally:
            elapsed = time.perf_counter() - started_at
            threshold = float(getattr(settings, "SLOW_REQUEST_LOG_SECONDS", 1.5) or 0)
            if threshold > 0 and elapsed >= threshold:
                user = getattr(request, "user", None)
                username = ""
                if getattr(user, "is_authenticated", False):
                    username = getattr(user, "username", "") or str(user)
                forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
                ip_address = forwarded.split(",", 1)[0].strip() if forwarded else request.META.get("REMOTE_ADDR", "")
                slow_request_logger.warning(
                    "slow_request method=%s path=%s status=%s duration_ms=%s user=%s ip=%s",
                    request.method,
                    request.get_full_path(),
                    getattr(response, "status_code", "error"),
                    int(elapsed * 1000),
                    username or "-",
                    ip_address or "-",
                )
