from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


_ONES = (
    "",
    "один",
    "два",
    "три",
    "четыре",
    "пять",
    "шесть",
    "семь",
    "восемь",
    "девять",
)
_ONES_FEM = (
    "",
    "одна",
    "две",
    "три",
    "четыре",
    "пять",
    "шесть",
    "семь",
    "восемь",
    "девять",
)
_TEENS = (
    "десять",
    "одиннадцать",
    "двенадцать",
    "тринадцать",
    "четырнадцать",
    "пятнадцать",
    "шестнадцать",
    "семнадцать",
    "восемнадцать",
    "девятнадцать",
)
_TENS = (
    "",
    "",
    "двадцать",
    "тридцать",
    "сорок",
    "пятьдесят",
    "шестьдесят",
    "семьдесят",
    "восемьдесят",
    "девяносто",
)
_HUNDREDS = (
    "",
    "сто",
    "двести",
    "триста",
    "четыреста",
    "пятьсот",
    "шестьсот",
    "семьсот",
    "восемьсот",
    "девятьсот",
)


def _triad(n: int, *, feminine: bool = False) -> str:
    if n <= 0:
        return ""
    ones = _ONES_FEM if feminine else _ONES
    h, rem = divmod(n, 100)
    parts = []
    if h:
        parts.append(_HUNDREDS[h])
    if 10 <= rem <= 19:
        parts.append(_TEENS[rem - 10])
    else:
        t, o = divmod(rem, 10)
        if t:
            parts.append(_TENS[t])
        if o:
            parts.append(ones[o])
    return " ".join(parts)


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19:
        return many
    n = n % 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def money_to_words(amount) -> str:
    """Сумма прописью: «Тринадцать тысяч ... рублей 00 копеек»."""
    value = Decimal(str(amount or "0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = ""
    if value < 0:
        sign = "минус "
        value = -value
    rubles = int(value)
    kopecks = int((value - rubles) * 100)

    if rubles == 0:
        words = "ноль"
    else:
        billions, rem = divmod(rubles, 1_000_000_000)
        millions, rem = divmod(rem, 1_000_000)
        thousands, ones = divmod(rem, 1000)
        chunks = []
        if billions:
            chunks.append(f"{_triad(billions)} {_plural(billions, 'миллиард', 'миллиарда', 'миллиардов')}")
        if millions:
            chunks.append(f"{_triad(millions)} {_plural(millions, 'миллион', 'миллиона', 'миллионов')}")
        if thousands:
            chunks.append(
                f"{_triad(thousands, feminine=True)} {_plural(thousands, 'тысяча', 'тысячи', 'тысяч')}"
            )
        if ones:
            chunks.append(_triad(ones))
        words = " ".join(part for part in chunks if part).strip()

    rub_word = _plural(rubles, "рубль", "рубля", "рублей")
    kop_word = _plural(kopecks, "копейка", "копейки", "копеек")
    text = f"{sign}{words} {rub_word} {kopecks:02d} {kop_word}".replace("  ", " ").strip()
    return text[:1].upper() + text[1:] if text else "Ноль рублей 00 копеек"
