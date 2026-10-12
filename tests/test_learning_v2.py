"""Apprentissage, 2e version (hors ligne) : A. mémoire qui vieillit (calib.json, demi-vie) ; B. correction par sens du pari et par tranche de proba (monotone,
identique à l'ancienne sans données fines) ; C. prudence par jambe des combinés (mesurée sur les jambes réglées, bornée ; l'optimiseur préfère moins de
jambes à cote égale, un nombre imposé reste respecté) ; D. adoption du modèle CLV sur plusieurs coupes successives (majorité ET ensemble des jours).
Lancer : python -m tests.test_learning_v2"""
import math
import os
import random
import tempfile
from datetime import date, timedelta

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-learn2-")
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ.pop("XBET_PROXY", None)
os.environ.pop("S365_PROXY", None)
os.environ["QUIET_HOURS"] = "off"

from kelly import calib, combos as CB, config as C, learn, store  # noqa: E402

D0 = date(2026, 1, 1)


def reset():
    store.save(calib.FILE, {})
    calib._memo.clear()


def rows(n, won, p0, market="alternate_totals_corners", name="Over", sk="soccer_brazil_campeonato"):
    return [{"key": f"e{i}|{market}||9.5|{name}", "market": market, "name": name, "sport_key": sk, "p0": p0, "prob": p0,
             "result": "win" if i < won else "loss"} for i in range(n)]


def test_aging():
    reset()
    assert calib.add(rows(100, 60, 0.65), D0) == 100
    d = calib.load()
    assert d["corners"] == [100, 60, 65.0] and d["_t"] == str(D0), d
    assert calib.add(rows(1, 1, 0.65), D0 + timedelta(days=240)) == 1          # 2 demi-vies : 100 anciens résultats pèsent comme 25
    n, w, s = calib.load()["corners"]
    assert abs(n - 26) < 1e-6 and abs(w - 16) < 1e-6 and abs(s - (16.25 + 0.65)) < 1e-6, (n, w, s)
    lg = calib.load()["_lg"]["soccer_brazil_campeonato"]["corners"]
    assert abs(lg[0] - 26) < 1e-6, lg                                          # championnats et cases vieillissent aussi
    assert abs(calib.load()["_cell"]["corners|plus"][0] - 26) < 1e-6
    # un ancien fichier sans date : rien n'est perdu, la date est posée
    store.save(calib.FILE, {"corners": [100, 60, 65.0]})
    calib._memo.clear()
    calib.add(rows(1, 0, 0.6), D0 + timedelta(days=30))
    assert calib.load()["corners"] == [101, 60, 65.6] and calib.load()["_t"] == str(D0 + timedelta(days=30))
    old = C.CALIB_HALF_LIFE
    C.CALIB_HALF_LIFE = 0                                                       # 0 : jamais d'oubli
    try:
        calib.add(rows(1, 0, 0.6), D0 + timedelta(days=900))
        assert calib.load()["corners"][0] == 102
    finally:
        C.CALIB_HALF_LIFE = old
    assert calib.count(25.6) == 26
    print("ok  A. mémoire qui vieillit : demi-vie, rapports inchangés, ancien fichier gardé, 0 = pas d'oubli")


def test_side():
    sd = calib.side
    assert sd({"market": "totals", "name": "Over"}) == "plus" and sd({"market": "team_sot_total", "name": "Under"}) == "moins"
    assert sd({"market": "btts", "key": "e|btts|||Yes"}) == "oui" and sd({"market": "btts", "name": "No"}) == "non"
    assert sd({"market": "h2h", "name": "Lyon", "home": "Lyon", "away": "PSG"}) == "dom"
    assert sd({"market": "h2h", "name": "PSG", "event": "Lyon - PSG"}) == "ext" and sd({"market": "h2h", "name": "Draw"}) == "nul"
    assert sd({"market": "double_chance", "name": "Lyon or Draw", "home": "Lyon", "away": "PSG"}) == "1X"
    assert sd({"market": "double_chance", "name": "Draw or PSG", "home": "Lyon", "away": "PSG"}) == "X2"
    assert sd({"market": "double_chance", "name": "Lyon or PSG", "home": "Lyon", "away": "PSG"}) == "12"
    assert sd({"market": "h2h", "name": "Autre", "home": "Lyon", "away": "PSG"}) is None and sd({}) is None
    assert [calib.band(p) for p in (0.55, 0.6, 0.69, 0.7, 0.9)] == [0, 1, 1, 2, 2]
    print("ok  B. sens du pari (plus/moins, oui/non, dom/nul/ext, 1X/X2/12) et tranches")


def acc(n, rate, p):
    return [n, round(n * rate, 4), round(n * p, 4)]


def test_fine_correction():
    # juste à 55 %, trop sûre d'elle à 75 % (75 % annoncés, 62 % réalisés) : seules les hautes probas sont rabaissées
    data = {"goals": [800, 0.55 * 400 + 0.62 * 400, 0.55 * 400 + 0.75 * 400],
            "_cell": {"goals|plus": [800, 0.55 * 400 + 0.62 * 400, 0.55 * 400 + 0.75 * 400],
                      "goals|plus|0": acc(400, 0.55, 0.55), "goals|plus|2": acc(400, 0.62, 0.75)}}
    lo, hi = calib.calibrate("totals", 0.55, data, side="plus"), calib.calibrate("totals", 0.75, data, side="plus")
    assert 0.54 < lo <= 0.55 and hi < 0.67, (lo, hi)
    old_hi = calib.calibrate("totals", 0.75, {"goals": data["goals"]})             # une seule droite pour tout : trop prudente en bas, pas assez en haut
    assert hi < old_hi and lo > calib.calibrate("totals", 0.55, {"goals": data["goals"]}), (hi, old_hi)
    # « moins de » n'a pas de résultats à part : correction de la famille, comme avant
    assert calib.calibrate("totals", 0.75, data, side="moins") == old_hi
    # sans sens ni case : exactement l'ancienne correction (famille, puis championnat)
    base = {"corners": acc(300, 0.58, 0.66), "_lg": {"soccer_brazil_campeonato": {"corners": acc(150, 0.52, 0.66)}}}
    k_fam = calib._k(base["corners"], fam="corners")
    k_lg = calib._k(base["_lg"]["soccer_brazil_campeonato"]["corners"], k_fam, fam="corners")
    for d in (base, base | {"_cell": {}}):
        assert calib.factor("alternate_totals_corners", d) == k_fam
        assert calib.factor("alternate_totals_corners", d, "soccer_brazil_campeonato") == k_lg
        assert calib.factor("alternate_totals_corners", d, "soccer_brazil_campeonato", side="plus", p=0.7) == k_lg
    # monotone : une proba brute plus haute ne donne jamais une proba corrigée plus basse, même avec des tranches très différentes
    rng = random.Random(5)
    for _ in range(300):
        cells = {f"goals|plus|{i}": acc(rng.choice((0, 80, 400, 3000)), rng.uniform(0.3, 0.95), (0.55, 0.65, 0.78)[i]) for i in range(3)}
        cells["goals|plus"] = acc(rng.choice((0, 100, 1000)), rng.uniform(0.4, 0.9), 0.66)
        d = {"goals": acc(rng.choice((0, 100, 1000)), rng.uniform(0.4, 0.9), 0.66), "_cell": cells}
        ps = [0.5 + i * 0.005 for i in range(91)]
        cal = [calib.calibrate("totals", p, d, side="plus") for p in ps]
        assert all(b >= a - 1e-12 for a, b in zip(cal, cal[1:])), (d, [(p, c) for p, c in zip(ps, cal)])
        assert all(calib.K_RANGE[0] - 1e-9 <= calib.factor("totals", d, side="plus", p=p) <= calib.K_RANGE[1] + 1e-9 for p in ps)
    # les cases se remplissent au règlement
    reset()
    calib.add(rows(70, 40, 0.75, market="totals", name="Over") + rows(5, 4, 0.56, market="totals", name="Under"), D0)
    cells = calib.load()["_cell"]
    assert cells["goals|plus"][0] == 70 and cells["goals|plus|2"][0] == 70 and cells["goals|moins|0"][0] == 5, cells
    store.save(calib.FILE, {"_cell": {"goals|plus|2": acc(40, 0.6, 0.75)}})
    calib._memo.clear()
    assert calib.cell_lines() == ["buts · plus · ≥ 70 % : 60 % réalisés pour 75 % annoncés (40)"], calib.cell_lines()
    print("ok  B. correction fine : seules les hautes probas rabaissées, monotone, identique à l'ancienne sans données fines")


def leg(i, odds, prob, ev=None):
    return {"key": f"k{i}", "id": ev or f"e{i}", "odds": odds, "prob": prob, "market": "h2h", "name": "Over"}


def test_combo_prudence():
    reset()
    assert CB.leg_discount() == 0.99
    legs = [leg(i, 1.3, 0.7) for i in range(10)]
    assert abs(CB.combo_prob(legs) - 0.7 ** 10 * 0.99 ** 10) < 1e-15 and CB.combo_prob(legs) < math.prod(c["prob"] for c in legs)
    # les jambes choisies perdent plus que prévu : la prudence grandit, sans sortir des bornes ; jamais comptées deux fois
    played = [{"prob": 0.7, "result": "win" if i < 180 else "loss"} for i in range(300)] + [{"prob": 0.7, "result": "push"}]
    assert calib.add_combo_legs(played, D0) == 300 and calib.add_combo_legs(played, D0) == 0
    d = CB.leg_discount()
    assert 0.90 <= d < 0.95, d
    calib.add_combo_legs([{"prob": 0.8, "result": "loss"} for _ in range(3000)], D0)
    assert CB.leg_discount() == C.COMBO_LEG_K[0]
    calib.add_combo_legs([{"prob": 0.5, "result": "win"} for _ in range(20000)], D0)
    assert CB.leg_discount() == C.COMBO_LEG_K[1]                                 # jamais moins de 1 % de prudence par jambe
    reset()
    # à cote égale (4,0), 2 jambes plutôt que 4 un tout petit peu plus probables avant prudence
    two = [leg(1, 2.0, 0.5), leg(2, 2.0, 0.5)]
    four = [leg(10 + i, math.sqrt(2), 0.71) for i in range(4)]
    assert 0.71 ** 4 > 0.5 ** 2
    got = CB.best_combo(two + four, 3.95, 4.05)
    assert got and {c["id"] for c in got[0]} == {"e1", "e2"}, got
    # un nombre de jambes imposé reste respecté (« combi N pile 4 », « min 4 » : le bouton pour forcer des jambes)
    got = CB.best_combo(two + four, 3.95, 4.05, min_legs=4, max_legs=4)
    assert got and len(got[0]) == 4 and abs(got[2] - CB.combo_prob(four)) < 1e-12, got
    print("ok  C. prudence par jambe : mesurée, bornée, comptée une fois ; moins de jambes à cote égale, nombre imposé respecté")


def clv_rows(days, scale_of):
    """30 lignes par jour : x de 0 à 29/30 ; le CLV vaut scale_of(jour) x x (+ un petit écart propre au jour). L'heuristique classe les lignes dans un ordre
    mélangé : le modèle (poids 1 sur x) gagne quand scale > 0, perd quand scale < 0."""
    out = []
    for d in range(days):
        day = str(D0 + timedelta(days=d))
        for i in range(30):
            x = i / 30
            out.append({"key": f"{day}|{i}", "date": day, "clv": round(scale_of(d) * x * (1 + 0.01 * (d % 3)), 6),
                        "features": {"x": x, "heuristic": (i * 7 % 30) / 30, "edge": 0.0}})
    return out


def with_stub(fn):
    """_ridge remplacé par un modèle qui suit x : le test ne juge que la règle d'adoption."""
    old = learn._ridge
    learn._ridge = lambda rows, names, lam: {"names": ["x"], "mean": [0.0], "std": [1.0], "w": [1.0, 0.0]}
    try:
        return fn()
    finally:
        learn._ridge = old


def test_folds():
    days = 30                                                       # 15 jours d'apprentissage, 3 coupes de 5 jours
    folds = learn._folds(clv_rows(days, lambda d: 1), 3, C.LEARN_MIN_TEST_DAYS, C.LEARN_MIN_TRAIN_DAYS)
    assert [len({r["date"] for r in te}) for _, te in folds] == [5, 5, 5]
    assert all(max(r["date"] for r in tr) < min(r["date"] for r in te) for tr, te in folds)          # jamais le futur
    assert learn._folds(clv_rows(12, lambda d: 1), 3, 5, 3) == []                                    # trop peu de jours

    def scales(f1, f2, f3):
        return lambda d: 0.2 if d < 15 else (f1, f2, f3)[(d - 15) // 5]
    # gagne nettement 2 coupes sur 3 : adopté
    m = with_stub(lambda: learn.train_clv(clv_rows(days, scales(0.145, 0.145, -0.003))))
    assert m["adopted"] and [f["win"] for f in m["folds"]] == [True, True, False] and m["test_days"] == 15, m
    # gagne 1 coupe sur 3, même si l'ensemble des jours passerait le seuil : pas adopté
    m = with_stub(lambda: learn.train_clv(clv_rows(days, scales(-0.003, -0.003, 0.145))))
    assert m["oos_model"] - m["oos_heuristic"] > C.LEARN_OOS_MARGIN and m["oos_t"] >= C.LEARN_OOS_Z, m     # l'ensemble des jours passe le seuil…
    assert not m["adopted"] and [f["win"] for f in m["folds"]] == [False, False, True], m          # … mais pas la majorité des coupes
    # 3 sur 3 : adopté ; perd partout : pas adopté
    assert with_stub(lambda: learn.train_clv(clv_rows(days, lambda d: 0.2)))["adopted"]
    assert not with_stub(lambda: learn.train_clv(clv_rows(days, lambda d: 0.2 if d < 15 else -0.2)))["adopted"]
    # moins de jours que 3 coupes : la coupe unique d'avant (pas de « folds »)
    m = with_stub(lambda: learn.train_clv(clv_rows(14, lambda d: 0.2)))
    assert "folds" not in m and m["test_days"] >= C.LEARN_MIN_TEST_DAYS and m["adopted"], m
    print("ok  D. adoption sur 3 coupes successives : majorité ET ensemble des jours ; peu de jours : coupe unique")


def main():
    test_aging()
    test_side()
    test_fine_correction()
    test_combo_prudence()
    test_folds()
    print("tests apprentissage v2 : OK")


if __name__ == "__main__":
    main()
