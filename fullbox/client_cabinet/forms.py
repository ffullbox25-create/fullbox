import re

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

from sku.models import Agency


def _normalize_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def _format_phone(digits: str) -> str:
    if not digits:
        return ""
    main = digits[1:]
    chunks = [
        main[:3],
        main[3:6],
        main[6:8],
        main[8:10],
    ]
    if len(chunks[0]) < 3:
        return f"+7 ({chunks[0]}"
    formatted = f"+7 ({chunks[0]})"
    if chunks[1]:
        formatted += f" {chunks[1]}"
    if chunks[2]:
        formatted += f"-{chunks[2]}"
    if chunks[3]:
        formatted += f"-{chunks[3]}"
    return formatted


class AgencyForm(forms.ModelForm):
    short_name = forms.CharField(
        required=False,
        label="Краткое наименование",
        widget=forms.TextInput(
            attrs={
                "placeholder": "Если пусто - заполнится по названию организации",
            }
        ),
    )
    portal_login = forms.CharField(
        required=False,
        label="Логин клиента",
        widget=forms.TextInput(
            attrs={
                "autocomplete": "off",
            }
        ),
    )
    portal_password = forms.CharField(
        required=False,
        strip=False,
        label="Пароль для входа",
        widget=forms.PasswordInput(
            render_value=False,
            attrs={
                "autocomplete": "new-password",
            }
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        required_fields = ("inn", "phone", "fio_agn")
        for name in required_fields:
            field = self.fields.get(name)
            if field:
                field.required = True
                field.widget.attrs["required"] = "required"
        phone_field = self.fields.get("phone")
        if phone_field:
            phone_field.widget.attrs.setdefault("inputmode", "tel")
            phone_field.widget.attrs.setdefault("placeholder", "+7 (___) ___-__-__")
        is_edit = bool(self.instance and getattr(self.instance, "pk", None))
        login_field = self.fields.get("portal_login")
        password_field = self.fields.get("portal_password")
        if login_field:
            if is_edit and getattr(self.instance, "portal_user_id", None):
                login_field.initial = self.instance.portal_user.username or ""
                login_field.widget.attrs["placeholder"] = "Логин клиента"
            else:
                login_field.widget.attrs["placeholder"] = "Создастся автоматически: clientN"
        if password_field:
            if is_edit:
                password_field.label = "Новый пароль"
                password_field.widget.attrs["placeholder"] = "Оставьте пустым, чтобы не менять пароль"
            else:
                password_field.label = "Пароль для входа"
                password_field.widget.attrs["placeholder"] = "Задайте пароль для входа клиента"
        if is_edit:
            self.fields["short_name"].initial = self.instance.short_name or ""
            if str(self.instance.kpp or "").strip().lower() in {"none", "null", "-"}:
                # Старые записи ИП могли хранить текстовый маркер отсутствия КПП.
                # Для формы это пустое необязательное поле, а не значение "None".
                self.initial["kpp"] = ""

    def clean_short_name(self):
        return _normalize_text(self.cleaned_data.get("short_name"))

    def clean_portal_login(self):
        value = str(self.cleaned_data.get("portal_login") or "").strip()
        if not value:
            return ""
        if len(value) < 3:
            raise forms.ValidationError("Логин должен быть не короче 3 символов.")
        if not re.fullmatch(r"[A-Za-z0-9_.@+-]+", value):
            raise forms.ValidationError("Логин может содержать только латиницу, цифры и символы . _ @ + -")
        User = get_user_model()
        qs = User.objects.filter(username=value)
        if self.instance and getattr(self.instance, "portal_user_id", None):
            qs = qs.exclude(pk=self.instance.portal_user_id)
        if qs.exists():
            raise forms.ValidationError("Такой логин уже занят.")
        return value

    def clean_portal_password(self):
        password = str(self.cleaned_data.get("portal_password") or "")
        if not password:
            return ""
        user = None
        if self.instance and getattr(self.instance, "portal_user_id", None):
            user = self.instance.portal_user
        if user is None:
            user = get_user_model()(
                username=str(self.cleaned_data.get("portal_login") or "").strip(),
                email=str(self.data.get("email") or "").strip(),
            )
        try:
            validate_password(password, user=user)
        except ValidationError as exc:
            raise forms.ValidationError(exc.messages) from exc
        return password

    def clean_agn_name(self):
        value = _normalize_text(self.cleaned_data.get("agn_name"))
        if value and len(value) < 2:
            raise forms.ValidationError("Название организации слишком короткое.")
        return value

    def clean_pref(self):
        return _normalize_text(self.cleaned_data.get("pref"))

    def clean_inn(self):
        inn_raw = _normalize_text(self.cleaned_data.get("inn")) or ""
        if not inn_raw:
            raise forms.ValidationError("ИНН обязателен.")
        inn = re.sub(r"\D", "", inn_raw)
        if not inn.isdigit():
            raise forms.ValidationError("ИНН должен содержать только цифры.")
        if inn and len(inn) not in (10, 12):
            raise forms.ValidationError("ИНН должен быть 10 или 12 цифр.")
        if inn:
            qs = Agency.objects.filter(inn=inn)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise forms.ValidationError("Такой ИНН уже есть в системе.")
        return inn

    def clean_kpp(self):
        value = _normalize_text(self.cleaned_data.get("kpp"))
        if not value or value.lower() in {"none", "null", "-"}:
            return None
        digits = re.sub(r"\D", "", value)
        if not digits.isdigit() or len(digits) != 9:
            raise forms.ValidationError("КПП должен содержать 9 цифр.")
        return digits

    def clean_ogrn(self):
        value = _normalize_text(self.cleaned_data.get("ogrn"))
        if not value:
            return None
        digits = re.sub(r"\D", "", value)
        if not digits.isdigit() or len(digits) not in (13, 15):
            raise forms.ValidationError("ОГРН должен содержать 13 или 15 цифр.")
        return digits

    def clean_phone(self):
        value = _normalize_text(self.cleaned_data.get("phone")) or ""
        if not value:
            raise forms.ValidationError("Телефон обязателен.")
        digits = re.sub(r"\D", "", value)
        if len(digits) == 10:
            digits = "7" + digits
        elif len(digits) == 11 and digits.startswith("8"):
            digits = "7" + digits[1:]
        if len(digits) != 11 or not digits.startswith("7"):
            raise forms.ValidationError("Телефон должен начинаться с +7 и содержать 11 цифр.")
        return _format_phone(digits)

    def clean_email(self):
        return _normalize_text(self.cleaned_data.get("email"))

    def clean_adres(self):
        return _normalize_text(self.cleaned_data.get("adres"))

    def clean_fakt_adres(self):
        return _normalize_text(self.cleaned_data.get("fakt_adres"))

    def clean_fio_agn(self):
        value = _normalize_text(self.cleaned_data.get("fio_agn")) or ""
        if not value:
            raise forms.ValidationError("ФИО контактного обязательно.")
        if re.search(r"\d", value):
            raise forms.ValidationError("ФИО не должно содержать цифры.")
        if len(value) < 3:
            raise forms.ValidationError("ФИО слишком короткое.")
        return value

    def clean_contract_numb(self):
        return _normalize_text(self.cleaned_data.get("contract_numb"))

    def clean_contract_link(self):
        value = _normalize_text(self.cleaned_data.get("contract_link"))
        if not value:
            return None
        validator = URLValidator()
        try:
            validator(value)
        except ValidationError as exc:
            raise forms.ValidationError("Ссылка на договор должна быть корректным URL.") from exc
        return value

    def clean(self):
        cleaned = super().clean()
        cleaned["agn_name"] = _normalize_text(cleaned.get("agn_name"))
        cleaned["short_name"] = _normalize_text(cleaned.get("short_name"))
        cleaned["pref"] = _normalize_text(cleaned.get("pref"))
        cleaned["adres"] = _normalize_text(cleaned.get("adres"))
        cleaned["fakt_adres"] = _normalize_text(cleaned.get("fakt_adres"))
        cleaned["contract_numb"] = _normalize_text(cleaned.get("contract_numb"))
        cleaned["contract_link"] = _normalize_text(cleaned.get("contract_link"))
        cleaned["email"] = _normalize_text(cleaned.get("email"))
        login = cleaned.get("portal_login") or ""
        password = cleaned.get("portal_password") or ""
        has_user = bool(self.instance and getattr(self.instance, "portal_user_id", None))
        if login and not has_user and not password:
            self.add_error("portal_password", "Укажите пароль, чтобы создать учетную запись клиента.")
        return cleaned


    class Meta:
        model = Agency
        fields = [
            "agn_name",
            "short_name",
            "pref",
            "inn",
            "kpp",
            "ogrn",
            "phone",
            "email",
            "adres",
            "fakt_adres",
            "fio_agn",
            "sign_oferta",
            "use_nds",
            "contract_numb",
            "contract_link",
            "archived",
        ]
        widgets = {
            "agn_name": forms.Textarea(attrs={"rows": 2, "data-autogrow": "true"}),
            "adres": forms.Textarea(attrs={"rows": 2}),
            "fakt_adres": forms.Textarea(attrs={"rows": 2}),
        }


class WBMarketplaceSettingsForm(forms.Form):
    market_key = forms.CharField(
        required=False,
        label="API токен WB",
        widget=forms.Textarea(
            attrs={
                "class": "market-token-input",
                "rows": 4,
                "autocomplete": "new-password",
                "autocapitalize": "off",
                "autocorrect": "off",
                "spellcheck": "false",
                "data-lpignore": "true",
                "data-1p-ignore": "true",
                "placeholder": "Введите новый токен Wildberries",
            }
        ),
    )

    def __init__(self, *args, credential_exists=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.credential_exists = credential_exists

    def clean_market_key(self):
        return _normalize_text(self.cleaned_data.get("market_key")) or ""


class OzonMarketplaceSettingsForm(forms.Form):
    client_id = forms.CharField(
        required=False,
        label="Client ID Ozon",
        widget=forms.TextInput(
            attrs={
                "class": "market-text-input",
                "autocomplete": "new-password",
                "autocapitalize": "off",
                "autocorrect": "off",
                "spellcheck": "false",
                "data-lpignore": "true",
                "data-1p-ignore": "true",
                "placeholder": "Client ID",
            }
        ),
    )
    market_key = forms.CharField(
        required=False,
        label="API ключ Ozon",
        widget=forms.Textarea(
            attrs={
                "class": "market-token-input",
                "rows": 4,
                "autocomplete": "new-password",
                "autocapitalize": "off",
                "autocorrect": "off",
                "spellcheck": "false",
                "data-lpignore": "true",
                "data-1p-ignore": "true",
                "placeholder": "Введите новый API ключ Ozon",
            }
        ),
    )

    def __init__(
        self,
        *args,
        credential_exists=False,
        existing_client_id="",
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.credential_exists = credential_exists
        self.existing_client_id = str(existing_client_id or "").strip()

    def clean_client_id(self):
        client_id = _normalize_text(self.cleaned_data.get("client_id")) or ""
        if not client_id:
            return ""
        match = re.fullmatch(r"(\d+)(?:\.0+)?", client_id)
        if not match:
            raise forms.ValidationError("Client ID Ozon должен быть положительным числом.")
        normalized = match.group(1)
        if int(normalized) <= 0:
            raise forms.ValidationError("Client ID Ozon должен быть положительным числом.")
        return normalized

    def clean_market_key(self):
        return _normalize_text(self.cleaned_data.get("market_key")) or ""

    def clean(self):
        cleaned = super().clean()
        client_id = cleaned.get("client_id") or ""
        market_key = cleaned.get("market_key") or ""
        if client_id and not market_key and not self.credential_exists:
            self.add_error("market_key", "Укажите API ключ Ozon.")
        if (
            client_id
            and not market_key
            and self.credential_exists
            and client_id != self.existing_client_id
        ):
            self.add_error(
                "market_key",
                "При изменении Client ID укажите новый API ключ Ozon.",
            )
        if market_key and not client_id:
            self.add_error("client_id", "Укажите Client ID Ozon.")
        return cleaned
