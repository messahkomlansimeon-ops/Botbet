"""Voie `saison`, commit D (hors ligne) : la ligne « saison » du bilan par voie (réalisé / annoncé, CLV 1xBet avec son intervalle, valeur et remplissage séparés, argent des combinés notés
par palier), le CLV par jambe avec `saison` à part, l'état dans `status`, et l'ÉTANCHÉITÉ : objectifs, coupure et lignes des autres voies identiques avec ou sans journal `saison`.
Lancer : python -m tests.test_season_report"""
import os
import tempfile
from datetime import date

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-season-report-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-season-report-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY", "ENSEMBLE_FROZEN_MODEL"):
    os.environ.pop(k, None)

from kelly import config as C, ensemble, fair, lanes, lanestats as L, money, seasonlane as SL, store  # noqa: E402

TODAY = str(date.today())


def row(i, strategy="stats", odds=2.2, close=2.0, res="win", key=None, **kw):
    return {"key": key or f"k{i}", "id": f"m{i}", "date": TODAY, "market": "alternate_totals_corners", "sport_key": "soccer_epl", "strategy": strategy, "result": res, "xodds": odds,
            "xclose": {"odds": close, "lead_min": 60, "susp": False}} | kw


def sea(i, sval=1, res="win", odds=2.2, close=2.0, pe=0.6, **kw):
    return row(i, "saison", odds, close, res, key=f"s{i}|alternate_totals_corners||9.5|Over|saison", sval=sval, pe=pe, pm=0.6, **kw)


def test_lines():
    money.set_daily(0)
    store.save(ensemble.LOG_FILE, [row(i) for i in range(3)])
    store.save(SL.LOG, [sea(1), sea(2, res="loss", odds=1.9, close=2.0), sea(3, sval=0, res="win", odds=1.5, close=1.55, pe=0.7), sea(4, sval=0, res="loss", pe=0.7),
                        sea(5, sval=None, ref=1, res="win")])                          # la 5e est une référence sans verdict : jamais une jambe
    L._memo["key"] = None
    # deux combinés saison notés et réglés (un gagné, un perdu) + un combiné d'une autre voie
    legs = lambda strat: [{"key": f"c{i}|totals||2.5|Over|saison", "id": f"c{i}", "result": "win", "strategy": strat, "sval": 1, "xodds": 2.0, "prob": 0.5} for i in range(2)]      # noqa: E731
    store.save("fair_combos.json", [
        {"date": TODAY, "tier": "ss3", "min_odds": 3.0, "prob": 0.3, "legs": legs("saison"), "result": "win", "has_value": True,
         "played": {"odds": 3.0, "stake": 500, "at": "x"}, "stake": 500, "cur": "FCFA"},
        {"date": TODAY, "tier": "ss10", "min_odds": 10.0, "prob": 0.1, "legs": [dict(l, result="loss") for l in legs("saison")], "result": "loss", "has_value": True,
         "played": {"odds": 10.0, "stake": 200, "at": "x"}, "stake": 200, "cur": "FCFA"}])
    lines = L.lane_lines([], None, plain=False)
    sl = next(x for x in lines if x.startswith("saison"))
    assert "valeur : 1/2 (réalisé 50 % pour 60 % annoncé" in sl and "remplissage : 1/2 (réalisé 50 % pour 70 % annoncé" in sl, sl
    assert "CLV 1xBet" in sl and "cote 3 : +1 000 FCFA sur 1" in sl and "cote 10 : −200 FCFA sur 1" in sl, sl
    assert sl.startswith("saison : 0 gagnée") or "gagnée" in sl.split(" · ")[0] or sl.startswith("saison : aucune jambe réglée dans un combiné"), sl
    assert "valeur : 1/2" in sl and "remplissage : 1/2" in sl, "la référence sans sval (5e ligne) n'est pas une jambe"
    # une jambe de remplissage de la voie (strategy « fill », lane « saison ») reste dans la ligne « saison », jamais dans « fill »
    store.save("fair_combos.json", store.load("fair_combos.json", []) + [{"date": TODAY, "tier": "ss30", "min_odds": 30.0, "prob": 0.03, "result": "loss", "has_value": False,
                                                                           "legs": [{"key": "f1|totals||2.5|Over|saison", "id": "f1", "strategy": "fill", "lane": "saison", "sval": 0, "result": "loss", "xodds": 1.5, "prob": 0.7}]}])
    lines_f = L.lane_lines([], None)
    assert not any(x.startswith("fill") for x in lines_f), lines_f
    assert any(x.startswith("saison :") and "gagnée" in x for x in lines_f)
    # un combiné « saison N » (palier m<n>, jambes saison) noté et réglé : ligne « sur mesure » ; une ligne sans `pe` n'entre pas dans l'annoncé
    store.save("fair_combos.json", store.load("fair_combos.json", []) + [{"date": TODAY, "tier": "m1", "custom": True, "min_odds": 5.0, "prob": 0.2, "legs": legs("saison"),
                                                                           "result": "win", "has_value": True, "played": {"odds": 5.0, "stake": 100, "at": "x"}, "stake": 100, "cur": "FCFA"}])
    store.save(SL.LOG, store.load(SL.LOG, []) + [sea(9, res="win", pe=None) | {"pe": None}])
    L._memo["key"] = None
    sl2 = next(x for x in L.lane_lines([], None) if x.startswith("saison"))
    assert "sur mesure : +400 FCFA sur 1" in sl2 and "valeur : 2/3 (réalisé 67 % pour 60 % annoncé" in sl2, sl2
    plain = next(x for x in L.lane_lines([], None, plain=True) if x.startswith("saison"))
    assert "prix pris contre le dernier prix 1xBet" in plain and "CLV 1xBet" not in plain, plain
    print("ok  bilan par voie : « saison » avec valeur / remplissage séparés (réalisé pour annoncé, CLV 1xBet ± intervalle), argent des combinés notés par palier de cote")


def test_leg_clv_apart():
    store.save("fair_combos.json", [{"date": TODAY, "tier": "ss10", "min_odds": 10.0, "prob": 0.1, "result": None, "has_value": True,
                                     "legs": [{"key": f"s{i}|alternate_totals_corners||9.5|Over|saison", "id": f"s{i}", "strategy": "saison", "sval": 1, "xodds": 2.2, "prob": 0.5} for i in (1, 2, 3)]
                                     + [{"key": "k0", "id": "m0", "strategy": "stats", "xodds": 2.2, "prob": 0.5}]}])
    L._memo["key"] = None
    lines = L.leg_clv_lines()
    assert lines[0].startswith("jambes mesurées : 1 / 1 "), lines[0]                       # la couverture ne compte que les autres voies
    sl = next(x for x in lines if x.startswith("saison (à part"))
    assert "3 mesurées sur 3 jambes" in sl and "CLV" in sl, sl
    assert not any("palier cote 10" in x.lower() or "palier" in x and "ss10" in x for x in lines if "stats" in x)
    print("ok  CLV par jambe : `saison` à part, hors du ratio de couverture, jamais rangée parmi les voies jouées")


def test_isolation():
    store.save(SL.LOG, [])
    store.save(ensemble.LOG_FILE, [row(i, odds=2.2) for i in range(3)] + [row(10, "sharpgap", odds=1.8, market="totals")])
    L._memo["key"] = None
    before = (L.objective_lines(), [x for x in L.lane_lines([]) if not x.startswith("saison")], L.cut_lines(), L.unlabeled(TODAY))
    store.save(SL.LOG, [sea(i, res="win" if i % 2 else "loss") for i in range(20)])
    L._memo["key"] = None
    after = (L.objective_lines(), [x for x in L.lane_lines([]) if not x.startswith("saison")], L.cut_lines(), L.unlabeled(TODAY))
    assert before == after, (before, after)
    assert not any(r.get("strategy") == "saison" for r in L.journal_rows()), "le journal d'ensemble ne contient rien de la voie"
    print("ok  étanchéité : objectifs, coupure, compteur d'étiquettes et lignes des autres voies identiques avec ou sans journal `saison`")


def test_status_line():
    out = lanes.lines()
    sl = next(x for x in out if x.startswith("saison :"))
    assert f"saison : {C.STRATEGY_SEASON}" in sl and f"poids du modèle {C.SEASON_MODEL_WEIGHT * 100:.0f} %" in sl and "paliers 3, 10, 30, 100" in sl and "part de mise 0.15" in sl, sl
    assert "a priori de marché, ajustement à l'adversaire (football-data seulement)" in sl, sl
    C.SEASON_PRIOR, C.SEASON_FIT = "flat", "moments"
    try:
        sl2 = next(x for x in lanes.lines() if x.startswith("saison :"))
        assert "a priori moyenne de ligue, moyennes rétrécies" in sl2, sl2
    finally:
        C.SEASON_PRIOR, C.SEASON_FIT = "market", "iterative"
    print("ok  status : état de la voie (interrupteur, poids du modèle, marge, paliers, part de mise)")


def main():
    for t in (test_lines, test_leg_clv_apart, test_isolation, test_status_line):
        t()
    print("tests voie saison (bilan) : OK")


if __name__ == "__main__":
    main()
