"""Порядок адресов при заходах на одну голову.

Отказал не ящик, а адрес: письмо GitHub недосылает без связи с почтой, зато к IP
придирчив. Повторный заход обязан идти с другого прокси, иначе он бессмыслен.
"""

from scripts.harvest_accounts import ATTEMPTS, proxy_order

POOL = ["http://u:p@1.1.1.1:1", "http://u:p@2.2.2.2:2", "http://u:p@3.3.3.3:3"]


def test_заходов_столько_же_сколько_попыток():
    assert len(proxy_order(POOL, 0)) == ATTEMPTS


def test_повторный_заход_идёт_с_другого_адреса():
    for turn in range(len(POOL) * 2):
        order = proxy_order(POOL, turn)
        assert len(set(order)) == len(order)


def test_головы_расходятся_по_пулу():
    assert proxy_order(POOL, 0)[0] != proxy_order(POOL, 1)[0]


def test_пул_из_одного_адреса_не_роняет_выборку():
    order = proxy_order(POOL[:1], 3)
    assert order == [POOL[0]] * ATTEMPTS
