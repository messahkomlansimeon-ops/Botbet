"""Victoire (1N2) et buts d'une équipe dans les pronostics (kelly/fair.py), lus chez 1xBet, réglés au score, suivis en direct, appris par famille ;
paris probables mais mal payés : montrés à part dans l'analyse d'un match, bouche-trous signalés d'un « combi N min M ». Hors ligne.
Lancer : python -m tests.test_win_team_goals"""
from tests import test_combi_track as T     # noqa: I001 - fixe DATA_DIR et les faux réseaux avant d'importer kelly
from tests import test_combi_kinds as K     # noqa: E402
from kelly import app, calib, combos as CB, fair, fmt, settle, store, watch  # noqa: E402
from kelly.sources import xbet  # noqa: E402


def test_markets():
    assert fmt.label("team_totals_home", "Over", 1.5, "Inter", "soccer") == "Inter : plus de 1,5 buts"
    assert fmt.label("team_totals_away", "Under", 0.5, "Parma", "soccer") == "Parma : moins de 0,5 but"
    assert fmt.label("h2h", "Inter", None, None, "soccer") == "Inter gagne"
    ev = {"scores": [{"name": "A", "score": "2"}, {"name": "B", "score": "1"}]}
    leg = lambda m, n, p: {"home": "A", "away": "B", "market": m, "name": n, "point": p}       # noqa: E731
    assert settle.settle_pick(leg("team_totals_home", "Over", 1.5), ev) == "win" and settle.settle_pick(leg("team_totals_away", "Over", 1.5), ev) == "loss"
    assert settle.settle_pick(leg("h2h", "A", None), ev) == "win" and settle.final_text(leg("team_totals_home", "Over", 1.5), ev) == "2-1"
    assert watch.state(leg("team_totals_away", "Under", 1.5), 2, 1, {}, True) == ("win", "2-1")
    assert watch.state(leg("team_totals_home", "Over", 2.5), 2, 1, {}, False)[0] == "live"
    assert calib.family("team_totals_home") == calib.family("team_totals_away") == "team_goals", "apprentissage : une famille à part"
    data = {"eventGroups": [{"events": [[{"type": 11, "cf": 1.4, "parameter": 1.5}, {"type": 14, "cf": 1.9, "parameter": 0.5}, {"type": 1, "cf": 1.3}]]}]}
    px = xbet.parse_main(data, "A", "B")[0]
    assert px[("team_totals_home", "Over", 1.5)] == 1.4 and px[("team_totals_away", "Under", 0.5)] == 1.9 and px[("h2h", "A", None)] == 1.3
    opts = fair._simple_options({"n_home": 10, "n_away": 10, "exp_goals_home": 2.4, "exp_goals_away": 0.8}, "A", "B")
    assert {"h2h", "team_totals_home", "team_totals_away"} <= {o[0] for o in opts}
    assert {"h2h", "team_totals_home"} <= set(fair.FREE_MARKETS), "suivis en silence : l'apprentissage les voit tous les jours"
    assert CB.kind_of("team_totals_home") == "b" and CB.kind_of("h2h") == "v"
    print("ok  victoire et buts d'une équipe : libellés, cotes 1xBet, règlement, suivi en direct, famille d'apprentissage")


def test_underpaid():
    K.setup()
    low = K.fouls(30, id="u0", key="u0|team_fouls_total||22.5|Over", prob=0.63, xodds=1.5, min_odds=1.67)
    assert app.underpaid(low) and not app.underpaid(K.fouls(0))
    txt = app.underpaid_lines([low, K.fouls(0)])
    assert "⚠️ Probables mais mal payés" in txt and "Plus de 22,5 fautes au total · Kelly 63 % · 1xBet 1,50 (juste 1,59)" in txt, txt
    assert app.underpaid_lines([low | {"xveto": 1}]) == "" and app.underpaid_lines([low | {"prob": 0.55}]) == ""
    assert app.underpaid_lines([low | {"xodds": 1.3}]) == "", "trop mal payé (plus de 8 % sous la juste valeur) : pas montré"
    # « au moins N » : les mal payés comblent le trou, nommés « mal payé »
    solid = {c["id"] for c in CB.fair_legs(K.world()) if c["market"].startswith("team_fouls_")}
    pool = K.world() + [K.fouls(40 + i, id=f"u{i}", key=f"u{i}|team_fouls_total||22.5|Over", prob=0.63, xodds=1.5, min_odds=1.67) for i in range(2)]
    store.save("today.json", {"day": K.DAY, "picks": [], "pronos": pool})
    n = len(solid) + 2
    with K.Fakes():
        out = str(app.custom_combo([], pool, 2.0, K.DAY, kinds=["f"], nlegs=(n, "min")))
    assert f"🧩 {n} paris" in out and "(63 %, mal payé)" in out, out
    kept = {c["key"] for c in app.keep_pronos(pool + [K.fouls(60 + i, id=f"z{i}", key=f"z{i}|x", prob=0.4) for i in range(80)])}
    assert {"u0|team_fouls_total||22.5|Over", "u1|team_fouls_total||22.5|Over"} <= kept, "gardés dans today.json pour compléter"
    print("ok  probables mais mal payés : montrés à part, jamais seuls, bouche-trous signalés d'un « min N », gardés pour la journée")


def main():
    test_markets()
    test_underpaid()
    print("tests victoire / buts d'une équipe / mal payés : OK")


if __name__ == "__main__":
    main()
