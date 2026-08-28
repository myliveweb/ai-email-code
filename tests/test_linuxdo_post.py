"""Очистка первого поста форума до простого текста.

Сеть и база не нужны: `plain_post` работает со строкой `cooked` из ответа Discourse.
Проверяем то, из-за чего перевод портился бы: обвязка Discourse вокруг картинок
и ссылок, слипшиеся абзацы и адрес станции, который в постах написан текстом ссылки.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.linuxdo_cdk_watch import plain_post  # noqa: E402


def test_paragraphs_stay_separate():
    """Абзацы не слипаются: между ними перенос, а не пробел."""
    out = plain_post("<p>站点已开放</p><p>额度 5 刀</p>")
    assert out == "站点已开放\n额度 5 刀"


def test_link_text_survives():
    """Адрес станции в постах написан текстом ссылки — его терять нельзя."""
    out = plain_post('<p>地址：<a href="https://x.com" class="onebox">api.example.com</a></p>')
    assert "api.example.com" in out


def test_script_and_style_dropped():
    """Содержимое script и style в текст поста не попадает."""
    out = plain_post("<style>.a{color:red}</style><p>兑换码 ABCD</p><script>var x=1</script>")
    assert out == "兑换码 ABCD"


def test_entities_unescaped():
    """HTML-entities разворачиваются: модель не должна читать &amp;."""
    assert plain_post("<p>1 &amp; 2 &lt;3&gt;</p>") == "1 & 2 <3>"


def test_image_replaced_by_marker():
    """Картинка остаётся пометкой — по ней видно, что в посте был скриншот."""
    out = plain_post('<p>看图</p><img src="/a.png" alt="">')
    assert "[картинка]" in out


def test_blank_runs_collapsed():
    """Пустых строк подряд не больше одной — иначе половина лимита уходит на воздух."""
    out = plain_post("<p>a</p><br><br><br><p>b</p>")
    assert "\n\n\n" not in out
