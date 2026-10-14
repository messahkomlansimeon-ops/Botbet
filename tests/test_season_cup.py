"""Voie `saison` étendue aux coupes (hors ligne, données fictives) : l'attente d'un match de coupe = profil de championnat de A x profil de championnat de B, chacun rapporté à la moyenne de
SA ligue, facteur domicile neutre 1,10, au moins 4 matchs de championnat par équipe, jambes étiquetées `saison` avec cup = true, séparées au bilan, jamais apprises.
Lancer : python -m tests.test_season_cup"""
import math
import os
import tempfile
from datetime import date, datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-season-cup-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-season-cup-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)

from tests.test_season import league, row  # noqa: E402
from tests import test_season_lane as T  # noqa: E402
from kelly import config as C, fair, lanestats as L, season, seasonlane as SL, store, strength  # noqa: E402

ON = date(2026, 10, 14)
CUP = "soccer_uefa_champs_league"


def rename(obs, prefix):
    return [dict(o, team=prefix + o["team"]) for o in obs]


def two_leagues(k=12):
    """Deux championnats fictifs : X (beaucoup de buts et de corners) et Y (peu), équipes aux noms distincts (X0.., Y0..)."""
    x = rename(league(10, 5, seed=1, mean_h=1.9, mean_a=1.5, corners=(6.5, 5.5), disp=0.1), "Xe")
    y = rename(league(10, 5, seed=2, mean_h=1.2, mean_a=0.9, corners=(4.5, 3.5), disp=0.1), "Yo")
    return {"soccer_x": season.fit(x, ON, "soccer_x", k=k), "soccer_y": season.fit(y, ON, "soccer_y", k=k)}


def test_formula():
    models = two_leagues(k=1e9)                       # k énorme : toute équipe = la moyenne de sa ligue (profil relatif 1)
    p = season.cup_predict(models, "XeT1", "YoT3")
    assert p and p["cup"] is True and p["lane"] == "saison" and p["rho"] == 0.0
    gx, gy = models["soccer_x"]["stats"]["goals"], models["soccer_y"]["stats"]["goals"]
    level = 0.5 * (0.5 * (gx["lg_h"] + gx["lg_a"]) + 0.5 * (gy["lg_h"] + gy["lg_a"]))
    assert abs(p["lh"] * p["la"] - level * level) < 1e-6, "le produit des deux attentes = niveau moyen des deux ligues au carré (facteur domicile réparti à parts égales)"
    assert abs(p["lh"] / p["la"] - 1.10) < 1e-6, "facteur domicile neutre 1,10 (domicile / extérieur)"
    c = p["stats"]["corners"]
    assert abs(c["h"] / c["a"] - 1.10) < 1e-6
    # k réel : le profil relatif de CHAQUE équipe vient de SA ligue (une équipe forte de la ligue faible reste forte)
    models = two_leagues(k=3)
    sa, sb = models["soccer_x"]["stats"]["goals"], models["soccer_y"]["stats"]["goals"]
    ta, tb = sa["t"]["XeT1"], sb["t"]["YoT3"]
    att_a, def_a = 0.5 * (ta["ah"] / sa["lg_h"] + ta["aa"] / sa["lg_a"]), 0.5 * (ta["dh"] / sa["lg_a"] + ta["da"] / sa["lg_h"])
    att_b, def_b = 0.5 * (tb["ah"] / sb["lg_h"] + tb["aa"] / sb["lg_a"]), 0.5 * (tb["dh"] / sb["lg_a"] + tb["da"] / sb["lg_h"])
    lvl = 0.5 * (0.5 * (sa["lg_h"] + sa["lg_a"]) + 0.5 * (sb["lg_h"] + sb["lg_a"]))
    p = season.cup_predict(models, "XeT1", "YoT3")
    assert abs(p["lh"] - lvl * att_a * def_b * math.sqrt(1.10)) < 1e-9 and abs(p["la"] - lvl * att_b * def_a / math.sqrt(1.10)) < 1e-9, (p["lh"], p["la"])
    # dispersion : moyenne des deux ligues ; grille et lignes lisibles comme un match de championnat
    assert abs(p["stats"]["corners"]["al"] - 0.5 * (models["soccer_x"]["stats"]["corners"]["alpha"] + models["soccer_y"]["stats"]["corners"]["alpha"])) < 1e-12
    g = strength.grid(p)
    assert abs(sum(g["1x2"].values()) - 1) < 1e-9 and "corners" in g
    fams = {x["market"] for x in strength.lines(p, "XeT1", "YoT3")}
    assert {"h2h", "totals", "btts", "alternate_totals_corners"} <= fams
    print("ok  coupe : attente = profil de championnat de A x de B rapportés à leur ligue x niveau moyen des deux ligues, domicile / extérieur = 1,10, dispersion moyenne des deux ligues")


def test_eligibility():
    models = two_leagues()
    assert season.cup_predict(models, "XeT1", "Inconnu FC") is None and season.cup_predict(models, "XeT1", "XeT1") is None
    # un championnat de la même ligue (coupe nationale entre deux équipes de X) est possible
    p = season.cup_predict(models, "XeT1", "XeT4")
    assert p and p["info"]["cup_leagues"] == ["soccer_x", "soccer_x"]
    # moins de 4 matchs de championnat : pas de prévision
    few = rename(league(10, 5, seed=1), "Xe") + [row("Zed", "2026-09-20", 1, goals=(1, 1), corners=(5, 5)), row("Zed", "2026-09-27", 0, goals=(0, 1), corners=(5, 5)),
                                                  row("XeT0", "2026-09-20", 0, goals=(1, 1), corners=(5, 5))]
    m2 = dict(models, soccer_x=season.fit(few, ON, "soccer_x"))
    assert season.cup_predict(m2, "Zed", "YoT3") is None, "2 matchs de championnat"
    # nom ambigu entre deux ligues : refusé
    twin = dict(models, soccer_y=season.fit(rename(league(10, 5, seed=2), "Xe"), ON, "soccer_y"))
    assert season.cup_predict(twin, "XeT1", "XeT2") is None, "équipes ambiguës : aucune prévision"
    assert season.locate(twin, "XeT1") is None, "même équipe dans deux ligues : ambigu"
    print("ok  éligibilité : équipe inconnue, 2 matchs de championnat et nom ambigu refusés ; coupe nationale entre deux équipes de la même ligue possible")


def test_make_and_lane():
    scan = T.setup()
    models_obs = {"soccer_x": rename(league(10, 5, seed=1, mean_h=1.9, mean_a=1.5, corners=(6.5, 5.5), disp=0.1), "Xe"),
                  "soccer_y": rename(league(10, 5, seed=2, mean_h=1.2, mean_a=0.9, corners=(4.5, 3.5), disp=0.1), "Yo")}
    season.collect = lambda sk, on, loaders=None, docs=None: (models_obs.get(sk, []), "football-data" if sk in models_obs else None)
    SL.known_leagues = lambda _filtered=True: ["soccer_x", "soccer_y"]
    ko = T.KO
    scan = {"leagues": {CUP: [{"id": "c1", "sport_key": CUP, "home_team": "XeT1", "away_team": "YoT3", "commence_time": ko, "league": "LDC"},
                              {"id": "c2", "sport_key": CUP, "home_team": "YoT2", "away_team": "XeT5", "commence_time": ko, "league": "LDC"},
                              {"id": "c3", "sport_key": CUP, "home_team": "Inconnu", "away_team": "XeT5", "commence_time": ko, "league": "LDC"}]}}
    pronos = SL.make(scan, T.NOW, T.DAY)
    assert pronos and all(c["cup"] is True and c["key"].endswith("|saison") and c["id"] in ("c1", "c2") for c in pronos), "jambes de coupe marquées cup, match inconnu écarté"
    legs, _ = SL.run(scan, [], T.DAY, T.NOW, fetch=T.fetch_of(pronos, over_odds=2.4, under_odds=1.62), history={})
    assert legs and all(c["cup"] is True and c["strategy"] == "saison" for c in legs), legs[:1]
    log = store.load(SL.LOG, [])
    assert log and all(r.get("cup") for r in log if r.get("sval") is not None), "le journal garde cup"
    # jamais apprises : ni poids ni calibration
    for r in log:
        r["result"] = "win"
    assert SL.learn(log) == 0 and store.load(SL.WEIGHTS_FILE, None) in (None, {})
    C.SEASON_LEAGUES = frozenset({"soccer_x"})
    try:
        assert SL.make(scan, T.NOW, T.DAY) == [], "SEASON_LEAGUES vide la coupe : seules les ligues listées"
    finally:
        C.SEASON_LEAGUES = frozenset()
    C.SEASON_CUPS = frozenset()
    try:
        assert SL.make(scan, T.NOW, T.DAY) == [], "coupe retirée de SEASON_CUPS : plus rien (ce n'est pas un championnat)"
    finally:
        C.SEASON_CUPS = frozenset(C.SEASON_CUPS) | {CUP}
    print("ok  jambes de coupe : cup = true, étiquette saison, journal, jamais apprises ; SEASON_LEAGUES / SEASON_CUPS respectés")


def test_report_split():
    day = date.today().isoformat()
    mk = lambda i, cup, res: {"key": f"s{i}|alternate_totals_corners||9.5|Over|saison", "id": f"m{i}", "date": day, "market": "alternate_totals_corners", "sport_key": "soccer_epl",
                              "strategy": "saison", "sval": 1, "result": res, "xodds": 2.2, "pe": 0.5, "pm": 0.5, "xclose": {"odds": 2.0, "lead_min": 60, "susp": False}} | ({"cup": True} if cup else {})   # noqa: E731
    store.save(SL.LOG, [mk(1, False, "win"), mk(2, False, "loss"), mk(3, True, "win"), mk(4, True, "win")])
    store.save(fair.COMBOS_FILE, [{"date": day, "tier": "ss3", "min_odds": 3.0, "prob": 0.3, "result": "win", "has_value": True,
                                   "legs": [{"key": "a|totals||2.5|Over|saison", "id": "a", "strategy": "saison", "sval": 1, "result": "win", "xodds": 2.0, "prob": 0.5},
                                            {"key": "b|totals||2.5|Over|saison", "id": "b", "strategy": "saison", "sval": 1, "cup": True, "result": "loss", "xodds": 2.0, "prob": 0.5}]}])
    L._memo["key"] = None
    lines = L.lane_lines([], None)
    a = next(x for x in lines if x.startswith("saison :"))
    c = next(x for x in lines if x.startswith("saison (coupes) :"))
    assert "1 gagnée sur 1" in a and "valeur : 1/2" in a, a
    assert "0 gagnée sur 1" in c and "valeur : 2/2" in c, c
    print("ok  bilan : les jambes de coupe ont leur ligne « saison (coupes) », jamais mêlées à celle des championnats")


def main():
    real = (season.collect, SL.known_leagues)
    try:
        test_formula()
        test_eligibility()
        test_make_and_lane()
        test_report_split()
    finally:
        season.collect, SL.known_leagues = real
    print("tests voie saison (coupes) : OK")


if __name__ == "__main__":
    main()
