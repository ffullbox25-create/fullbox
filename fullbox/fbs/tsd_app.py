from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_GET


APK_CONTENT_TYPE = "application/vnd.android.package-archive"


def _apk_path() -> Path:
    return Path(settings.FULLBOX_TSD_APK_PATH)


def _apk_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _no_store(response):
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_GET
def tsd_app_version(request):
    path = _apk_path()
    if not path.is_file():
        return _no_store(
            JsonResponse(
                {
                    "available": False,
                    "error": "apk_not_found",
                },
                status=503,
            )
        )

    version_code = int(settings.FULLBOX_TSD_VERSION_CODE)
    download_url = request.build_absolute_uri(reverse("fbs:tsd_app_download"))
    separator = "&" if "?" in download_url else "?"
    released_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
    return _no_store(
        JsonResponse(
            {
                "available": True,
                "applicationId": settings.FULLBOX_TSD_APPLICATION_ID,
                "versionCode": version_code,
                "versionName": settings.FULLBOX_TSD_VERSION_NAME,
                "minimumAndroidSdk": 26,
                "required": bool(settings.FULLBOX_TSD_UPDATE_REQUIRED),
                "downloadUrl": f"{download_url}{separator}v={version_code}",
                "sha256": _apk_sha256(path),
                "size": path.stat().st_size,
                "releasedAt": released_at,
            }
        )
    )


@require_GET
def tsd_app_download(request):
    path = _apk_path()
    if not path.is_file():
        return _no_store(JsonResponse({"error": "apk_not_found"}, status=404))

    safe_version = re.sub(
        r"[^0-9A-Za-z._-]+", "-", str(settings.FULLBOX_TSD_VERSION_NAME)
    ).strip("-.")
    filename = f"Fullbox-TSD-{safe_version or 'latest'}.apk"
    response = FileResponse(path.open("rb"), content_type=APK_CONTENT_TYPE)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Content-Length"] = str(path.stat().st_size)
    response["ETag"] = f'"{_apk_sha256(path)}"'
    return _no_store(response)


__all__ = ["tsd_app_download", "tsd_app_version"]
