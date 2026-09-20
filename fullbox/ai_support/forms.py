import json
from urllib.parse import urlsplit, urlunsplit

from django import forms

from .models import Incident, IncidentMessage


def sanitize_source_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = "/"
    return urlunsplit(("", "", path[:800], parsed.query[:180], ""))[:1000]


class IncidentCreateForm(forms.ModelForm):
    context_json = forms.CharField(required=False, widget=forms.HiddenInput())

    class Meta:
        model = Incident
        fields = (
            "zone",
            "object_type",
            "object_id",
            "description",
            "expected_result",
            "reproducible",
            "severity",
            "source_url",
            "page_title",
        )
        widgets = {
            "description": forms.Textarea(attrs={"rows": 5, "placeholder": "Опишите, что вы делали и что произошло"}),
            "expected_result": forms.Textarea(attrs={"rows": 3, "placeholder": "Какой результат вы ожидали"}),
            "source_url": forms.HiddenInput(),
            "page_title": forms.HiddenInput(),
        }

    def clean_source_url(self):
        return sanitize_source_url(self.cleaned_data.get("source_url"))

    def clean_context_json(self):
        raw = str(self.cleaned_data.get("context_json") or "").strip()
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(payload, dict):
            return {}
        allowed = {"screen", "viewport", "timezone", "online"}
        return {
            str(key)[:40]: value
            for key, value in payload.items()
            if key in allowed and isinstance(value, (str, int, float, bool, list, dict, type(None)))
        }


class IncidentMessageForm(forms.ModelForm):
    class Meta:
        model = IncidentMessage
        fields = ("body",)
        widgets = {
            "body": forms.Textarea(
                attrs={"rows": 3, "placeholder": "Добавьте уточнение или ответьте ИИ-программисту"}
            )
        }
