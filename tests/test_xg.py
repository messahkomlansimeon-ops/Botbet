"""Tests hors ligne de l'xG par match (fmmatch -> profils -> features). Lancer : python -m tests.test_xg"""
import json
import os
import shutil
import tempfile
from datetime import timedelta

TMP = tempfile.mkdtemp(prefix="kelly-xg-test-")
os.environ["DATA_DIR"] = os.path.join(TMP, "data")
os.environ["SOURCES_CACHE_DIR"] = os.path.join(TMP, "cache")
os.environ["ODDS_CACHE_DIR"] = os.path.join(TMP, "odds")
os.environ.pop("TELEGRAM_TOKEN", None)

from kelly import config as C, store  # noqa: E402
from kelly.sources import features, fmmatch  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "sources")
SK = "soccer_epl"


def close(a, b, tol=0.011):
    return abs(a - b) <= tol


def fresh():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(os.path.join(TMP, "data"))
    store.DATA_DIR = os.path.join(TMP, "data")
    features.reload()


def page(mutate=None):
    data = json.load(open(os.path.join(FIX, "fotmob_match.json"), encoding="utf-8"))
    if mutate:
        mutate(data)
    inner = json.dumps({"props": {"pageProps": data}}, ensure_ascii=False)
    return f'<html><script id="__NEXT_DATA__" type="application/json">{inner}</script></html>'


def set_stat(data, key, val):
    for grp in data["content"]["stats"]["Periods"]["All"]["stats"]:
        for x in grp["stats"]:
            if x["key"] == key:
                x["stats"] = val


def add(d, mid, ago, home, away, goals, xg, npxg=None):
    """Enregistre un match synthétique (équipes Arsenal 1 / Chelsea 2...) ; `xg` : [domicile, extérieur] ou None."""
    s = {"goals": list(goals), "shots": [10, 10]}
    if xg:
        s["xg"] = list(xg)
    if npxg:
        s["npxg"] = list(npxg)
    day = str(fmmatch._today() - timedelta(days=ago))
    m = {"id": str(mid), "d": day, "home": home[1], "away": away[1], "hid": str(home[0]), "aid": str(away[0]), "s": s}
    assert fmmatch.record(d, m, SK)


ARS, CHE = (1, "Arsenal"), (2, "Chelsea")
OTH = (3, "Everton")


def table(**teams):
    """Faux classement de saison FotMob injecté dans le cache de features (clé « fm »)."""
    features._cache["fm"] = {"leagues": {SK: {"teams": teams}}}


# --- lecture des pages ---------------------------------------------------------

def test_parse_xg():
    m = fmmatch.parse_match(page())
    assert m["s"]["xg"] == [1.62, 1.6] and m["s"]["npxg"] == [1.62, 1.6], m["s"]
    assert m["s"]["goals"] == [1, 1] and m["s"]["throws"] == [11, 20]            # les autres stats ne bougent pas
    # xG absent : le match reste lisible, sans xg
    m = fmmatch.parse_match(page(lambda d: set_stat(d, "expected_goals", None)))
    assert m and "xg" not in m["s"] and "npxg" in m["s"]
    # 0.00 - 0.00 alors qu'il y a des buts : suspect, traité comme manquant (npxg aussi)
    def zero(d):
        set_stat(d, "expected_goals", ["0.00", "0.00"])
        set_stat(d, "expected_goals_non_penalty", ["0.00", "0.00"])
    m = fmmatch.parse_match(page(zero))
    assert m and "xg" not in m["s"] and "npxg" not in m["s"] and m["s"]["goals"] == [1, 1]
    # valeur non numérique ou arrondie à 2 décimales
    m = fmmatch.parse_match(page(lambda d: (set_stat(d, "expected_goals", ["1.6249", "n/a"]))))
    assert "xg" not in m["s"]
    m = fmmatch.parse_match(page(lambda d: set_stat(d, "expected_goals", ["1.6249", "0.8"])))
    assert m["s"]["xg"] == [1.62, 0.8]


def test_record_and_settle_unchanged():
    fresh()
    d = fmmatch.load()
    m = fmmatch.parse_match(page())
    m["id"] = "7"
    assert fmmatch.record(d, m, SK)
    row = d["teams"][m["hid"]]["m"][0]
    assert len(row) == 2 + 2 * len(fmmatch.KEYS) and row[-4:] == [1.62, 1.6, 1.62, 1.6]
    assert d["teams"][m["aid"]]["m"][0][-4:] == [1.6, 1.62, 1.6, 1.62]             # vue de l'équipe extérieure : inversée
    assert "xg" in d["recent"]["7"]["s"]


# --- stockage ancien format, profils -------------------------------------------

def test_old_rows_still_load():
    fresh()
    day = fmmatch._d2i(str(fmmatch._today()))
    old = [[day - i, i % 2] + [2, 1, 10, 9, 5, 4, 6, 5, 10, 11, 2, 1, 12, 10, 3, 2] for i in range(6)]      # 8 stats seulement (ancien format)
    assert len(old[0]) == 2 + 16
    d = {"teams": {"1": {"nm": "Arsenal", "sk": [SK], "d": str(fmmatch._today()), "m": old}}, "seen": {}, "recent": {}, "v": 1}
    store.save(fmmatch.FILE, d, compact=True)
    p = fmmatch.profiles()["teams"]["Arsenal"]
    assert p["all"]["n"] == 6 and p["all"]["goals_for"] == 2 and p["all"]["throws_for"] == 12
    assert "xg_for" not in p["all"] and p["all"]["xg_n"] == 0
    # un match neuf s'ajoute à côté des anciennes lignes courtes
    d = fmmatch.load()
    add(d, 99, 0, ARS, OTH, (2, 0), (1.8, 0.4))
    fmmatch.save(d)
    p = fmmatch.profiles()["teams"]["Arsenal"]
    assert p["all"]["n"] == 7 and p["all"]["xg_n"] == 1 and p["all"]["goals_for"] > 0


def test_profiles_xg():
    fresh()
    d = fmmatch.load()
    for i, (h, a, xg) in enumerate([(ARS, CHE, (2.0, 1.0)), (CHE, ARS, (0.5, 1.5)), (ARS, OTH, (3.0, 0.2)), (OTH, ARS, (1.0, 2.5))]):
        add(d, i + 1, 10 - i, h, a, (1, 1), xg, npxg=(xg[0] - 0.1, xg[1]))
    fmmatch.save(d)
    p = fmmatch.profiles()["teams"]
    ars = p["Arsenal"]                                         # 4 matchs >= APISTATS_MIN_N (3) : servi
    assert ars["all"]["n"] == 4 and ars["all"]["xg_n"] == 4
    assert close(ars["all"]["xg_for"], (2.0 + 1.5 + 3.0 + 2.5) / 4) and close(ars["all"]["xg_against"], (1.0 + 0.5 + 0.2 + 1.0) / 4)
    assert close(ars["home"]["xg_for"], 2.5) and close(ars["away"]["xg_for"], 2.0) and ars["home"]["xg_n"] == 2
    assert close(ars["all"]["npxg_for"], (1.9 + 1.5 + 2.9 + 2.5) / 4)
    assert "Chelsea" not in p                                  # 2 matchs seulement : pas servi (APISTATS_MIN_N)


def test_profile_min_count_for_xg():
    fresh()
    d = fmmatch.load()
    for i in range(5):                                         # 5 matchs dont un seul avec xG : moyenne de xG non servie
        add(d, 10 + i, 20 - i, ARS if i % 2 else OTH, OTH if i % 2 else ARS, (1, 0), (1.0, 0.5) if i == 0 else None)
    fmmatch.save(d)
    a = fmmatch.profiles()["teams"]["Arsenal"]["all"]
    assert a["n"] == 5 and a["xg_n"] == 1 and "xg_for" not in a and "xg_against" not in a, a


# --- features ------------------------------------------------------------------

def season_and_matches(n_xg=6, ars_goals=(2, 1), che_goals=(1, 2)):
    """Arsenal : xG 2.0 pour / 1.0 contre ; Chelsea : 1.0 pour / 2.0 contre, sur `n_xg` matchs avec xG (plus 6 - n_xg sans)."""
    fresh()
    d = fmmatch.load()
    mid = 100
    for i in range(6):
        for team, other, gf, ga, xf, xa in ((ARS, OTH, *ars_goals, 2.0, 1.0), (CHE, OTH, *che_goals, 1.0, 2.0)):
            home = i % 2 == 0
            h, a = (team, other) if home else (other, team)
            goals = (gf, ga) if home else (ga, gf)
            xg = (xf, xa) if home else (xa, xf)
            mid += 1
            add(d, mid, 30 - i, h, a, goals, xg if i < n_xg else None)
    fmmatch.save(d)
    features.reload()
    table(Arsenal={"n": 10, "xg": 15.0, "xga": 10.0}, Chelsea={"n": 10, "xg": 12.0, "xga": 14.0})


def test_features_prefers_match_xg():
    season_and_matches()
    f = features.match_features("Arsenal", "Chelsea", SK)
    assert f["xg_src"] == "fotmob-matchs", f
    assert close(f["exp_xg_home"], 2.0) and close(f["exp_xg_away"], 1.0) and close(f["exp_xg_total"], 3.0), f
    assert {"exp_xg_home", "exp_xg_away", "exp_xg_total"} <= set(f["aps_keys"]) and f["aps_q"] > 0
    assert "FotMob (matchs)" in f["sources"] and "FotMob" not in f["sources"], f["sources"]
    assert "exp_shots" in f                                                      # les autres stats sont toujours remplies


def test_features_falls_back_to_season_table():
    season_and_matches(n_xg=3)                                  # 3 matchs avec xG < XG_MIN_MATCHES (4)
    f = features.match_features("Arsenal", "Chelsea", SK)
    assert f["xg_src"] == "fotmob-classement", f
    assert close(f["exp_xg_home"], (1.5 + 1.4) / 2) and close(f["exp_xg_away"], (1.2 + 1.0) / 2), f
    assert "FotMob" in f["sources"]
    assert not {"exp_xg_home", "exp_xg_total"} & set(f.get("aps_keys") or ())  # le classement n'est pas un profil de matchs
    # aucun xG du tout, aucun classement : pas d'exp_xg_*
    season_and_matches(n_xg=0)
    features._cache["fm"] = {}
    f = features.match_features("Arsenal", "Chelsea", SK)
    assert "exp_xg_total" not in f and "xg_src" not in f


def test_xg_min_matches_respected():
    season_and_matches(n_xg=4)
    assert features.match_features("Arsenal", "Chelsea", SK)["xg_src"] == "fotmob-matchs"      # 4 = XG_MIN_MATCHES : suffit
    old = C.XG_MIN_MATCHES
    try:
        C.XG_MIN_MATCHES = 5
        assert features.match_features("Arsenal", "Chelsea", SK)["xg_src"] == "fotmob-classement"
        C.XG_MIN_MATCHES = 2
        assert features.match_features("Arsenal", "Chelsea", SK)["xg_src"] == "fotmob-matchs"
    finally:
        C.XG_MIN_MATCHES = old
    # une seule équipe suffisamment fournie en xG ne suffit pas
    season_and_matches()
    d = fmmatch.load()
    for tid in ("2",):
        for r in d["teams"][tid]["m"]:
            r[-4:] = [None] * 4
    fmmatch.save(d)
    features.reload()
    table(Arsenal={"n": 10, "xg": 15.0, "xga": 10.0}, Chelsea={"n": 10, "xg": 12.0, "xga": 14.0})
    assert features.match_features("Arsenal", "Chelsea", SK)["xg_src"] == "fotmob-classement"


def test_small_sample_regresses_to_goals():
    """4 matchs avec xG (XG_FULL_N = 6) : 4/6 d'xG et 2/6 de buts, les buts étant ici 0."""
    season_and_matches(n_xg=4, ars_goals=(0, 0), che_goals=(0, 0))
    assert C.XG_FULL_N == 6
    f = features.match_features("Arsenal", "Chelsea", SK)
    assert f["xg_src"] == "fotmob-matchs"
    # Arsenal pour 2.0 -> 4/6 * 2 ; Chelsea contre 2.0 -> 4/6 * 2 ; moyenne 1.33 ; extérieur : 1.0 -> 0.67
    assert close(f["exp_xg_home"], 4 / 3) and close(f["exp_xg_away"], 2 / 3), f


def test_understat_untouched():
    """Un xG d'Understat (couverture complète) n'est pas écrasé ; la clé xg_src le dit."""
    season_and_matches()
    out = {"sources": ["understat"], "exp_xg_home": 1.1, "exp_xg_away": 0.9, "exp_xg_total": 2.0, "xg_src": "understat"}
    features._fmmatch(out, "Arsenal", "Chelsea", SK)
    assert out["exp_xg_total"] == 2.0 and out["xg_src"] == "understat"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    try:
        for t in tests:
            t()
            print(f"ok  {t.__name__}")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{len(tests)} tests xg : OK")


if __name__ == "__main__":
    main()
