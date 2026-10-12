"""Tests hors ligne de l'xG d'API-Football (stats -> apistats -> features -> affichage). Lancer : python -m tests.test_apistats_xg"""
import json
import os
import random
import shutil
import tempfile
from datetime import datetime, timedelta, timezone

TMP = tempfile.mkdtemp(prefix="kelly-apsxg-test-")
os.environ["DATA_DIR"] = os.path.join(TMP, "data")
os.environ["SOURCES_CACHE_DIR"] = os.path.join(TMP, "cache")
os.environ["ODDS_CACHE_DIR"] = os.path.join(TMP, "odds")
os.environ["STATS_CACHE_DIR"] = os.path.join(TMP, "apistats")
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ.pop("STATS_DAILY_CAP", None)

from kelly import apistats, config as C, store, stats  # noqa: E402
from kelly.sources import features, fmmatch  # noqa: E402
from tests.fake_api import FakeFootballAPI, match as fake_match, ts_of  # noqa: E402

SK = "soccer_brazil_campeonato"
SAN, FLA, OPP = (128, "Santos"), (127, "Flamengo"), (900, "Vitoria")
DAY7, DAY8 = "2026-10-07", "2026-10-08"
REAL_TODAY = stats._today


def close(a, b, tol=0.011):
    return abs(a - b) <= tol


def fresh():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(os.path.join(TMP, "data"))
    store.DATA_DIR = os.path.join(TMP, "data")
    stats._today = REAL_TODAY
    features.reload()


def stat_list(corners=5, xg="absent", shots=10):
    """Statistiques d'une équipe comme /fixtures/statistics les renvoie ; xg : valeur de « expected_goals » ou « absent » (type non envoyé)."""
    out = [{"type": "Total Shots", "value": shots}, {"type": "Shots on Goal", "value": 4}, {"type": "Corner Kicks", "value": corners},
           {"type": "Fouls", "value": 11}, {"type": "Yellow Cards", "value": 2}, {"type": "Red Cards", "value": None},
           {"type": "Ball Possession", "value": "52%"}]
    if xg != "absent":
        out.append({"type": "expected_goals", "value": xg})
    return out


def resp(home, away, xg_h="absent", xg_a="absent", corners=(5, 4)):
    return [{"team": {"id": home[0]}, "statistics": stat_list(corners[0], xg_h)},
            {"team": {"id": away[0]}, "statistics": stat_list(corners[1], xg_a)}]


_ids = iter(range(1000, 100000))


def play(d, ago, home, away, goals, xg=None, corners=(5, 4)):
    """Enregistre un match (home, away : (id, nom)) joué il y a `ago` jours ; xg : (domicile, extérieur) renvoyés par l'API, ou None
    (statistique non envoyée). -> la ligne ajoutée à l'équipe à domicile."""
    day = datetime.fromisoformat(REAL_TODAY()).replace(tzinfo=timezone.utc) - timedelta(days=ago)
    e = {"id": next(_ids), "ts": int(day.timestamp()) + 20 * 3600, "home": home[1], "home_id": home[0], "away": away[1], "away_id": away[0],
         "gh": goals[0], "ga": goals[1], "lk": SK, "ln": "Brasileirão"}
    st = stats._entry_from(resp(home, away, *(xg or ("absent", "absent")), corners=corners))
    assert apistats.record(d, e, st) == 2
    return next(r for r in d["teams"][str(home[0])]["m"] if r[apistats.R_DAY] == apistats._d2i(datetime.fromtimestamp(e["ts"], tz=timezone.utc).date().isoformat()))


def fill_history(n_xg=6, n=6, santos=(2.0, 1.0), flamengo=(1.0, 2.0), goals_s=(2, 1), goals_f=(1, 2)):
    """Santos : xG 2.0 pour / 1.0 contre ; Flamengo : 1.0 pour / 2.0 contre, `n` matchs chacune contre Vitoria dont `n_xg` avec xG."""
    fresh()
    d = apistats.load()
    for i in range(n):
        has = i < n_xg
        for team, (gf, ga), (xf, xa) in ((SAN, goals_s, santos), (FLA, goals_f, flamengo)):
            home = i % 2 == 0
            h, a = (team, OPP) if home else (OPP, team)
            goals = (gf, ga) if home else (ga, gf)
            xg = ((xf, xa) if home else (xa, xf)) if has else None
            play(d, 30 - i, h, a, goals, xg)
    apistats.save(d)
    features.reload()
    return d


# --- lecture de expected_goals (stats) -----------------------------------------------------------------------------------------

def test_team_values_reads_expected_goals():
    fresh()
    xg = lambda v: stats._team_values({"statistics": stat_list(xg=v)}).get("xg")        # noqa: E731
    assert xg(1.62) == 1.62 and xg("1.62") == 1.62 and xg("1.6249") == 1.62 and xg(0) == 0.0 and xg("0.00") == 0.0
    assert xg(2) == 2.0
    for bad in (None, "", "n/a", "nan", "inf", -0.3, "-1", 55, "45%x"):                  # null, illisible, négatif ou absurde : inconnu
        assert xg(bad) is None, bad
    tv = stats._team_values({"statistics": stat_list()})                                  # type non envoyé : clé absente (jamais 0)
    assert "xg" not in tv and "xg" not in tv["nul"], tv
    tv = stats._team_values({"statistics": stat_list(xg=None)})
    assert "xg" not in tv and "xg" not in tv["nul"], tv
    # les autres valeurs (règlement) ne bougent pas, avec ou sans xG
    a, b = stats._team_values({"statistics": stat_list(xg=1.4)}), stats._team_values({"statistics": stat_list()})
    assert {k: v for k, v in a.items() if k != "xg"} == b, (a, b)
    ea, eb = stats._entry_from(resp(SAN, FLA, 1.4, "0.9")), stats._entry_from(resp(SAN, FLA))
    assert ea["teams"]["128"]["xg"] == 1.4 and ea["teams"]["127"]["xg"] == 0.9 and "xg" not in eb["teams"]["128"]
    for k in ("corners", "cards", "shots", "sot", "fouls"):                               # totaux du match (règlement) identiques
        assert ea[k] == eb[k], k
    assert "xg" not in ea                                                                  # pas de total d'xG : rien à régler


def test_fixture_stats_cache_and_no_extra_request():
    fresh()
    calls = []

    def fake_get(path, **kw):
        calls.append(path)
        return resp(SAN, FLA, "1.62", None)
    old = stats.get
    stats.get = fake_get
    try:
        st = stats.fixture_stats(4242, players=False)
        assert calls == ["/fixtures/statistics"], calls                                     # une seule requête, la même qu'avant
        assert st["teams"]["128"]["xg"] == 1.62 and "xg" not in st["teams"]["127"]
        again = stats.fixture_stats(4242, players=False)                                    # cache disque : zéro requête de plus
        assert len(calls) == 1 and again["teams"]["128"]["xg"] == 1.62
        cached = stats._stats_cache()["4242"]
        assert cached["teams"]["128"]["xg"] == 1.62
    finally:
        stats.get = old


# --- lignes de l'historique : nouvelles, anciennes, mélangées ---------------------------------------------------------------------

def test_record_row_format_and_unknown_xg():
    fresh()
    d = apistats.load()
    assert apistats.KEYS[-1] == "xg" and apistats.KEYS[:6] == ("goals", "shots", "sot", "corners", "fouls", "cards")
    base = apistats.R_STATS + 2 * 6                                                         # longueur d'une ligne sans xG
    row = play(d, 3, SAN, FLA, (2, 1), ("1.624", 0.8))
    assert len(row) == base + 2 and row[-2:] == [1.62, 0.8], row
    row = play(d, 4, SAN, FLA, (2, 1))                                                      # xG non envoyé : ligne courte, comme une ancienne
    assert len(row) == base, row
    row = play(d, 5, SAN, FLA, (2, 1), (1.1, None))                                         # un seul côté : inconnu pour les deux (jamais 0)
    assert len(row) == base, row
    row = play(d, 6, SAN, FLA, (2, 1), (0, 0))                                              # 0 - 0 alors qu'il y a des buts : non suivi
    assert len(row) == base, row
    row = play(d, 7, SAN, FLA, (0, 0), (0, 0))                                              # vrai 0-0 sans occasions : zéro cru
    assert len(row) == base + 2 and row[-2:] == [0, 0], row
    away = d["teams"]["127"]["m"][-1]                                                      # le match d'il y a 3 jours (le plus récent)
    assert away[-2:] == [0.8, 1.62] and away[apistats.R_HOME] == 0                          # vue de l'équipe extérieure : inversée
    # un entrée de cache écrite avant l'ajout de l'xG (clé « xg » absente) ne donne qu'une ligne courte
    old_entry = {"teams": {"128": {"corners": 6.0, "cards": 2.0, "shots": 10.0, "sot": 4.0, "fouls": 11.0, "yel": 2.0, "nul": []},
                           "127": {"corners": 4.0, "cards": 2.0, "shots": 9.0, "sot": 3.0, "fouls": 12.0, "yel": 2.0, "nul": []}}}
    e = {"id": 777, "ts": 1790000000, "home": "Santos", "home_id": 128, "away": "Flamengo", "away_id": 127, "gh": 1, "ga": 1, "lk": SK}
    assert apistats.record(d, e, old_entry) == 2
    dd = apistats._d2i(datetime.fromtimestamp(1790000000, tz=timezone.utc).date().isoformat())
    assert [len(r) for r in d["teams"]["128"]["m"] if r[apistats.R_DAY] == dd] == [base]
    # un match dont seul l'xG serait connu n'est pas ajouté (rien d'autre d'utilisable)
    only = {"teams": {"128": {"xg": 1.0, "nul": ["corners", "cards", "yel", "shots", "sot", "fouls"]},
                      "127": {"xg": 0.5, "nul": ["corners", "cards", "yel", "shots", "sot", "fouls"]}}}
    assert apistats.record(d, dict(e, id=778), only) == 0


def test_old_rows_still_load_and_mix():
    fresh()
    day = apistats._d2i(REAL_TODAY())
    old = [[day - 20 - i, 0, i % 2] + [2, 1, 10, 9, 5, 4, 6, 5, 10, 11, 2, 1] for i in range(1, 7)]      # 15 colonnes : ancien format, sans xG
    assert len(old[0]) == apistats.R_STATS + 2 * 6
    apistats.save({"teams": {"128": {"nm": "Santos", "sk": [SK], "lg": "Brasileirão", "d": REAL_TODAY(), "m": old}},
                   "lg": ["Brasileirão"], "seen": {}, "v": 2})
    p = apistats.profiles()["teams"]["Santos"]
    assert p["all"]["n"] == 6 and p["all"]["goals_for"] == 2 and p["all"]["corners_for"] == 6 and p["all"]["cards_against"] == 1
    assert p["all"]["xg_n"] == 0 and "xg_for" not in p["all"] and "xg_against" not in p["all"], p["all"]     # inconnu, jamais 0
    assert p["home"]["xg_n"] == 0 and p["away"]["xg_n"] == 0
    a = apistats.averages(128)
    assert a and a["n"] == 6 and a["corners_for"] == 6                                      # lecture corners / cartons inchangée
    f = features.match_features("Santos", "Flamengo", SK)
    assert "exp_xg_total" not in f and "xg_src" not in f
    # un fichier de l'ancien format n'est pas réécrit par une simple lecture
    raw = store.load(apistats.FILE, {})
    assert raw["teams"]["128"]["m"] == old
    # des matchs neufs avec xG s'ajoutent à côté des anciennes lignes ; l'xG ne compte que les matchs qui en portent (pas de dilution par 0)
    d = apistats.load()
    for i, x in enumerate(((1.0, 0.5), (2.0, 1.5), (3.0, 0.5), (2.0, 0.5))):
        play(d, 10 - i, SAN, FLA, (1, 1), x)
    apistats.save(d)
    p = apistats.profiles()["teams"]["Santos"]
    assert p["all"]["n"] == 10 and p["all"]["xg_n"] == 4, p["all"]
    assert close(p["all"]["xg_for"], 2.0) and close(p["all"]["xg_against"], 0.75), p["all"]
    assert p["all"]["goals_for"] > 0
    # une ligne ancienne tronquée ou abîmée ne casse pas la lecture
    d = apistats.load()
    d["teams"]["128"]["m"][0] = d["teams"]["128"]["m"][0][:10]
    apistats.save(d)
    assert apistats.profiles()["teams"]["Santos"]["all"]["xg_n"] == 4


def test_profiles_xg_views_and_min_count():
    fresh()
    d = apistats.load()
    for i, x in enumerate(((2.0, 1.0), (0.5, 1.5), (3.0, 0.2), (1.0, 2.5))):
        home = i % 2 == 0
        h, a = (SAN, OPP) if home else (OPP, SAN)
        play(d, 10 - i, h, a, (1, 1), x)
    apistats.save(d)
    s = apistats.profiles()["teams"]["Santos"]
    assert s["all"]["n"] == 4 and s["all"]["xg_n"] == 4
    assert close(s["all"]["xg_for"], (2.0 + 1.5 + 3.0 + 2.5) / 4) and close(s["all"]["xg_against"], (1.0 + 0.5 + 0.2 + 1.0) / 4)
    assert s["home"]["xg_n"] == 2 and s["away"]["xg_n"] == 2 and close(s["home"]["xg_for"], 2.5) and close(s["away"]["xg_for"], 2.0)
    # 5 matchs dont un seul avec xG : moyenne non servie (APISTATS_MIN_N), xg_n le dit
    fresh()
    d = apistats.load()
    for i in range(5):
        play(d, 20 - i, SAN, OPP, (1, 0), (1.0, 0.5) if i == 0 else None)
    apistats.save(d)
    a = apistats.profiles()["teams"]["Santos"]["all"]
    assert a["n"] == 5 and a["xg_n"] == 1 and "xg_for" not in a and "xg_against" not in a, a


# --- features : priorité des sources d'xG ---------------------------------------------------------------------------------------

def test_features_uses_api_football_xg_when_fotmob_missing():
    fill_history()
    f = features.match_features("Santos", "Flamengo", SK)
    assert f["xg_src"] == "api-football-matchs", f
    assert close(f["exp_xg_home"], 2.0) and close(f["exp_xg_away"], 1.0) and close(f["exp_xg_total"], 3.0), f
    assert {"exp_xg_home", "exp_xg_away", "exp_xg_total"} <= set(f["aps_keys"]) and f["aps_q"] > 0
    assert f["sources"] == ["API-Football"] and "exp_corners" in f and "exp_goals" in f          # les autres stats sont toujours remplies
    # xG des deux équipes lus par prematch.goal_means : sans modèle approfondi, ce sont les buts attendus du pari
    from kelly import prematch
    lh, la, label = prematch.goal_means(f)
    assert close(lh, 2.0) and close(la, 1.0) and label == "xG"


def test_api_football_min_matches_and_regression_to_goals():
    fill_history(n_xg=3)                                         # 3 matchs avec xG < XG_MIN_MATCHES (4)
    f = features.match_features("Santos", "Flamengo", SK)
    assert "exp_xg_total" not in f and "xg_src" not in f, f
    assert "exp_goals" in f                                      # les autres chiffres de l'équipe restent là
    fill_history(n_xg=4)                                         # 4 = XG_MIN_MATCHES : suffit
    assert features.match_features("Santos", "Flamengo", SK)["xg_src"] == "api-football-matchs"
    old = C.XG_MIN_MATCHES
    try:
        C.XG_MIN_MATCHES = 5
        assert "xg_src" not in features.match_features("Santos", "Flamengo", SK)
        C.XG_MIN_MATCHES = 2
        assert features.match_features("Santos", "Flamengo", SK)["xg_src"] == "api-football-matchs"
    finally:
        C.XG_MIN_MATCHES = old
    # une seule des deux équipes bien fournie en xG ne suffit pas
    d = fill_history()
    for r in d["teams"]["127"]["m"]:
        del r[-2:]
    apistats.save(d)
    features.reload()
    assert "xg_src" not in features.match_features("Santos", "Flamengo", SK)
    # 4 matchs avec xG sur XG_FULL_N = 6 : 4/6 d'xG et 2/6 de buts (ici 0)
    fill_history(n_xg=4, goals_s=(0, 0), goals_f=(0, 0))
    f = features.match_features("Santos", "Flamengo", SK)
    assert f["xg_src"] == "api-football-matchs" and C.XG_FULL_N == 6
    assert close(f["exp_xg_home"], 4 / 3) and close(f["exp_xg_away"], 2 / 3), f


def put_fotmob(n_xg, xg_s=(3.0, 0.5), xg_f=(0.4, 2.5)):
    """Historique FotMob (fmmatch) de Santos et Flamengo dans la même ligue, `n_xg` matchs avec xG, 6 matchs en tout."""
    d = fmmatch.load()
    for i in range(6):
        for k, (team, (xf, xa)) in enumerate(((SAN, xg_s), (FLA, xg_f))):
            home = i % 2 == 0
            h, a = (team, OPP) if home else (OPP, team)
            s = {"goals": [1, 1], "shots": [10, 10]}
            if i < n_xg:
                s["xg"] = [xf, xa] if home else [xa, xf]
            day = str(fmmatch._today() - timedelta(days=30 - i))
            fmmatch.record(d, {"id": f"fm{i}-{k}", "d": day, "home": h[1], "away": a[1], "hid": str(h[0]), "aid": str(a[0]), "s": s}, SK)
    fmmatch.save(d)
    features.reload()


def test_fotmob_matches_keep_priority_over_api_football():
    fill_history()
    put_fotmob(6)
    f = features.match_features("Santos", "Flamengo", SK)
    assert f["xg_src"] == "fotmob-matchs", f
    assert close(f["exp_xg_home"], 2.75) and close(f["exp_xg_away"], 0.45), f                  # valeurs FotMob, pas 2.0 / 1.0
    assert "FotMob (matchs)" in f["sources"]
    # FotMob sous le seuil (3 matchs avec xG) : API-Football prend le relais
    fill_history()
    put_fotmob(3)
    f = features.match_features("Santos", "Flamengo", SK)
    assert f["xg_src"] == "api-football-matchs" and close(f["exp_xg_home"], 2.0), f
    assert "API-Football" in f["sources"]


def test_api_football_replaces_season_table_only():
    fill_history()
    ars = {"sources": ["FotMob"], "exp_xg_home": 1.5, "exp_xg_away": 1.2, "exp_xg_total": 2.7, "xg_src": "fotmob-classement"}
    features._apistats(ars, "Santos", "Flamengo", SK)
    assert ars["xg_src"] == "api-football-matchs" and close(ars["exp_xg_home"], 2.0) and close(ars["exp_xg_total"], 3.0), ars
    assert "FotMob" not in ars["sources"] and "API-Football" in ars["sources"]                # le classement n'a plus rien fourni
    # Understat n'est jamais écrasé
    us = {"sources": ["understat"], "exp_xg_home": 1.1, "exp_xg_away": 0.9, "exp_xg_total": 2.0, "xg_src": "understat"}
    features._apistats(us, "Santos", "Flamengo", SK)
    assert us["xg_src"] == "understat" and us["exp_xg_total"] == 2.0 and us["exp_xg_home"] == 1.1
    # xG FotMob par match : intact aussi
    fm = {"sources": ["FotMob (matchs)"], "exp_xg_home": 3.0, "exp_xg_away": 0.5, "exp_xg_total": 3.5, "xg_src": "fotmob-matchs"}
    features._apistats(fm, "Santos", "Flamengo", SK)
    assert fm["xg_src"] == "fotmob-matchs" and fm["exp_xg_total"] == 3.5
    # même quand toutes les autres stats sont déjà là, l'xG d'API-Football remplace le classement
    full = {"sources": ["FotMob"], "exp_xg_home": 1.5, "exp_xg_away": 1.2, "exp_xg_total": 2.7, "xg_src": "fotmob-classement"}
    for stat in ("corners", "goals", "shots", "sot", "fouls", "cards"):
        full[f"exp_{stat}"] = full[f"exp_{stat}_home"] = full[f"exp_{stat}_away"] = 1.0
    features._apistats(full, "Santos", "Flamengo", SK)
    assert full["xg_src"] == "api-football-matchs" and close(full["exp_xg_total"], 3.0)
    assert full["exp_corners"] == 1.0                                                          # les autres chiffres déjà là ne bougent pas


# --- règlement ---------------------------------------------------------------------------------------------------------------------

def test_settlement_unaffected():
    from kelly import settle
    fresh()
    a, b = stats._entry_from(resp(SAN, FLA, 1.4, 0.9, corners=(7, 3))), stats._entry_from(resp(SAN, FLA, corners=(7, 3)))
    for k in ("corners", "cards", "shots", "sot", "fouls", "players", "players_done"):
        assert a[k] == b[k], k
    for tid in ("128", "127"):
        assert {k: v for k, v in a["teams"][tid].items() if k != "xg"} == b["teams"][tid]
    src = open(settle.__file__, encoding="utf-8").read()
    assert "xg" not in src.lower(), "settle.py ne lit pas l'xG"


# --- taille du fichier ---------------------------------------------------------------------------------------------------------------

def test_size_stays_under_cap_with_xg():
    fresh()
    rnd = random.Random(11)

    def row(day, xg=True):
        r = [day, rnd.randint(0, 40), rnd.randint(0, 1)] + [x for _ in range(6) for x in (rnd.randint(0, 25), rnd.randint(0, 25))]
        return r + [round(rnd.uniform(0, 3.5), 2), round(rnd.uniform(0, 3.5), 2)] if xg else r
    mk = lambda xg: {"teams": {str(1000 + i): {"nm": "Equipe %d" % (1000 + i), "al": ["Alias %d" % i], "sk": [SK, "soccer_conmebol_copa_libertadores"],      # noqa: E731
                                                "lg": "Brasileirão Série B", "d": REAL_TODAY(), "m": [row(260901 + k, xg) for k in range(15)]}
                               for i in range(400)}, "lg": ["Brasileirão"], "seen": {}, "v": 2}
    apistats.save(mk(True))                                      # le pire cas : 15 matchs avec xG pour chacune des 300 équipes
    size = store.size(apistats.FILE)
    loaded = apistats.load()
    assert size < 300_000 and size <= C.APISTATS_MAX_BYTES, size
    assert 250 <= len(loaded["teams"]) <= C.APISTATS_MAX_TEAMS == 300, len(loaded["teams"])
    assert all(len(t["m"]) <= 15 for t in loaded["teams"].values())
    # sans xG (ancien format) : 300 équipes, aucune n'est écartée par la limite d'octets
    fresh()
    apistats.save(mk(False))
    assert len(apistats.load()["teams"]) == 300 and store.size(apistats.FILE) < 300_000
    print(f"   apistats_profiles.json : pire cas avec xG = {size // 1000} Ko ({len(loaded['teams'])} équipes), sans xG = {store.size(apistats.FILE) // 1000} Ko")


# --- de bout en bout : le passage du matin, avec le faux API-Football (forfait gratuit) -----------------------------------------------

class XgAPI(FakeFootballAPI):
    """Faux API-Football dont /fixtures/statistics renvoie aussi expected_goals (nombre, chaîne ou null selon l'équipe)."""

    def stats_for(self, fid):
        out = super().stats_for(fid)
        for i, t in enumerate(out or []):
            tid = t["team"]["id"]
            v = {0: 1.62, 1: "0.85", 2: None}[tid % 3]
            t["statistics"].append({"type": "expected_goals", "value": v})
        return out


def test_morning_pass_end_to_end():
    fresh()
    os.environ["STATS_API_KEY"] = "test-key"
    os.environ.pop("STATS_DAILY_CAP", None)
    stats._today = lambda: DAY8
    stats._calls, stats._halted = 0, False
    stats._refreshed.clear(), stats._refused.clear(), stats._neg.clear()
    stats.BUDGET = 60
    home, away = (3000, "Alpha FC"), (3001, "Bravo FC")           # 3000 % 3 == 0 -> 1.62 ; 3001 % 3 == 1 -> "0.85"
    nul = (3002, "Charlie FC")                                    # 3002 % 3 == 2 -> null
    ms = [fake_match(8_100_000 + i, home, away, ts_of(DAY7, 20), ("Serie A", "Brazil"), goals=(2, 1)) for i in range(2)]
    ms += [fake_match(8_100_100, home, nul, ts_of(DAY7, 21), ("Serie A", "Brazil"), goals=(1, 0))]
    api = XgAPI([], today=DAY8)
    api.add_day(DAY7, ms)
    old_get = stats.requests.get
    stats.requests.get = api
    try:
        rep = apistats.fill(DAY8, {"leagues": {}})
    finally:
        stats.requests.get = old_get
    assert rep["added"] == 3 and api.refused == [], rep
    assert api.count("/fixtures/statistics") == 3 and api.count("/fixtures/players") == 0      # une requête par match : l'xG ne coûte rien de plus
    d = apistats.load()
    xi = apistats.R_STATS + 2 * 6
    rows_home = d["teams"]["3000"]["m"]
    assert len(rows_home) == 3
    assert [len(r) for r in rows_home] == [xi + 2, xi + 2, xi], rows_home                       # 3e match : l'adversaire a un null -> xG inconnu
    assert rows_home[0][xi:] == [1.62, 0.85] and d["teams"]["3001"]["m"][0][xi:] == [0.85, 1.62]
    # les statistiques brutes du cache portent l'xG, null = clé absente
    cached = stats._stats_cache()["8100000"]["teams"]
    assert cached["3000"]["xg"] == 1.62 and cached["3001"]["xg"] == 0.85
    assert "xg" not in stats._stats_cache()["8100100"]["teams"]["3002"]
    p = apistats.profiles()["teams"]["Alpha FC"]
    assert p["all"]["n"] == 3 and p["all"]["xg_n"] == 2 and "xg_for" not in p["all"]            # 2 matchs avec xG < APISTATS_MIN_N : moyenne non servie


# --- affichage -----------------------------------------------------------------------------------------------------------------------

def test_display_mentions_source_without_jargon():
    from kelly import app
    fill_history()
    f = features.match_features("Santos", "Flamengo", SK)
    txt = "\n".join(app.features_lines(f))
    assert "Buts attendus d'après les occasions (xG) : <b>3.0</b>" in txt and "(API-Football, matchs récents)" in txt, txt
    prof = features.team_profile("Santos")
    assert prof["aps"]["all"]["xg_n"] == 6
    t = app.fmt_team(prof, "Santos")
    assert "Buts attendus d'après les occasions (API-Football, 6 derniers matchs) : 2.00 pour / 1.00 contre" in t, t
    assert "xg" not in t.lower(), t                                                               # /stats ne dit pas « xG » ici
    # sous le seuil : rien d'affiché
    fill_history(n_xg=3)
    t = app.fmt_team(features.team_profile("Santos"), "Santos")
    assert "Buts attendus d'après les occasions" not in t, t


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    try:
        for t in tests:
            t()
            print(f"ok  {t.__name__}")
    finally:
        stats._today = REAL_TODAY
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{len(tests)} tests xG API-Football : OK")


if __name__ == "__main__":
    main()
