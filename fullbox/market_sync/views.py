"""Thin compatibility facade for marketplace sync views and helpers."""

from . import web_ui as _web_ui


def __getattr__(name: str):
    return getattr(_web_ui, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_web_ui)))
