"""« saison N » sans combiné : la plus haute cote annoncée vient des MÊMES jambes que le constructeur (voie saison, cote 1xBet, jambes libres), jamais des jambes déjà utilisées ou hors voie,
et ne contredit jamais la cote visée. Hors ligne, données temporaires. Lancer : python -m tests.test_season_reach_msg"""
import os
import sys
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-sreach-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-sreach-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)

from kelly import combos as CB  # noqa: E402


def leg(i, o, lane="saison", xodds=True):
    return {"id": f"m{i}", "market": "totals", "odds": o, "xodds": o if xodds else None, "prob": 0.5, "lane": lane, "sval": True}


def test_reach_on_builder_pool():
    legs = [leg(0, 10), leg(1, 10), leg(2, 10, lane="autre"), leg(3, 10, xodds=False)]
    assert CB.reach(legs) == 10000.0, "l'ancien calcul comptait des jambes que le constructeur ne prend pas"
    assert CB.reach(CB.season_pool(legs), 25) == 100.0, "seules les jambes saison avec cote 1xBet comptent"
    print("ok  plus haute cote = celle des jambes du constructeur")


if __name__ == "__main__":
    test_reach_on_builder_pool()
    print("tests saison reach : OK")
