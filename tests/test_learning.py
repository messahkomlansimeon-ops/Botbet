"""Apprentissage : cote finale gratuite des paris fantômes (football-data, BetExplorer), fiabilité par championnat, bilan de la
semaine. Lancer : python -m tests.test_learning"""
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-learning-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-learning-cache-")
os.environ["ODDS_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-learning-odds-")
os.environ.pop("TELEGRAM_TOKEN", None)

from kelly import calib, config as C, freeclose, learn, store, weekly  # noqa: E402

NOW = datetime(2026, 10, 12, 8, tzinfo=timezone.utc)


def row(key, market, name, sk="soccer_epl", home="Arsenal", away="Chelsea", point=None, prob_book=0.5, days=1, **kw):
    ko = (NOW - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    return {"key": key, "id": key, "sport_key": sk, "group": "soccer", "home": home, "away": away, "commence": ko, "market": market,
            "name": name, "point": point, "odds": 2.1, "prob": prob_book, "prob_book": prob_book, "book": "Pinnacle", "sent": False,
            "closed_at": None, "clv": None, "date": ko[:10], "result": None} | kw


class FakeFD:
    """settle.FootballData : un match Arsenal - Chelsea avec ses cotes de clôture Pinnacle (1N2 et plus/moins 2,5)."""

    def match(self, b):
        if b["home"] != "Arsenal":
            return None
        return {"odds": {"1x2": {"close": {"ps": [1.8, 3.8, 4.6]}}, "ou25": {"close": {"avg": [1.7, 2.2]}}}}


def test_free_closing_odds():
    rows = [row("h", "h2h", "Arsenal", prob_book=0.5),                                   # football-data / Pinnacle
            row("o", "totals", "Over", point=2.5, prob_book=0.52),                        # football-data / moyenne
            row("o3", "totals", "Over", point=3.5),                                       # ligne absente des cotes de clôture
            row("b", "h2h", "Draw", sk="soccer_brazil_campeonato", home="Santos", away="Flamengo", prob_book=0.3),   # BetExplorer
            row("s", "h2h", "Arsenal", sent=True, closed_at="2026-10-11T17:00", clv=0.02),   # déjà relevé : intact
            row("old", "h2h", "Arsenal", days=C.FREE_CLOSE_DAYS + 1),                       # trop ancien
            row("live", "h2h", "Arsenal", days=0.05)]                                         # pas encore terminé
    bx = {"leagues": {"soccer_brazil_campeonato": {"matches": [{"d": rows[3]["commence"][:10], "home": "Santos", "away": "Flamengo",
                                                                "o1": 2.9, "ox": 3.3, "o2": 2.5}]}}}
    assert freeclose.fill(rows, NOW, FakeFD(), bx) == 3
    by = {r["key"]: r for r in rows}
    p_home = (1 / 1.8) / (1 / 1.8 + 1 / 3.8 + 1 / 4.6)
    assert abs(by["h"]["clv"] - round(math.log(p_home / 0.5), 4)) < 1e-9 and by["h"]["close_src"] == "football-data/ps"
    assert by["h"]["closed_at"] == "free:football-data" and by["h"]["w"] == C.FREE_CLOSE_WEIGHT and by["h"]["clv"] > 0
    assert by["o"]["close_src"] == "football-data/avg" and by["b"]["close_src"] == "betexplorer" and by["b"]["clv"] < 0
    assert by["o3"]["clv"] is None and by["old"]["clv"] is None and by["live"]["clv"] is None and by["s"]["clv"] == 0.02
    # les « bookmakers lents » ne se mesurent que sur de vrais relevés
    assert learn.book_stats([dict(by["h"])] * 6) == {}
    print("ok  cote finale gratuite")


def test_reliability_by_league():
    calib._memo.clear()
    store.save(calib.FILE, {"corners": [10, 6, 6.5]})                                   # ancien format : toujours lu
    calib._memo.clear()
    mk = "alternate_totals_corners"
    rows = [{"market": mk, "sport_key": "soccer_brazil_campeonato", "prob": 0.7, "p0": 0.7, "result": "win" if i < 33 else "loss"}
            for i in range(60)]
    rows += [{"market": mk, "sport_key": "soccer_epl", "prob": 0.7, "p0": 0.7, "result": "win" if i < 42 else "loss"} for i in range(60)]
    assert calib.add(rows) == 120
    fam = calib.factor(mk)
    br, epl = calib.factor(mk, sport_key="soccer_brazil_campeonato"), calib.factor(mk, sport_key="soccer_epl")
    assert br < fam < epl, (br, fam, epl)                                                # le Brésil plus corrigé que l'Angleterre
    assert calib.factor(mk, sport_key="soccer_usa_mls") == fam                           # championnat sans assez de pronostics
    assert calib.calibrate(mk, 0.7, sport_key="soccer_brazil_campeonato") < calib.calibrate(mk, 0.7, sport_key="soccer_epl")
    ll = calib.league_lines()
    assert ll[0].startswith("Premier League : 70 % réalisés pour 70 % annoncés (60)") and ll[-1].startswith("Brasileirão"), ll
    assert calib.lines()[0].startswith("corners : ")                                    # « _lg » n'est pas une famille
    print("ok  fiabilité par championnat")


def test_weekly_report():
    store.save("weekly.json", {})
    store.save("bets.json", {"picks": [], "combos": []})
    store.save("tracked.json", [])
    store.save("fair.json", [])
    store.save("fair_combos.json", [])
    calib._memo.clear()
    store.save(calib.FILE, {})
    calib._memo.clear()
    assert weekly.due(NOW) and weekly.message(NOW) is None                              # rien à dire : pas de message
    weekly.mark(NOW)
    assert not weekly.due(NOW) and weekly.due(NOW + timedelta(days=7))
    store.save("bets.json", {"picks": [], "combos": [{"tier": "🛡️ SÛR", "keys": [], "odds": 5.0, "stake": 1.0, "date": "2026-10-10",
                                                      "result": "win", "odds_final": 5.0}]})
    store.save("tracked.json", [row("a", "h2h", "Arsenal", sent=True, clv=0.03, closed_at="x"),
                                row("g", "h2h", "Arsenal", clv=-0.01, close_src="betexplorer", closed_at="free:betexplorer")])
    store.save(calib.FILE, {"corners": [100, 55, 70.0], "goals": [80, 50, 50.0]})
    calib._memo.clear()
    msg = weekly.message(NOW)
    assert "Ce que Kelly a appris cette semaine" in msg and "1 gagné sur 1" in msg, msg
    assert "+4 u" not in msg and " u<" not in msg and "Tes paris notés" not in msg, msg          # un combiné seulement conseillé : aucun montant, jamais d'unités
    # le même combiné NOTÉ (cote et mise de 1xBet, en FCFA) : le résultat de tes paris notés
    store.save("bets.json", {"picks": [], "combos": [{"tier": "🛡️ SÛR", "keys": [], "odds": 5.0, "stake": None, "date": "2026-10-10",
                                                      "result": "win", "odds_final": 5.0, "cur": "FCFA", "played": {"odds": 5.0, "stake": 1000}}]})
    noted = weekly.message(NOW)
    assert "💰 <b>Tes paris notés</b> : <b>+4 000 FCFA</b> sur 1 pari" in noted and " u<" not in noted, noted
    assert "battue 1 fois sur 1" in msg and "Paris suivis en silence : 0 sur 1 (1 mesurés gratuitement)" in msg, msg
    assert "⚠️ corners : trop optimiste, 55 % réalisés pour 70 % annoncés (100)" in msg and "Kelly baisse ses probabilités" in msg, msg
    assert "✅ buts : fiable" in msg or "👍 buts : prudente" in msg, msg
    print("ok  bilan de la semaine")


def main():
    test_free_closing_odds()
    test_reliability_by_league()
    test_weekly_report()
    print("tests apprentissage : OK")


if __name__ == "__main__":
    main()
