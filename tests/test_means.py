"""Correction des moyennes (hors ligne) : attendu (« mu », posé à la création du pronostic) contre réel (« val », posé au règlement), une fois par
match et marché ; facteur rapproché de 1 tant qu'il y a peu de matchs, borné, vieilli ; appliqué aux moyennes avant le calcul des chances (profils et
modèle approfondi, avec son match moyen) ; 📊 Marchés : détail plus / moins et ligne de la moyenne. Lancer : python -m tests.test_means"""
import os
import tempfile
from datetime import date

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-means-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)
os.environ["QUIET_HOURS"] = "off"

from kelly import calib, config as C, ensemble, fair, markets, means, settle, store  # noqa: E402

DAY = date(2026, 10, 13)


def row(i, mu, val, market="team_shots_total", name="Over", point=24.5, result="win", day="2026-10-12"):
    return {"key": f"m{i}|{market}||{point}|{name}", "id": f"m{i}", "market": market, "name": name, "point": point, "date": day, "mu": mu,
            "val": val, "result": result}


def reset():
    store.save(means.FILE, {})
    means.reset()


def test_learn_and_factor():
    reset()
    assert means.factor("shots") == 1.0 and fair.ROW == ensemble.ROW and "mu" in fair.ROW
    rows = [row(i, 23.0, 25.0) for i in range(40)]
    rows += [row(i, 23.0, 25.0, name="Under", point=26.5) for i in range(40)]          # même match, même marché, autre ligne : compté une fois
    rows += [row(99, 10.0, 12.0, market="alternate_totals_corners"), row(98, 2.0, 3.0, market="totals")]   # les buts : jamais
    rows += [row(97, 23.0, 30.0, result="unknown"), row(96, None, 25.0), row(95, 23.0, None)]                # non réglé, sans attendu ou sans réel
    assert means.learn(rows, DAY) == 41 and means.learn(rows, DAY) == 0, "une fois par match et marché"
    d = means.load()
    assert d["fam"]["shots"] == [40.0, 1000.0, 920.0] and d["fam"]["corners"] == [1.0, 12.0, 10.0], d["fam"]
    k = means.factor("shots")
    assert abs(k - (1 + (1000 / 920 - 1) * 40 / 80)) < 1e-9, k                      # 25 / 23 = 1,087, à moitié cru avec 40 matchs : 1,043
    assert 1 < means.factor("corners") < 1.01, "1 match : à peine bougé"
    store.save(means.FILE, {"fam": {"shots": [5000, 2000.0, 1000.0], "fouls": [5000, 500.0, 1000.0]}, "done": {}})
    means.reset()
    assert means.factor("shots") == means.BOUNDS[1] and means.factor("fouls") == means.BOUNDS[0]     # jamais plus de +25 % ni -20 %
    C.MEANS_CORRECT = False
    try:
        assert means.factor("shots") == 1.0
    finally:
        C.MEANS_CORRECT = True
    store.save(means.FILE, {"fam": {"shots": [10.0, 250.0, 230.0]}, "done": {}, "t": "2026-07-15"})
    means.reset()
    means.learn([row(1, 23.0, 25.0, day="2026-10-13")], DAY)
    acc = means.load()["fam"]["shots"]
    assert abs(acc[0] - (10 * 0.5 + 1)) < 0.01, acc                                 # 90 jours plus tard : les anciens matchs comptent moitié moins
    print("ok  correction des moyennes : une fois par match et marché, jamais les buts ; rapprochée de 1, bornée, vieillie ; désactivable")


def test_applied_to_means():
    reset()
    f = {"exp_shots": 23.0, "n_home": 10, "n_away": 10}
    p_before = {(o[0], o[1], o[2]): o[3] for o in fair._stat_options(f, "H", "A") if o[0] == "team_shots_total"}
    assert fair.raw_mean(f, "team_shots_total") == 23.0 and fair.raw_mean(f, "totals") is None
    store.save(means.FILE, {"fam": {"shots": [4000, 4000 * 25.3, 4000 * 23.0]}, "done": {}})
    means.reset()
    k = means.factor("shots")
    assert 1.09 < k < 1.10, k
    opts = [o for o in fair._stat_options(f, "H", "A") if o[0] == "team_shots_total"]
    assert all(f"{23.0 * k:.1f} tirs attendus" in o[4] for o in opts), opts[0]          # moyenne corrigée, dite dans la justification
    common = [key for key in p_before if key in {(o[0], o[1], o[2]) for o in opts}]
    after = {(o[0], o[1], o[2]): o[3] for o in opts}
    assert common and all(after[x] > p_before[x] for x in common if x[1] == "Over") and all(after[x] < p_before[x] for x in common if x[1] == "Under")
    assert fair.raw_mean(f, "team_shots_total") == 23.0, "l'attendu posé sur le pronostic reste brut"
    pred = {"q": 1, "stats": {"shots": {"h": 12.0, "a": 11.0, "al": 0.1, "at": 0.05}, "goals": {"h": 1.4, "a": 1.1}},
            "base": {"stats": {"shots": {"h": 12.5, "a": 11.5}}}}
    sp = means.scaled(pred)
    assert abs(sp["stats"]["shots"]["h"] - 12.0 * k) < 1e-9 and abs(sp["base"]["stats"]["shots"]["a"] - 11.5 * k) < 1e-9 and sp["stats"]["shots"]["al"] == 0.1
    assert sp["stats"]["goals"] == pred["stats"]["goals"] and pred["stats"]["shots"]["h"] == 12.0, "les buts et l'original intacts"
    reset()
    assert means.scaled(pred) is pred
    print("ok  moyennes corrigées avant le calcul des chances (profils, modèle approfondi et son match moyen) : plus monte, moins baisse ; l'attendu posé reste brut")


def test_league_factor():
    reset()
    rows = [row(i, 23.0, 25.0) | {"sport_key": "soccer_epl"} for i in range(60)]
    rows += [row(100 + i, 23.0, 28.0) | {"sport_key": "soccer_xb_argentina_2"} for i in range(30)]
    rows += [row(200 + i, 23.0, 30.0) | {"sport_key": "soccer_xb_bolivia"} for i in range(2)]
    assert means.learn(rows, DAY) == 92
    kf = means.factor("shots")
    ka, kb = means.factor("shots", "soccer_xb_argentina_2"), means.factor("shots", "soccer_xb_bolivia")
    assert abs(ka - (kf + (28 / 23 - kf) * 30 / 60)) < 1e-9, (kf, ka)              # 30 matchs : à moitié son propre écart, à moitié celui de la famille
    assert kf < kb < kf + 0.03 and means.factor("shots", "soccer_nowhere") == kf, (kf, kb)     # 2 matchs : presque la famille ; inconnu : la famille
    f = {"exp_shots": 23.0, "n_home": 10, "n_away": 10, "sk": "soccer_xb_argentina_2"}
    assert f"{23.0 * ka:.1f} tirs attendus" in next(o for o in fair._stat_options(f, "H", "A") if o[0] == "team_shots_total")[4]
    pred = {"q": 1, "stats": {"shots": {"h": 12.0, "a": 11.0, "al": 0.1, "at": 0.05}}}
    assert abs(means.scaled(pred, "soccer_xb_argentina_2")["stats"]["shots"]["h"] - 12.0 * ka) < 1e-9
    lines = means.league_lines("shots")
    assert len(lines) == 1 and lines[0].endswith("28,0 réels pour 23,0 attendus (30 matchs) → ×" + f"{ka:.2f}".replace(".", ",")), lines     # Bolivie : trop peu
    print("ok  correction par championnat : rapprochée de celle de la famille tant qu'il a peu de matchs ; appliquée aux moyennes ; affichée dès 15 matchs")


def test_settlement_sets_actual():
    b = {"market": "team_shots_total", "home": "H", "away": "A", "group": "soccer", "who": None}
    fst = {"side": {"home": {"shots": 14}, "away": {"shots": 12}}}
    assert settle.stat_actual(b, fst) == 26.0 and settle.stat_actual(b | {"market": "alternate_totals_corners"}, {"corners": 9}) == 9.0
    assert settle.stat_actual(b | {"market": "totals"}, fst) is None and settle.stat_actual(b, {}) is None
    print("ok  au règlement : le nombre réel du chiffre du pari (match ou équipe), jamais pour les buts")


def test_market_card_lines():
    store.save(calib.FILE, {"shots": [67, 30, 43.5], "_cell": {"shots|plus": [22, 16, 14.1], "shots|moins": [23, 9, 14.7], "shots|plus|1": [5, 3, 3.2],
                                                              "goals|plus": [51, 39, 33.2]}})
    calib._memo.clear()
    store.save(means.FILE, {"fam": {"shots": [140, 140 * 25.0, 140 * 23.1]}, "done": {}})
    means.reset()
    assert markets.sides_line("shots") == "↕️ moins ⚠️ 39 % / 64 % (23) · plus 73 % / 64 % (22)", markets.sides_line("shots")
    assert markets.sides_line("cards") == ""
    line = means.line("shots")
    assert line == "moyenne : 23,1 attendus, 25,0 réels (140 matchs) → corrigée ×1,06", line
    card = str(markets.card())
    assert "<i>↕️ moins ⚠️ 39 % / 64 % (23) · plus 73 % / 64 % (22)</i>" in card and "<i>moyenne : 23,1 attendus, 25,0 réels (140 matchs)" in card, card
    print("ok  📊 Marchés : détail plus / moins (⚠️ sens 10 points sous l'annoncé) et correction de la moyenne")


def main():
    test_learn_and_factor()
    test_applied_to_means()
    test_league_factor()
    test_settlement_sets_actual()
    test_market_card_lines()
    print("tests correction des moyennes : OK")


if __name__ == "__main__":
    main()
