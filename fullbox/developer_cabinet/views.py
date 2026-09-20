import logging

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import render

from employees.access import get_request_role

from .chz_import import (
    IMPORT_MODE_CHZ,
    IMPORT_MODE_NO_CHZ,
    execute_chz_import,
    normalize_import_mode,
    validate_chz_import_file,
)


logger = logging.getLogger(__name__)


def _is_developer_request(request):
    role = get_request_role(request)
    return role == "developer" or request.user.username == "dev" or request.user.is_superuser


@login_required
def dashboard(request):
    if not _is_developer_request(request):
        return HttpResponseForbidden("Доступ запрещен")
    return render(request, "developer_cabinet/dashboard.html")


@login_required
def chz_import(request):
    if not _is_developer_request(request):
        return HttpResponseForbidden("Доступ запрещен")

    context = {
        "import_mode": normalize_import_mode(
            request.POST.get("import_mode") if request.method == "POST" else request.GET.get("import_mode")
        ),
        "import_modes": [
            {"value": IMPORT_MODE_CHZ, "label": "Товар с ЧЗ"},
            {"value": IMPORT_MODE_NO_CHZ, "label": "Остатки без ЧЗ"},
        ],
    }
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "validate":
            upload = request.FILES.get("file")
            if upload:
                try:
                    context["report"] = validate_chz_import_file(
                        upload,
                        user=request.user,
                        import_mode=context["import_mode"],
                    )
                except Exception as exc:
                    logger.exception(
                        "developer_chz_validation_failed file=%s user=%s",
                        getattr(upload, "name", ""),
                        request.user.username,
                    )
                    context["report"] = {
                        "ok": False,
                        "import_mode": context["import_mode"],
                        "summary": {"errors_count": 1, "warnings_count": 0},
                        "errors": [
                            {
                                "row": "",
                                "field": "file",
                                "message": _format_import_exception(exc),
                            }
                        ],
                        "warnings": [],
                        "preview_rows": [],
                    }
            else:
                context["report"] = {
                    "ok": False,
                    "import_mode": context["import_mode"],
                    "summary": {"errors_count": 1, "warnings_count": 0},
                    "errors": [{"row": "", "field": "file", "message": "Выберите Excel-файл."}],
                    "warnings": [],
                    "preview_rows": [],
                }
        elif action == "import":
            try:
                context["import_result"] = execute_chz_import(request.POST.get("token", ""), user=request.user)
            except Exception as exc:
                logger.exception(
                    "developer_chz_import_failed token=%s user=%s",
                    request.POST.get("token", ""),
                    request.user.username,
                )
                context["import_result"] = {
                    "ok": False,
                    "batch_id": "",
                    "summary": {},
                    "errors": [
                        {
                            "row": "",
                            "field": "Импорт",
                            "message": _format_import_exception(exc),
                        }
                    ],
                }

    return render(request, "developer_cabinet/chz_import.html", context)


def _format_import_exception(exc: Exception) -> str:
    reason = " ".join(str(exc).split())
    if "invalid literal for int()" in reason:
        return (
            "Импорт не выполнен: в поле «Место» есть буквенная координата. "
            "Укажите существующий код места или числовой адрес в формате "
            "OS·ряд·секция·ярус·ячейка. Данные не записаны."
        )
    if reason:
        return f"Импорт не выполнен, данные не записаны. Причина: {reason[:500]}"
    return "Импорт не выполнен из-за внутренней ошибки. Данные не записаны."
