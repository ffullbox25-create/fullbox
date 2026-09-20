import re

from django import forms

from sku.models import MarketCredential


class WBSettingsForm(forms.ModelForm):
    def clean_market_key(self):
        token = (self.cleaned_data.get("market_key") or "").strip()
        if not token:
            raise forms.ValidationError("Укажите токен WB.")
        return token

    class Meta:
        model = MarketCredential
        fields = ["market_key"]
        labels = {
            "market_key": "API токен WB",
        }
        widgets = {
            "market_key": forms.Textarea(
                attrs={
                    "rows": 3,
                    "class": "input",
                    "autocomplete": "off",
                    "placeholder": "Вставьте токен Wildberries",
                }
            ),
        }


class OzonSettingsForm(forms.ModelForm):
    def clean_client_id(self):
        client_id = (self.cleaned_data.get("client_id") or "").strip()
        if not client_id:
            raise forms.ValidationError("Укажите Client ID Ozon.")
        match = re.fullmatch(r"(\d+)(?:\.0+)?", client_id)
        if not match:
            raise forms.ValidationError("Client ID Ozon должен быть положительным числом.")
        normalized = match.group(1)
        if int(normalized) <= 0:
            raise forms.ValidationError("Client ID Ozon должен быть положительным числом.")
        return normalized

    def clean_market_key(self):
        token = (self.cleaned_data.get("market_key") or "").strip()
        if not token:
            raise forms.ValidationError("Укажите API ключ Ozon.")
        return token

    class Meta:
        model = MarketCredential
        fields = ["client_id", "market_key"]
        labels = {
            "client_id": "Client ID Ozon",
            "market_key": "API ключ Ozon",
        }
        widgets = {
            "client_id": forms.TextInput(
                attrs={
                    "class": "input",
                    "autocomplete": "off",
                    "placeholder": "Client ID",
                }
            ),
            "market_key": forms.Textarea(
                attrs={
                    "rows": 3,
                    "class": "input",
                    "autocomplete": "off",
                    "placeholder": "API ключ Ozon",
                }
            ),
        }
