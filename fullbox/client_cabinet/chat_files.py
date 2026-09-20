"""Вложения чата: MIME/размер, сериализация, без складской логики."""

from __future__ import annotations

import mimetypes
import os
from typing import Any

# До 15 МБ на файл, до 10 файлов за сообщение.
MAX_CHAT_FILE_BYTES = 15 * 1024 * 1024
MAX_CHAT_FILES_PER_MESSAGE = 10

ALLOWED_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".txt",
    ".csv",
    ".rtf",
    ".zip",
}

ALLOWED_MIME_PREFIXES = (
    "image/",
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument",
    "application/vnd.ms-excel",
    "text/plain",
    "text/csv",
    "application/rtf",
    "application/zip",
    "application/x-zip-compressed",
)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


class ChatFileError(ValueError):
    """Ошибка валидации вложения чата (текст для пользователя)."""


def _ext(name: str) -> str:
    return os.path.splitext(str(name or ""))[1].lower()


def guess_content_type(filename: str, uploaded=None) -> str:
    ctype = ""
    if uploaded is not None:
        ctype = str(getattr(uploaded, "content_type", "") or "").strip().lower()
    if not ctype or ctype == "application/octet-stream":
        guessed, _ = mimetypes.guess_type(str(filename or ""))
        ctype = (guessed or "").lower()
    return ctype or "application/octet-stream"


def is_image_filename(filename: str, content_type: str = "") -> bool:
    if _ext(filename) in IMAGE_EXTENSIONS:
        return True
    return str(content_type or "").lower().startswith("image/")


def format_size(num_bytes: int | None) -> str:
    n = int(num_bytes or 0)
    if n < 1024:
        return f"{n} Б"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} КБ"
    return f"{n / (1024 * 1024):.1f} МБ"


def validate_chat_uploads(files) -> list:
    """Проверяет список UploadedFile. Пустой список допустим. Иначе — ChatFileError."""
    items = [f for f in (files or []) if f and getattr(f, "name", "")]
    if not items:
        return []
    if len(items) > MAX_CHAT_FILES_PER_MESSAGE:
        raise ChatFileError(f"Можно прикрепить не больше {MAX_CHAT_FILES_PER_MESSAGE} файлов")
    cleaned = []
    for uploaded in items:
        name = str(getattr(uploaded, "name", "") or "file")
        ext = _ext(name)
        if ext not in ALLOWED_EXTENSIONS:
            raise ChatFileError(
                f"Тип файла «{ext or 'без расширения'}» не поддерживается. "
                "Допустимы изображения, PDF, Word, Excel, TXT, ZIP."
            )
        size = int(getattr(uploaded, "size", 0) or 0)
        if size <= 0:
            raise ChatFileError(f"Файл «{os.path.basename(name)}» пустой")
        if size > MAX_CHAT_FILE_BYTES:
            raise ChatFileError(
                f"Файл «{os.path.basename(name)}» больше {format_size(MAX_CHAT_FILE_BYTES)}"
            )
        ctype = guess_content_type(name, uploaded)
        if not any(ctype.startswith(p) or ctype == p for p in ALLOWED_MIME_PREFIXES):
            # Некоторые браузеры отдают octet-stream — доверяем расширению.
            if ctype not in {"application/octet-stream", ""}:
                raise ChatFileError(f"MIME «{ctype}» для «{os.path.basename(name)}» не допускается")
        cleaned.append(uploaded)
    return cleaned


def serialize_chat_attachment(attachment, *, agency_id=None) -> dict[str, Any]:
    filename = attachment.filename
    ctype = guess_content_type(filename, getattr(attachment, "file", None))
    try:
        size = int(attachment.file.size)
    except Exception:
        size = 0
    suffix = f"?client={agency_id}" if agency_id else ""
    return {
        "id": attachment.id,
        "filename": filename,
        "url": f"/client/api/v1/chat/attachments/{attachment.id}/{suffix}",
        "content_type": ctype,
        "size": size,
        "size_label": format_size(size),
        "is_image": is_image_filename(filename, ctype),
    }


def accept_attr() -> str:
    """Значение для input accept=."""
    return "image/*,.pdf,.doc,.docx,.xls,.xlsx,.txt,.csv,.rtf,.zip"
