"""Discrétion (hors ligne) : mises rondes (STAKE_STEP), plafond de tickets par jour (MAX_TICKETS_DAY), User-Agent de navigateur pour 1xBet seulement. Lancer : python -m tests.test_discreet"""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-discreet-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-discreet-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY", "KELLY_UA"):
    os.environ.pop(k, None)

from kelly import config as C, money  # noqa: E402
from kelly.sources import http, s365, xbet  # noqa: E402


def test_round_stakes():
    assert C.STAKE_STEP == 0, "dans les tests (DATA_DIR défini) le plancher est éteint par défaut : les autres tests gardent l'ancien pas"
    C.STAKE_STEP = 100
    assert money.round_step(5000) == 100 and money.round_step(50000) == 500 and money.round_step(900) == 5, "budget trop petit pour un pas de 100 : inchangé"
    C.STAKE_STEP = 0
    assert money.round_step(5000) == 50
    C.STAKE_STEP = 100
    items = [{"kind": "pari", "tier": "x", "prob": 0.6 + i * 0.01, "odds": 2.0} for i in range(4)]
    stakes = money.allocate(items, budget=5000)[0]
    assert all(s % 100 == 0 for s in stakes), stakes
    C.STAKE_STEP = 0
    print("ok  mises rondes : pas de 100 FCFA dès 1 000 de budget, STAKE_STEP=0 rétablit l'ancien pas")


def test_ticket_cap():
    items = [{"kind": "pari", "tier": "x", "prob": 0.5 + i * 0.05, "odds": 2.2} for i in range(5)]
    base = money.allocate(items, budget=20000)[0]
    assert sum(1 for s in base if s) >= 4, base
    C.MAX_TICKETS_DAY = 2
    try:
        capped = money.allocate(items, budget=20000)[0]
    finally:
        C.MAX_TICKETS_DAY = 0
    assert sum(1 for s in capped if s) == 2 and all(c == b or c == 0 for c, b in zip(capped, base)) and set(i for i, s in enumerate(capped) if s) == set(sorted(range(5), key=lambda i: -base[i])[:2])
    print("ok  plafond de tickets : seules les 2 plus grosses mises restent, 0 = pas de plafond")


def test_user_agent():
    assert http.UA == http.BROWSER_UA and http.UA.startswith("Mozilla/5.0") and "Bot" not in http.UA, "toutes les lectures se présentent comme un navigateur de téléphone"
    assert not http.UA_BY_DOMAIN and "KellyBot" not in http.UA
    print("ok  toutes les sources (1xBet, 365scores, FotMob, football-data…) : User-Agent de navigateur de téléphone")


def test_jitter():
    sleeps = []
    real = (http._sleep, http.delay_for, C.SANDBOX, http._last.copy())
    http._sleep = sleeps.append
    http.delay_for = lambda d: 3.0
    http._last["x.test"] = http.state.clock()
    try:
        C.SANDBOX = False
        http._wait("x.test")
        assert len(sleeps) == 2 and 2.5 <= sleeps[0] <= 3.0 and 0 <= sleeps[1] <= 0.75 + 1e-9, sleeps
        sleeps.clear()
        C.SANDBOX = True
        http._wait("x.test")
        assert len(sleeps) == 1, "tests : pas d'écart aléatoire"
    finally:
        http._sleep, http.delay_for, C.SANDBOX = real[:3]
        http._last.clear()
        http._last.update(real[3])
    print("ok  rythme : 0 à 25 % d'attente en plus entre deux requêtes, jamais moins ; aucun écart dans les tests")


def main():
    test_round_stakes()
    test_ticket_cap()
    test_user_agent()
    test_jitter()
    print("tests discrétion : OK")


if __name__ == "__main__":
    main()
