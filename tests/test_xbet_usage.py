"""Compteur de données 1xBet (xbet_usage.json) : deux relevés qui comptent l'un après l'autre sans relire leur copie, ou en même temps dans deux processus, ne s'écrasent jamais.
Hors ligne. Lancer : python -m tests.test_xbet_usage"""
import multiprocessing
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-xbet-usage-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-xbet-usage-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY"):
    os.environ.pop(k, None)

from kelly import store  # noqa: E402
from kelly.sources import xbet  # noqa: E402


def count(n):
    """Un relevé qui a consommé 1 000 octets et 1 requête (zéro au départ)."""
    xbet.http.WIRE[xbet.DOMAIN] = {"bytes": 1000, "requests": 1}
    for _ in range(n):
        xbet._save_usage(xbet.usage(), {"bytes": 0, "requests": 0})


def test_sequential_stale_copies():
    store.save(xbet.USAGE, {})
    stale = xbet.usage()                    # copie lue AVANT les autres relevés
    count(1)
    xbet.http.WIRE[xbet.DOMAIN] = {"bytes": 500, "requests": 1}
    xbet._save_usage(stale, {"bytes": 0, "requests": 0}, session=7)
    u = store.load(xbet.USAGE, {})
    assert u["bytes"] == 1500 and u["requests"] == 2 and u["session"] == 7 and stale["bytes"] == 1500, (u, stale)
    print("ok  compteur 1xBet : une copie périmée n'efface pas la consommation d'un autre relevé, la session est retenue")


def test_two_processes():
    store.save(xbet.USAGE, {})
    ps = [multiprocessing.get_context("fork").Process(target=count, args=(25,)) for _ in range(4)]
    for p in ps:
        p.start()
    for p in ps:
        p.join()
    u = store.load(xbet.USAGE, {})
    assert u["bytes"] == 100 * 1000 and u["requests"] == 100, u
    print("ok  compteur 1xBet : 4 processus qui comptent en même temps ne perdent aucun octet")


if __name__ == "__main__":
    test_sequential_stale_copies()
    test_two_processes()
    print("tests compteur 1xBet : OK")
