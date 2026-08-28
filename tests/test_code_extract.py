"""Извлечение кода подтверждения из письма — ловушки, на которых оно ломалось.

Сеть и база тут не нужны: `_extract_verification_code` работает со списком писем.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.app.main import _extract_verification_code  # noqa: E402


def mail(body: str, date: str = "2026-08-24T10:00:00Z") -> dict:
    return {"body": body, "date": date}


def test_anchor_not_reaching_across_words():
    """Якорь «код» не должен дотянуться до цифр за словом «телефона»."""
    body = "Введите код подтверждения: 481596. Для связи код телефона 8391."
    assert _extract_verification_code([mail(body)], 6, "код подтверждения") == "481596"


def test_anchor_blocks_letters_in_gap():
    """Между якорем и кодом слов быть не должно — иначе цепляется чужое число."""
    body = "Ваш код мы отправили на телефон 8391777, ищите его в смс"
    assert _extract_verification_code([mail(body)], 6, "код") is None


def test_css_hex_does_not_win():
    """Шестизначный hex-цвет в вёрстке стоит раньше кода, но якорь ведёт к коду."""
    body = '<div style="color:#1a2b3c;background:#445566">Verification code: 903471</div>'
    assert _extract_verification_code([mail(body)], 6, "Verification code") == "903471"


def test_html_tags_in_gap_allowed():
    body = "Ваш код: <strong style='color:#fff'>&nbsp;204815</strong>"
    assert _extract_verification_code([mail(body)], 6, "Ваш код") == "204815"


def test_fullwidth_punctuation():
    """Китайские письма ставят полноширинное двоеточие."""
    body = "您的验证码为：738201，请勿泄露。"
    assert _extract_verification_code([mail(body)], 6, "验证码") == "738201"


def test_alnum_requires_digit():
    """Слово той же длины кодом не считается, цифра внутри обязательна."""
    plain = _extract_verification_code([mail("Your code: PASSWORD")], 8, "code", "alnum")
    assert plain is None
    real = _extract_verification_code([mail("Your code: aB3xK9zQ")], 8, "code", "alnum")
    assert real == "aB3xK9zQ"


def test_newest_message_wins():
    older = mail("Ваш код: 111111", "2026-08-23T10:00:00Z")
    newer = mail("Ваш код: 222222", "2026-08-24T10:00:00Z")
    assert _extract_verification_code([older, newer], 6, "Ваш код") == "222222"


def test_strict_pattern_beats_loose_in_older_mail():
    """Строгий шаблон прогоняется по всем письмам раньше запасного."""
    loose_but_newer = mail("Ваш код будет отправлен позже: 999999", "2026-08-24T12:00:00Z")
    strict_but_older = mail("Ваш код: 555555", "2026-08-24T09:00:00Z")
    got = _extract_verification_code([loose_but_newer, strict_but_older], 6, "Ваш код")
    assert got == "555555"


def test_loose_pattern_as_fallback():
    """Когда строгого совпадения нет нигде, в дело идёт широкий зазор."""
    body = "Ваш код для входа в аккаунт — 616263"
    assert _extract_verification_code([mail(body)], 6, "Ваш код") == "616263"


def test_code_length_respected():
    """Шестизначный запрос не должен откусывать шесть цифр от восьмизначного числа."""
    body = "Ваш код: 12345678"
    assert _extract_verification_code([mail(body)], 6, "Ваш код") is None


def test_no_anchor_falls_back_to_github_phrase():
    body = "Hi there\nVerification code: 314159\nThanks"
    assert _extract_verification_code([mail(body)]) == "314159"


def test_body_preview_used_when_body_empty():
    msg = {"body": "", "bodyPreview": "Ваш код: 707070", "date": "2026-08-24T10:00:00Z"}
    assert _extract_verification_code([msg], 6, "Ваш код") == "707070"


def test_full_body_beats_truncated_preview():
    """Graph режет bodyPreview на 255 символах — код там разрывался пополам."""
    preview = "Device: Chrome on Linux\r\nVerification code: 141"
    msg = {
        "body": "Device: Chrome on Linux\r\nVerification code: 141339\r\nThanks",
        "bodyPreview": preview,
        "date": "2026-08-24T13:42:00Z",
    }
    assert _extract_verification_code([msg], 6, "Verification code") == "141339"


def test_empty_list():
    assert _extract_verification_code([], 6, "Ваш код") is None


@pytest.mark.parametrize("anchor", ["ваш код", "ВАШ КОД", "Ваш Код"])
def test_anchor_case_insensitive(anchor):
    assert _extract_verification_code([mail("Ваш код: 424242")], 6, anchor) == "424242"
