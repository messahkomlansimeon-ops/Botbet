"""Tests hors ligne de kelly/stats.py (aucun réseau). Lancer : python -m tests.test_stats"""
import itertools
import math
import os
import random
import tempfile
from datetime import date, datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-stats-test-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-stats-test-cache-")     # jamais le vrai cache (CSV du PC)
os.environ["ODDS_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-stats-test-odds-")
os.environ.pop("STATS_API_KEY", None)
os.environ.pop("STATS_DAILY_CAP", None)

import contextlib  # noqa: E402
import json  # noqa: E402

from kelly import apistats, cache, config as C, learn, stats, store  # noqa: E402
from kelly.sources import features, footballdata  # noqa: E402
from tests.fake_api import FakeFootballAPI, match as fake_match, seed_history as seed_team, ts_of  # noqa: E402


class Resp:
    def __init__(self, data, status=200, headers=None):
        self._d, self.status_code, self.headers = data, status, headers or {}

    def json(self):
        return self._d


PIN = "2026-10-09"                          # « aujourd'hui » des tests : le forfait gratuit ne sert que hier..demain autour de ce jour


stats._today = lambda: PIN


def reset(fake=None, cap=None, today=PIN):
    stats._today = lambda: today
    stats._calls, stats._halted = 0, False
    stats._refreshed.clear()
    stats._refused.clear()
    stats._neg.clear()
    stats.BUDGET = 60
    for f in os.listdir(store.DATA_DIR):
        os.remove(os.path.join(store.DATA_DIR, f))
    os.environ["STATS_API_KEY"] = "test-key"
    if cap:
        os.environ["STATS_DAILY_CAP"] = str(cap)
    else:
        os.environ.pop("STATS_DAILY_CAP", None)
    calls = []
    stats.requests.get = lambda url, **kw: (calls.append((url, kw.get("params"))), fake(url, kw.get("params")))[1]
    return calls


def next_run(hours=24):
    """Exécution suivante (le lendemain) : mémoire vide, relevés vieillis de `hours` heures."""
    stats._refreshed.clear()
    stats._neg.clear()
    d = store.load("fixtures_day.json", {})
    d["_fetched"] = {k: t - hours * 3600 for k, t in d.get("_fetched", {}).items()}
    store.save("fixtures_day.json", d)


def fx(i, home, away, ts, status="NS", goals=(None, None), fulltime=None):
    out = {"fixture": {"id": i, "timestamp": ts, "status": {"short": status}},
           "teams": {"home": {"id": i * 10, "name": home}, "away": {"id": i * 10 + 1, "name": away}},
           "goals": {"home": goals[0], "away": goals[1]}}
    if fulltime:
        out["score"] = {"fulltime": {"home": fulltime[0], "away": fulltime[1]}}
    return out


T0 = int(datetime(2026, 10, 10, 19, 0, tzinfo=timezone.utc).timestamp())
DAY = [fx(1, "Paris Saint Germain", "Olympique Marseille", T0),
       fx(2, "Manchester United", "Manchester City", T0),
       fx(3, "Inter", "AC Milan", T0),
       fx(4, "Bayern München", "Borussia Dortmund", T0),
       fx(5, "Arsenal W", "Chelsea W", T0),
       fx(6, "Arsenal", "Chelsea", T0 + 5 * 3600)]


def test_disabled():
    os.environ.pop("STATS_API_KEY", None)
    assert not stats.enabled() and stats.get("/fixtures", date="2026-10-10") is None
    assert stats.find_fixture("2026-10-10", "A", "B") is None
    assert stats.fixture_stats(1) is None and stats.team_averages(1, "d") is None
    assert stats.expected_totals("2026-10-10", "A", "B") is None
    picks = [{"x": 1}]
    assert stats.enrich(picks, "2026-10-10") is picks


def test_error_envelope_halts():
    n = {"c": 0}

    def fake(url, p):
        n["c"] += 1
        return Resp({"errors": {"plan": "Free plans do not have access to this season"}, "response": []})
    calls = reset(fake)
    assert stats.get("/fixtures", date="2026-10-10") is None
    assert stats.get("/fixtures", date="2026-10-10") is None
    assert stats.get("/fixtures/statistics", fixture=1) is None
    assert len(calls) == 1, calls                        # arrêt après la 1re erreur
    # erreur sous forme de liste, et enveloppe vide (liste/dict) = OK
    reset(lambda u, p: Resp({"errors": ["rate limit"], "response": []}))
    assert stats.get("/x") is None and stats._halted
    for empty in ([], {}):
        reset(lambda u, p, e=empty: Resp({"errors": e, "response": [1]}))
        assert stats.get("/x") == [1] and not stats._halted
    reset(lambda u, p: Resp({}, status=429))
    assert stats.get("/x") is None and stats._halted


def test_daily_cap_and_budget():
    calls = reset(lambda u, p: Resp({"errors": [], "response": [1]}), cap=3)
    assert [stats.get("/x") for _ in range(5)] == [[1], [1], [1], None, None]
    assert len(calls) == 3
    assert store.load("stats_quota.json", {})["n"] == 3
    # le compteur survit à un « redémarrage » du processus
    stats._calls, stats._halted = 0, False
    assert stats.get("/x") is None and len(calls) == 3
    # nouveau jour : compteur remis à zéro
    store.save("stats_quota.json", {"day": "2000-01-01", "n": 99})
    stats._halted = False
    assert stats.get("/x") == [1]
    # budget par exécution
    reset(lambda u, p: Resp({"errors": [], "response": [1]}))
    stats.BUDGET = 2
    assert [stats.get("/x") for _ in range(3)] == [[1], [1], None]
    stats.BUDGET = 60
    # en-tête de quota restant
    calls = reset(lambda u, p: Resp({"errors": [], "response": [1]},
                                    headers={"x-ratelimit-requests-remaining": "0",
                                             "x-ratelimit-requests-limit": "100"}))
    assert stats.get("/x") == [1] and stats.get("/x") is None and len(calls) == 1


def test_team_matching():
    calls = reset(lambda u, p: Resp({"errors": [], "response": DAY}))
    day = "2026-10-10"
    f = stats.find_fixture(day, "Paris Saint Germain", "Marseille")
    assert f and f["id"] == 1
    assert stats.find_fixture(day, "Manchester United", "Manchester City")["id"] == 2
    assert stats.find_fixture(day, "Inter Milan", "AC Milan")["id"] == 3
    assert stats.find_fixture(day, "Bayern Munich", "Borussia Dortmund")["id"] == 4
    assert len(calls) == 1 and calls[0][1]["timezone"] == "UTC"   # un seul appel pour la journée
    # cas négatifs : ne doit jamais matcher un autre match
    assert stats.find_fixture(day, "Manchester United", "Liverpool") is None
    assert stats.find_fixture(day, "Manchester City", "Manchester United") is None   # domicile/extérieur inversés
    assert stats.find_fixture(day, "Real Madrid", "Barcelona") is None
    assert stats.find_fixture(day, "Inter Miami", "Atlanta United") is None
    # le féminin n'est pas le masculin, et inversement
    assert stats.find_fixture(day, "Arsenal", "Chelsea", kickoff="2026-10-10T19:30:00Z") is None   # hors ±3 h
    assert stats.find_fixture(day, "Arsenal", "Chelsea", kickoff="2026-10-10T19:30:00Z") is None
    assert stats.find_fixture(day, "Arsenal W", "Chelsea W")["id"] == 5
    # le cache du jour est relu depuis le disque : pas de 2e appel
    assert len(calls) == 1
    assert stats.team_sim("Tottenham Hotspur", "Tottenham") >= 0.85
    assert stats.team_sim("Wolves", "Wolverhampton Wanderers") == 1.0


def test_player_matching():
    s = {"players": {"K. Mbappé": {"shots": 4.0}, "B. Silva": {"shots": 1.0}, "D. Silva": {"shots": 2.0},
                     "Vinícius Júnior": {"shots": 3.0}, "T. Alexander-Arnold": {"shots": 5.0},
                     "Casemiro": {"shots": 0.0}, "Gabriel Magalhães": {"shots": 2.0}}}
    ps = stats.player_stat
    assert ps(s, "Kylian Mbappe", "shots") == 4.0
    assert ps(s, "K. Mbappé", "shots") == 4.0
    assert ps(s, "Mbappe", "shots") == 4.0
    assert ps(s, "Bernardo Silva", "shots") == 1.0
    assert ps(s, "Silva", "shots") is None                       # ambigu
    assert ps(s, "Vinicius Junior", "shots") == 3.0
    assert ps(s, "Trent Alexander Arnold", "shots") == 5.0
    assert ps(s, "Casemiro", "shots") == 0.0
    assert ps(s, "Gabriel Magalhaes", "shots") == 2.0
    assert ps(s, "Erling Haaland", "shots") is None
    assert ps(s, "J. Mbappé", "shots") is None                   # mauvaise initiale
    assert ps(None, "x", "shots") is None and ps({"players": {}}, "x", "shots") is None


def test_poisson():
    p = stats.poisson_over
    assert abs(p(9.0, 8.5) - 0.5443) < 0.001
    assert p(0, 3.5) == 0.0 and 0 < p(1, 0.5) < 1
    assert p(5, 4.5) < p(6, 4.5) < p(7, 4.5)                      # croissant en la moyenne
    assert p(10, 8.5) > p(10, 9.5) > p(10, 10.5)                  # décroissant en la ligne


def stat_resp(ids, corners_h, corners_a):
    mk = lambda tid, c: {"team": {"id": tid}, "statistics": [
        {"type": "Corner Kicks", "value": c}, {"type": "Yellow Cards", "value": None},
        {"type": "Red Cards", "value": 1}, {"type": "Total Shots", "value": 10},
        {"type": "Ball Possession", "value": "45%"}, {"type": "Fouls", "value": None}]}
    return [mk(ids[0], corners_h), mk(ids[1], corners_a)]


def test_fixture_stats_and_averages():
    def fake(url, p):
        path = url.replace(stats.BASE, "")
        if path == "/fixtures/statistics":
            return Resp({"errors": [], "response": stat_resp((10, 20), 6, 4)})
        if path == "/fixtures/players":
            return Resp({"errors": [], "response": [{"players": [
                {"player": {"name": "K. Mbappé"}, "statistics": [{"games": {"minutes": 90}, "shots": {"total": None, "on": 2}, "fouls": {}}]}]}]})
        return Resp({"errors": [], "response": []})
    calls = reset(fake)
    st = stats.fixture_stats(7)
    assert st["corners"] == 10 and st["cards"] == 2 and st["players"]["K. Mbappé"] == {
        "shots": 0.0, "shots_on_target": 2.0, "fouls": 0.0}
    n = len(calls)
    assert stats.fixture_stats(7) is st or stats.fixture_stats(7) == st and len(calls) == n   # cache
    # moyennes : lues dans l'historique accumulé (apistats), SANS requête ; None sous 3 matchs avec statistiques
    calls.clear()
    d = apistats.load()
    for i in range(5):
        e = {"id": 100 + i, "ts": 1791000000 + i * 86400, "home": "A", "home_id": 10, "away": "B", "away_id": 20, "gh": 1, "ga": 0,
             "lk": "soccer_brazil_campeonato", "ln": "Brasileirão"}
        apistats.record(d, e, stats._entry_from(stat_resp((10, 20), 6 + i, 4)))
    e = {"id": 200, "ts": 1791000000, "home": "C", "home_id": 30, "away": "D", "away_id": 40, "gh": 1, "ga": 0, "lk": "africa", "lg": "L (Egypt)"}
    apistats.record(d, e, stats._entry_from(stat_resp((30, 40), 6, 4)))
    apistats.save(d)
    a = stats.team_averages(10, "d")
    assert a["n"] == 5 and a["corners_for"] == 8.0 and a["corners_against"] == 4.0 and a["cards_for"] == 1.0, a
    assert stats.team_averages(30, "d") is None and stats.team_averages(99, "d") is None            # 1 match / inconnue
    assert not calls, calls
    # corners absents des stats : pas de règlement sur 0
    reset(lambda u, p: Resp({"errors": [], "response": [{"team": {"id": 1}, "statistics": []},
                                                        {"team": {"id": 2}, "statistics": []}]}))
    assert stats.fixture_stats(8) is None


def test_day_cache_refresh_and_result():
    """Règlement gratuit : le cache du matin (statut NS) d'un jour PASSÉ est re-téléchargé (1 requête, une
    fois par exécution) ; buts du temps réglementaire ; jour du jour et matchs reportés non re-téléchargés."""
    day = str(date.fromisoformat(stats._today()) - timedelta(days=1))
    ts = int(datetime.fromisoformat(day + "T19:00:00+00:00").timestamp())
    ko = day + "T19:00:00Z"
    state = {"status": "NS"}

    def fake(url, p):
        if state["status"] == "NS":
            rows = [fx(1, "Alpha", "Bravo", ts), fx(2, "Charlie", "Delta", ts), fx(3, "Echo", "Foxtrot", ts)]
        else:
            rows = [fx(1, "Alpha", "Bravo", ts, "FT", (2, 1)),
                    fx(2, "Charlie", "Delta", ts, "AET", (3, 2), fulltime=(1, 1)),    # 90 min : 1-1
                    fx(3, "Echo", "Foxtrot", ts, "PST")]                              # reporté
        return Resp({"errors": [], "response": rows})
    calls = reset(fake)
    r = stats.fixture_result(day, "Alpha", "Bravo", ko)                # le matin : NS
    assert r and r["finished"] is False and r["home"] is None and len(calls) == 1
    assert stats.fixture_result(day, "Alpha", "Bravo", ko)["finished"] is False
    assert len(calls) == 1                                             # même exécution : pas de 2e appel
    state["status"] = "FT"
    next_run()                                                         # exécution suivante
    r = stats.fixture_result(day, "Alpha", "Bravo", ko)
    assert r["finished"] and (r["home"], r["away"]) == (2, 1) and len(calls) == 2, (r, len(calls))
    r = stats.fixture_result(day, "Charlie", "Delta", ko)
    assert r["finished"] and (r["home"], r["away"]) == (1, 1) and r["status"] == "AET"   # temps réglementaire
    r = stats.fixture_result(day, "Echo", "Foxtrot", ko)
    assert r["status"] == "PST" and not r["finished"] and r["home"] is None
    stats._refreshed.clear()
    stats.fixture_result(day, "Alpha", "Bravo", ko)                    # plus de match NS : cache gardé
    assert len(calls) == 2, calls
    assert stats.fixture_result(day, "Inconnu", "Equipe", ko) is None
    # le jour même : un match NS n'est pas re-téléchargé (il n'est pas fini)
    today = stats._today()
    calls = reset(lambda u, p: Resp({"errors": [], "response": [fx(9, "Alpha", "Bravo", ts, "NS")]}))
    stats.find_fixture(today, "Alpha", "Bravo")
    stats._refreshed.clear()
    stats.find_fixture(today, "Alpha", "Bravo")
    assert len(calls) == 1
    # ancien cache sans buts (format d'avant) pour un jour passé : re-téléchargé
    reset(lambda u, p: Resp({"errors": [], "response": [fx(1, "Alpha", "Bravo", ts, "FT", (0, 0))]}))
    store.save("fixtures_day.json", {day: [{"id": 1, "ts": ts, "status": "FT", "home": "Alpha", "home_id": 10,
                                            "away": "Bravo", "away_id": 11}]})
    assert stats.fixture_result(day, "Alpha", "Bravo", ko)["finished"]


# --- estimations d'avant-match : sources gratuites d'abord, API-Football en repli -----------------------

def view(cf, ca, kf, ka, gf, ga):
    return {"n": 10, "corners_for": cf, "corners_against": ca, "cards_for": kf, "cards_against": ka,
            "goals_for": gf, "goals_against": ga}


def put_profiles(fd=True, us=True):
    """Profils de sources minuscules dans le dossier de données de test (Arsenal - Chelsea)."""
    team = lambda *a: {"div": "E0", "last": "2026-10-01", "all": view(*a), "home": view(*a), "away": view(*a)}   # noqa: E731
    form = lambda xg, xga: {"n": 6, "xg": xg, "xga": xga}                                                        # noqa: E731
    uteam = lambda xg, xga: {"all": form(xg, xga), "home": form(xg, xga), "away": form(xg, xga)}                  # noqa: E731
    if fd:       # corners attendus : (7 + 6)/2 + (5 + 4)/2 = 11.0 ; cartons : (1.5 + 2)/2 + (2 + 2)/2 = 3.75 ; buts 1.7 + 1.1
        store.save("src_footballdata.json", {"teams": {"Arsenal": team(7, 4, 1.5, 2, 2.0, 0.8),
                                                       "Chelsea": team(5, 6, 2, 2, 1.4, 1.2)}, "referees": {}, "leagues": {}})
    if us:       # xG attendus : (1.9 + 1.5)/2 = 1.7 pour Arsenal, (1.3 + 1.0)/2 = 1.15 pour Chelsea
        store.save("src_understat.json", {"leagues": {"EPL": {
            "teams": {"Arsenal": uteam(1.9, 1.0), "Chelsea": uteam(1.3, 1.5)},
            "players": {"Arsenal": {"Bukayo Saka": {"min": 2000, "pos": "F", "shots90": 2.9},
                                    "Martin Ødegaard": {"min": 1500, "pos": "M", "shots90": 1.2, "sot90": 0.5}}}}}})
    features.reload()


def mk_pick(market, name, point, who=None, prob=0.5, home="Arsenal", away="Chelsea", eid="e1"):
    return {"id": eid, "group": "soccer", "sport_key": "soccer_epl", "home": home, "away": away,
            "event": f"{home} - {away}", "commence": "2026-10-10T19:00:00Z", "market": market, "name": name,
            "point": point, "who": who, "prob": prob, "prob_book": prob, "odds": 2.0, "edge": prob * 2 - 1,
            "score": 0.1, "heuristic": 0.1, "weight": 1.6, "why": "", "book": "b1"}


def api_fake(cards_yellow=2):
    """Faux API-Football : la liste du jour (Arsenal - Chelsea, ids 10 et 11) ; l'historique des équipes vient de seed_history()."""
    def fake(url, p):
        path = url.replace(stats.BASE, "")
        if path == "/fixtures" and p.get("date"):
            return Resp({"errors": [], "response": [fx(1, "Arsenal", "Chelsea", T0)]})
        return Resp({"errors": [], "response": []})
    return fake


def seed_history(n=5, cards_yellow=2):
    """Historique accumulé (apistats) de Arsenal (10) et Chelsea (11) : `n` matchs à 6 corners pour / 4 contre, `cards_yellow` jaunes chacun."""
    stat = lambda tid, corners: {"team": {"id": tid}, "statistics": [                                           # noqa: E731
        {"type": "Corner Kicks", "value": corners}, {"type": "Yellow Cards", "value": cards_yellow},
        {"type": "Red Cards", "value": 0}, {"type": "Total Shots", "value": 10}, {"type": "Fouls", "value": 10}]}
    d = apistats.load()
    for i in range(n):
        e = {"id": 300 + i, "ts": T0 - (i + 1) * 86400, "home": "Arsenal", "home_id": 10, "away": "Chelsea", "away_id": 11, "gh": 2, "ga": 1}
        apistats.record(d, e, stats._entry_from([stat(10, 6), stat(11, 4)]))
    apistats.save(d)


def test_enrich_sources_first_then_api():
    """Corners/cartons : les sources gratuites d'abord (AUCUN appel API-Football : quota préservé), l'API en repli."""
    calls = reset(api_fake())
    put_profiles()
    over, under, cards = (mk_pick("alternate_totals_corners", "Over", 9.5), mk_pick("alternate_totals_corners", "Under", 9.5),
                          mk_pick("alternate_totals_cards", "Over", 3.5))
    stats.enrich([over, under, cards], "2026-10-10")
    assert calls == [], calls                                       # sources suffisantes : zéro requête API-Football
    p_over = stats.poisson_over(11.0, 9.5)
    assert abs(over["prob"] - (0.6 * 0.5 + 0.4 * p_over)) < 1e-9 and abs(under["prob"] - (0.6 * 0.5 + 0.4 * (1 - p_over))) < 1e-9
    assert over["prob"] > 0.5 > under["prob"] and abs(over["edge"] - (over["prob"] * 2 - 1)) < 1e-9
    assert "stats réelles : 11.0 corners attendus vs ligne 9.5" in over["why"] and "11.0 corners" in under["why"]
    assert over["pm"] == {"kind": "corners", "src": "sources", "exp": 11.0, "gap": 1.5, "pgap": round(p_over - 0.5, 3)}
    assert under["pm"]["gap"] == -1.5 and under["pm"]["pgap"] < 0
    assert cards["pm"]["exp"] == 3.75 and cards["why"] == ""        # écart < 8 points : mélangé mais pas signalé
    assert over["score"] > 0.1 > under["score"] and over["heuristic"] == over["score"]   # score et heuristique bougent ensemble
    # repli API-Football : sans profil de source, mêmes calculs avec les moyennes des 5 derniers matchs accumulés (aucune requête de
    # statistiques ni de derniers matchs : seule la liste du jour sert à retrouver les ids)
    calls = reset(api_fake())
    seed_history()
    features.reload()
    a = mk_pick("alternate_totals_corners", "Over", 8.5)
    stats.enrich([a], "2026-10-10")
    assert [p for _, p in calls] == [{"date": "2026-10-10", "timezone": "UTC"}], calls
    assert a["pm"]["src"] == "api" and a["pm"]["exp"] == 10.0 and "10.0 corners attendus vs ligne 8.5" in a["why"], a
    assert abs(a["prob"] - (0.6 * 0.5 + 0.4 * stats.poisson_over(10.0, 8.5))) < 1e-9
    # sources présentes pour les corners mais pas les cartons : repli API seulement pour ce que les sources n'ont pas
    calls = reset(api_fake())
    seed_history()
    put_profiles(us=False)
    store.save("src_footballdata.json", {"teams": {"Arsenal": {"div": "E0", "all": {"n": 10, "corners_for": 7, "corners_against": 4}},
                                                    "Chelsea": {"div": "E0", "all": {"n": 10, "corners_for": 5, "corners_against": 6}}}})
    features.reload()
    co, ca = mk_pick("alternate_totals_corners", "Over", 9.5), mk_pick("alternate_totals_cards", "Over", 3.5)
    stats.enrich([co, ca], "2026-10-10")
    assert co["pm"]["src"] == "sources" and ca["pm"]["src"] == "api" and ca["pm"]["exp"] == 4.0, (co["pm"], ca["pm"])
    assert [p for _, p in calls] == [{"date": "2026-10-10", "timezone": "UTC"}]        # seule la liste du jour est demandée
    # plafond d'événements pour le repli API
    calls = reset(api_fake())
    seed_history()
    features.reload()
    two = [mk_pick("alternate_totals_corners", "Over", 9.5, eid="e1"), mk_pick("alternate_totals_corners", "Over", 9.5, eid="e2")]
    stats.enrich(two, "2026-10-10", max_events=1)
    assert two[0].get("pm") and not two[1].get("pm")
    # tout désactivé (ni clé API ni profils) : paris inchangés
    os.environ.pop("STATS_API_KEY", None)
    features.reload()
    u = mk_pick("alternate_totals_corners", "Over", 9.5)
    stats.enrich([u], "2026-10-10")
    assert u["prob"] == 0.5 and "pm" not in u and u["why"] == ""


def test_player_prop_model_signal():
    """player_shots / player_shots_on_target : Poisson sur les tirs/90 (85 min) face à la ligne ; « pourquoi » lisible."""
    reset(api_fake())
    os.environ.pop("STATS_API_KEY", None)
    put_profiles()
    over = mk_pick("player_shots", "Over", 1.5, "Bukayo Saka", prob=0.45)
    under = mk_pick("player_shots", "Under", 1.5, "Bukayo Saka", prob=0.55)
    exp = 2.9 * 85 / 90
    sot_est = mk_pick("player_shots_on_target", "Over", 0.5, "Bukayo Saka", prob=0.5)       # tirs cadrés absents : estimés
    sot_real = mk_pick("player_shots_on_target", "Over", 1.5, "Martin Odegaard", prob=0.5, home="Chelsea", away="Arsenal")
    stats.enrich([over, under, sot_est, sot_real], "2026-10-10")
    p = stats.poisson_over(exp, 1.5)
    assert abs(over["prob"] - (0.6 * 0.45 + 0.4 * p)) < 1e-9 and over["prob"] > 0.45, over
    assert "stats réelles : 2.9 tirs/90 vs ligne 1.5" in over["why"]
    assert abs(under["prob"] - (0.6 * 0.55 + 0.4 * (1 - p))) < 1e-9 and under["prob"] < 0.55
    assert over["pm"]["kind"] == "shots" and abs(over["pm"]["exp"] - round(exp, 2)) < 1e-9 and over["pm"]["gap"] == round(exp - 1.5, 2)
    assert under["pm"]["gap"] == round(1.5 - exp, 2)
    # tirs cadrés estimés (35 % des tirs), poids du modèle divisé par 2 ; mention « (estimé) »
    e = 2.9 * 0.35 * 85 / 90
    assert abs(sot_est["prob"] - (0.8 * 0.5 + 0.2 * stats.poisson_over(e, 0.5))) < 1e-9 and "(estimé)" in sot_est["why"]
    # tirs cadrés réels (FBref) : utilisés tels quels ; joueur trouvé dans l'équipe à l'extérieur
    assert sot_real["pm"]["exp"] == round(0.5 * 85 / 90, 2) and "(estimé)" not in sot_real["why"]
    # joueur inconnu ou d'une autre équipe : inchangé
    for who, home, away in (("Inconnu Total", "Arsenal", "Chelsea"), ("Bukayo Saka", "Chelsea", "Liverpool")):
        x = mk_pick("player_shots", "Over", 1.5, who, home=home, away=away)
        stats.enrich([x], "2026-10-10")
        assert x["prob"] == 0.5 and "pm" not in x, x
    # features figées pour l'apprentissage
    f = learn.features(over, datetime(2026, 10, 10, 8, tzinfo=timezone.utc))
    assert f["gap_shots"] == over["pm"]["gap"] and f["pm_gap"] == over["pm"]["pgap"] and f["has_pm"] == 1.0
    assert "gap_corners" not in f and "xg_diff" not in f


def test_goal_markets_and_features():
    """totals / btts : Poisson sur les buts attendus (xG Understat), poids 20 % ; 1N2 : seulement l'écart de buts."""
    reset(api_fake())
    os.environ.pop("STATS_API_KEY", None)
    put_profiles()
    tot = mk_pick("totals", "Over", 2.5, prob=0.40)
    under = mk_pick("totals", "Under", 2.5, prob=0.60)
    btts = mk_pick("btts", "Yes", None, prob=0.40)
    h2h = [mk_pick("h2h", "Arsenal", None, prob=0.5), mk_pick("h2h", "Chelsea", None, prob=0.3), mk_pick("h2h", "Draw", None, prob=0.2)]
    stats.enrich([tot, under, btts] + h2h, "2026-10-10")
    lh, la = 1.7, 1.15                                                            # xG attendus (voir put_profiles)
    p = stats.poisson_over(lh + la, 2.5)
    assert abs(tot["prob"] - (0.8 * 0.40 + 0.2 * p)) < 1e-9 and abs(under["prob"] - (0.8 * 0.60 + 0.2 * (1 - p))) < 1e-9
    assert "xG : 2.8 buts attendus vs ligne 2.5" in tot["why"] and tot["pm"]["kind"] == "goals", tot
    yes = (1 - math.exp(-lh)) * (1 - math.exp(-la))
    assert abs(btts["prob"] - (0.8 * 0.40 + 0.2 * yes)) < 1e-9 and "xG : 1.7-1.1 buts attendus" in btts["why"]
    assert [c["prob"] for c in h2h] == [0.5, 0.3, 0.2] and "pm" in h2h[0]       # 1N2 : proba intacte
    assert h2h[0]["pm"]["xgd"] == 0.55 and h2h[1]["pm"]["xgd"] == -0.55 and h2h[2]["pm"]["xgd"] == -0.55
    f = learn.features(tot, datetime(2026, 10, 10, 8, tzinfo=timezone.utc))
    assert f["gap_goals"] == tot["pm"]["gap"] and learn.features(h2h[1], datetime(2026, 10, 10, 8, tzinfo=timezone.utc))["xg_diff"] == -0.55
    # sans Understat : moyennes de buts football-data
    reset(api_fake())
    os.environ.pop("STATS_API_KEY", None)
    put_profiles(us=False)
    t2 = mk_pick("totals", "Over", 2.5, prob=0.40)
    stats.enrich([t2], "2026-10-10")
    assert t2["pm"]["exp"] == 2.7 and t2["pm"]["kind"] == "goals"                # (2.0 + 1.2)/2 + (1.4 + 0.8)/2
    assert "moyennes de buts : 2.7 buts attendus" in t2["why"]


def test_caching_no_refetch():
    """Cache API-Football : jour passé terminé = pour toujours ; erreurs et réponses vides pas redemandées en boucle ;
    statistiques absentes mémorisées ; moyennes d'équipe réutilisées quelques jours ; relevé incomplet re-téléchargé
    au plus toutes les C.FIXTURES_RETRY_HOURS et pendant C.FIXTURES_STALE_DAYS jours seulement."""
    from kelly import cache, config as C
    day = str(date.fromisoformat(stats._today()) - timedelta(days=1))
    ts = int(datetime.fromisoformat(day + "T19:00:00+00:00").timestamp())
    ko = day + "T19:00:00Z"
    # 1) HTTP 500 : une seule requête, même si on cherche 5 matchs (avant : une requête par recherche)
    calls = reset(lambda u, p: Resp({"errors": [], "response": []}, status=500))
    cache.reset()
    for _ in range(5):
        stats.find_fixture(day, "Alpha", "Bravo", ko)
    assert len(calls) == 1 and cache.COUNTS["api-football"]["hit"] >= 4, (len(calls), cache.COUNTS)
    # 2) réponse vide : pas redemandée dans la même exécution
    calls = reset(lambda u, p: Resp({"errors": [], "response": []}))
    for _ in range(3):
        stats.find_fixture(day, "Alpha", "Bravo", ko)
    assert len(calls) == 1
    # 3) jour passé terminé : zéro requête le lendemain et au-delà
    calls = reset(lambda u, p: Resp({"errors": [], "response": [fx(1, "Alpha", "Bravo", ts, "FT", (2, 1))]}))
    assert stats.fixture_result(day, "Alpha", "Bravo", ko)["finished"] and len(calls) == 1
    next_run(24 * 20)
    assert stats.fixture_result(day, "Alpha", "Bravo", ko)["finished"] and len(calls) == 1
    # 4) relevé incomplet (NS) : pas avant FIXTURES_RETRY_HOURS, plus du tout après FIXTURES_STALE_DAYS jours
    calls = reset(lambda u, p: Resp({"errors": [], "response": [fx(1, "Alpha", "Bravo", ts, "NS")]}))
    stats.fixture_result(day, "Alpha", "Bravo", ko)
    next_run(1)
    stats.fixture_result(day, "Alpha", "Bravo", ko)
    assert len(calls) == 1, "moins de 6 h : pas de nouveau relevé"
    next_run(C.FIXTURES_RETRY_HOURS + 1)
    stats.fixture_result(day, "Alpha", "Bravo", ko)
    assert len(calls) == 2, "relevé incomplet re-téléchargé après 6 h"
    old_day = str(date.fromisoformat(stats._today()) - timedelta(days=C.FIXTURES_STALE_DAYS + 2))
    calls = reset(lambda u, p: Resp({"errors": [], "response": [fx(1, "Alpha", "Bravo", ts, "NS")]}))
    stats.find_fixture(old_day, "Alpha", "Bravo")
    assert calls == [], "forfait gratuit : un jour hors de hier..demain n'est jamais demandé"
    C.STATS_DATE_WINDOW = 30                                           # forfait payant : toute date est permise, la règle d'âge s'applique
    try:
        stats.find_fixture(old_day, "Alpha", "Bravo")
        next_run(48)
        stats.find_fixture(old_day, "Alpha", "Bravo")
        stats.fixture_result(old_day, "Alpha", "Bravo")
    finally:
        C.STATS_DATE_WINDOW = 1
    assert len(calls) == 1, "un match NS vieux de plus de 3 jours n'est pas re-téléchargé indéfiniment"
    # 5) statistiques absentes : mémorisées, redemandées seulement après STATS_NO_DATA_DAYS
    calls = reset(lambda u, p: Resp({"errors": [], "response": []}))
    assert stats.fixture_stats(77) is None and stats.fixture_stats(77) is None
    stats._neg.clear()
    assert stats.fixture_stats(77) is None and len(calls) == 1, "absence de statistiques mémorisée"
    cache_ = stats._stats_cache()                                     # fichier à part (pas dans la base) : cache brut de stats.py
    cache_["77"] = {"missing": str(date.fromisoformat(stats._today()) - timedelta(days=C.STATS_NO_DATA_DAYS + 1))}
    with open(stats._stats_path(), "w") as f:
        json.dump(cache_, f)
    assert stats.fixture_stats(77) is None and len(calls) == 2
    # 6) forfait gratuit : « last », « ids » et les dates hors hier..demain ne sont JAMAIS envoyés (une erreur de plan arrêterait tout)
    calls = reset(lambda u, p: Resp({"errors": [], "response": [1]}))
    for kw in ({"team": 10, "last": 6}, {"ids": "1-2-3"}, {"date": "2026-10-04"}, {"date": "2026-10-11"}):
        path = "/fixtures"
        assert stats.get(path, **kw) is None and stats.refused(path, kw), kw
    assert calls == [] and not stats._halted
    assert stats.find_fixture("2026-10-04", "Alpha", "Bravo") is None and calls == []             # règlement d'un vieux pari : hors fenêtre
    for ok_day in ("2026-10-08", "2026-10-09", "2026-10-10"):                                     # hier, aujourd'hui, demain
        assert stats.get("/fixtures", date=ok_day) == [1]
    assert len(calls) == 3
    # 7) hors ligne (commandes Telegram) : aucune requête
    calls = reset(lambda u, p: Resp({"errors": [], "response": [1]}))
    with cache.offline():
        assert stats.get("/fixtures", date="2026-10-10") is None and stats.team_averages(10, "2026-10-10") is None
    assert calls == []


# --- stats mondiales : accumulation jour après jour des profils d'équipe (kelly/apistats.py), forfait gratuit --------------------
# Le faux API-Football (tests/fake_api.py) refuse « last », « ids » et toute date hors hier..demain avec les erreurs EXACTES relevées
# par les vraies sondes du 8 octobre 2026 : `api.refused == []` prouve que Kelly n'a jamais rien envoyé d'interdit.

REAL = os.path.join(os.path.dirname(__file__), "fixtures", "real_2026-10-08")
DAY7, DAY8, DAY9 = "2026-10-07", "2026-10-08", "2026-10-09"
LEAGUES8 = {1492398: ("Serie A", "Brazil"), 1492390: ("Serie A", "Brazil"), 1520912: ("Serie B", "Brazil"), 1520915: ("Serie B", "Brazil"),
            1638102: ("Veikkausliiga", "Finland"), 1638103: ("Veikkausliiga", "Finland"), 1492780: ("Premier Division", "Ireland")}
_ids = itertools.count(8_000_000)
_teams = itertools.count(800_000)


def real_day():
    """(matchs du jour d'API-Football, relevé gratuit des ligues) du 8 octobre 2026, vrais fichiers de l'époque (les ligues du Brésil, de
    la Finlande et de l'Irlande y sont nommées comme chez API-Football)."""
    entries = [dict(e, league=LEAGUES8[e["id"]]) if e["id"] in LEAGUES8 else e for e in json.load(open(os.path.join(REAL, "fixtures_day.json")))[DAY8]]
    monde = json.load(open(os.path.join(REAL, "monde.json")))
    scan = {"day": DAY8, "leagues": {k: [{"id": f"{k}-{i}", "sport_key": k, "home_team": h, "away_team": a,
                                          "commence_time": f"{DAY8}T{t}:00Z"} for i, (h, a, t) in enumerate(v)]
                                     for k, v in monde["leagues"].items()}}
    return entries, scan


def world(api_kw=None, cap=None, today=DAY8):
    """Données vides + faux API-Football (forfait gratuit) sur le jeu du 8 octobre. -> (faux API, relevé gratuit)."""
    entries, scan = real_day()
    api = FakeFootballAPI(entries, today=today, **(api_kw or {}))
    reset(None, cap, today=today)
    stats.requests.get = api
    features.reload()
    return api, scan


@contextlib.contextmanager
def on_day(day, api=None):
    """Le lendemain (horloge UTC d'API-Football déplacée) : mémoire vide, mêmes fichiers, listes de la veille vieillies de 24 h, compteur
    de l'API remis à zéro."""
    if api:
        api.new_day(day)
    old = stats._today
    stats._today = lambda: day
    stats._halted, stats._calls = False, 0
    next_run(24)
    try:
        yield
    finally:
        stats._today = old


def newteam(prefix="Club"):
    i = next(_teams)
    return i, f"{prefix} {i}"


def mk(home, away, day, league=("Serie A", "Brazil"), hour=21, status="FT"):
    return fake_match(next(_ids), home, away, ts_of(day, hour), league, status)


def seed(api, team, n, *args, **kw):
    """Donne `n` matchs d'historique à `team` (id, nom) en passant par apistats.record (tests/fake_api.seed_history)."""
    seed_team(api, team, n, *args, **kw)
    features.reload()


def asked(api, path="/fixtures/statistics", key="fixture"):
    return [p[key] for pth, p in api.calls if pth == path]


def test_free_plan_restrictions_are_modelled():
    """Le faux API-Football renvoie les erreurs exactes des vraies sondes ; Kelly n'envoie jamais ces demandes."""
    api, scan = world()
    r = api(stats.BASE + "/fixtures", params={"team": 127, "last": 12}).json()
    assert r["errors"] == {"plan": "Free plans do not have access to the Last parameter."}, r
    assert api(stats.BASE + "/fixtures", params={"ids": "1-2-3"}).json()["errors"] == {"plan": "Free plans do not have access to the Ids parameter."}
    assert api(stats.BASE + "/fixtures", params={"date": "2026-10-04"}).json()["errors"] == {
        "plan": "Free plans do not have access to this date, try from 2026-10-07 to 2026-10-09."}
    assert api(stats.BASE + "/fixtures", params={"date": DAY7, "timezone": "UTC"}).json()["errors"] == []
    assert len(api.refused) == 3
    api.calls.clear(), api.refused.clear()
    # côté Kelly : refusé d'avance, zéro requête, rien d'arrêté
    for path, kw in (("/fixtures", {"team": 127, "last": 12}), ("/fixtures", {"ids": "1-2"}), ("/fixtures", {"date": "2026-10-04"}),
                     ("/fixtures", {"date": "2026-10-10"})):
        assert stats.get(path, **kw) is None
    assert api.calls == [] and not stats._halted and stats.find_fixture("2026-10-04", "Alpha", "Bravo") is None
    # une journée complète de remplissage n'envoie rien d'interdit, et ne demande ni joueurs ni derniers matchs
    api.add_day(DAY7, [mk(newteam(), newteam(), DAY7) for _ in range(5)])
    rep = apistats.fill(DAY8, scan)
    assert rep["added"] == 5 + 1 and api.refused == [], (rep, api.refused)                    # + Mashujaa, terminé dans la nuit
    assert api.count("/fixtures/players") == 0 and not any("last" in p or "ids" in p or "team" in p for _, p in api.calls)
    assert {p["date"] for path, p in api.calls if path == "/fixtures"} == {DAY7, DAY8}


def test_brazilian_names():
    """Les clubs brésiliens que The Odds API et API-Football nomment différemment se retrouvent ; deux États ne se confondent pas."""
    sim = stats.team_sim
    assert sim("Nautico PE", "Nautico Recife") >= 0.8 and sim("Atletico Mineiro", "Atletico-MG") >= 0.8
    assert sim("Atletico Paranaense", "Athletico-PR") >= 0.8 and sim("Ceará", "Ceara") == 1.0
    assert sim("Grêmio Novorizontino", "Novorizontino") >= 0.8
    assert sim("Atletico MG", "Atletico GO") == 0 and sim("Botafogo SP", "Botafogo RJ") == 0
    assert sim("Atletico Mineiro", "Atletico Paranaense") < 0.8 and sim("Arsenal", "Arsenal W") == 0
    world()
    for home, away in (("Nautico PE", "Grêmio Novorizontino"), ("Atletico Paranaense", "Atletico Mineiro"), ("Santos", "Flamengo"),
                       ("Ceará", "Criciuma")):
        assert stats.find_fixture(DAY8, home, away, f"{DAY8}T22:30:00Z" if "Atletico" not in home else f"{DAY8}T23:00:00Z"), (home, away)


def test_league_classification():
    """Seules les ligues qui valent une requête passent : pas de grands championnats déjà détaillés par football-data, ni réserves, jeunes,
    féminines ou coupes."""
    from kelly import apileagues as AL
    ok = {("Brazil", "Serie A"): ("soccer_brazil_campeonato", AL.GAP), ("Brazil", "Serie B"): ("soccer_brazil_serie_b", AL.GAP),
          ("Argentina", "Liga Profesional Argentina"): ("soccer_argentina_primera_division", AL.GAP),
          ("USA", "Major League Soccer"): ("soccer_usa_mls", AL.GAP), ("Mexico", "Liga MX"): ("soccer_mexico_ligamx", AL.GAP),
          ("Chile", "Primera División"): ("soccer_chile_campeonato", AL.GAP), ("Japan", "J1 League"): ("soccer_japan_j_league", AL.GAP),
          ("South-Korea", "K League 1"): ("soccer_korea_kleague1", AL.GAP), ("China", "Super League"): ("soccer_china_superleague", AL.GAP),
          ("Saudi-Arabia", "Pro League"): ("soccer_saudi_arabia_pro_league", AL.GAP), ("Australia", "A-League Men"): ("soccer_australia_aleague", AL.GAP),
          ("World", "CONMEBOL Libertadores"): ("soccer_conmebol_copa_libertadores", AL.GAP),
          ("World", "UEFA Nations League"): ("soccer_uefa_nations_league", AL.INTL),
          ("World", "World Cup - Qualification Europe"): ("soccer_fifa_world_cup_qualifiers_europe", AL.INTL),
          ("World", "World Cup - Qualification CONCACAF"): ("soccer_fifa_world_cup_qualifiers_concacaf", AL.INTL),
          ("World", "Friendlies"): ("soccer_international_friendlies", AL.FRIENDLY), ("Colombia", "Primera A"): ("soccer_colombia_primera_a", AL.EXTRA)}
    for (country, name), (key, pri) in ok.items():
        got = AL.classify(country, name)
        assert got and got[0] == key and got[2] == pri and AL.priority(key) == pri, (country, name, got)
    for country, name in (("England", "Premier League"), ("Italy", "Serie A"), ("Turkey", "Süper Lig"), ("Portugal", "Primeira Liga"),
                          ("Brazil", "Serie A Women"), ("Brazil", "Serie C"), ("Argentina", "Reserve League"), ("Argentina", "Primera B Nacional"),
                          ("Australia", "A-League Women"), ("World", "Club Friendlies"), ("Brazil", "Copa do Brasil"), ("Spain", "La Liga"),
                          ("World", "UEFA U21 Championship"), ("Spain", "Segunda División RFEF - Group 5"), (None, None)):
        assert AL.classify(country, name) is None, (country, name)
    # l'Afrique est reconnue par stats.african_league, et marquée « africa » dans la liste du jour
    e = stats._entry({"fixture": {"id": 1, "timestamp": 1, "status": {"short": "FT"}}, "league": {"name": "Botola Pro", "country": "Morocco"},
                      "teams": {"home": {"id": 1, "name": "A"}, "away": {"id": 2, "name": "B"}}, "goals": {"home": 1, "away": 0}})
    assert e["lk"] == "africa" and e["lg"] == "Botola Pro (Morocco)" and AL.priority("africa") == AL.AFRICA


def test_apistats_selection_priority():
    """Ordre des requêtes : catégorie de ligue (sans football-data d'abord, Afrique, sélections, amicaux, équipes saturées, ligues hors
    catalogue), puis équipes qui rejouent aujourd'hui et équipes les moins fournies ; les ligues inutiles ne coûtent rien."""
    api, scan = world()
    nt = newteam
    sat = [nt(), nt()]
    r_, t_, b_, u_ = [nt(), nt()], [nt(), nt()], [nt(), nt()], [nt(), nt()]
    for t in sat:
        seed(api, t, 10)                                  # saturées : autant de matchs que la fenêtre des moyennes
    for t in r_ + t_:
        seed(api, t, 2)
    for t in b_:
        seed(api, t, 1, ("Serie B", "Brazil"), "soccer_brazil_serie_b", "Brasileirão Série B")
    api.add_day(DAY8, [mk(t_[0], nt(), DAY8, hour=22, status="NS")])         # T[0] rejoue aujourd'hui
    wanted = [("U", mk(*u_, DAY7)),                                           # 0 match chacune : le plus utile
              ("T", mk(*t_, DAY7)),                                           # 2 matchs, une équipe rejoue aujourd'hui
              ("B", mk(*b_, DAY7, ("Serie B", "Brazil"))),                    # Série B, 1 match chacune
              ("R", mk(*r_, DAY7)),                                           # 2 matchs, personne ne rejoue
              ("AF", mk(nt(), nt(), DAY7, ("Second League", "Egypt"))),
              ("NL", mk(nt(), nt(), DAY7, ("UEFA Nations League", "World"))),
              ("FR", mk(nt(), nt(), DAY7, ("Friendlies", "World"))),
              ("SAT", mk(*sat, DAY7)),
              ("CO", mk(nt(), nt(), DAY7, ("Primera A", "Colombia")))]
    useless = [mk(nt(), nt(), DAY7, ("Premier League", "England")), mk(nt(), nt(), DAY7, ("Serie A", "Italy")),
               mk(nt(), nt(), DAY7, ("Reserve League", "Argentina")), mk(nt(), nt(), DAY7, ("Serie A Women", "Brazil")),
               mk(nt(), nt(), DAY7, ("Primera B Nacional", "Argentina")), mk(nt(), nt(), DAY7, ("Serie A", "Brazil"), status="NS"),
               mk(nt(), nt(), DAY7, ("Serie C", "Brazil"))]
    mixed = [m for _, m in wanted] + useless
    api.add_day(DAY7, mixed[::-1])                       # ordre de la liste sans rapport avec la priorité
    n_before = len(api.calls)
    rep = apistats.fill(DAY8, scan)
    order = asked(api)
    mashujaa = order.index(1600780)                                  # Tanzanie, terminé ce matin (liste du jour) : Afrique comme AF
    assert 4 <= mashujaa <= 5 and [i for i in order if i != 1600780] == [m["id"] for _, m in wanted], \
        [(k, order.index(m["id"]) if m["id"] in order else None) for k, m in wanted]
    assert rep["added"] == len(wanted) + 1 and rep["stop"] is None and api.refused == []                    # + Mashujaa (Tanzanie, terminé ce matin)
    assert not set(order) & {m["id"] for m in useless}
    # le match ajouté est dans l'historique des DEUX équipes, avec ses dates et sa ligue
    d = apistats.load()
    for tid in u_:
        t = d["teams"][str(tid[0])]
        assert len(t["m"]) == 1 and t["m"][0][2] in (0, 1) and t["sk"] == ["soccer_brazil_campeonato"] and t["lg"] == "Brasileirão"
    assert d["teams"][str(u_[0][0])]["m"][0][2] == 1 and d["teams"][str(u_[1][0])]["m"][0][2] == 0       # domicile / extérieur
    assert apistats._i2d(d["teams"][str(u_[0][0])]["m"][0][0]) == DAY7


def test_apistats_budget_and_reserve():
    """Jamais plus de APISTATS_DAILY (55) requêtes de profils par jour (listes de matchs comprises), jamais sous la réserve de 30 du
    règlement ; les matchs les plus utiles passent d'abord ; ceux d'hier qui restent sont perdus et le journal le dit."""
    assert C.APISTATS_DAILY == 55 and C.FAIR_API_RESERVE == 30
    api, scan = world()
    first = [mk(newteam(), newteam(), DAY7) for _ in range(60)]
    poor = [mk(newteam(), newteam(), DAY7, ("Primera A", "Colombia")) for _ in range(60)]
    api.add_day(DAY7, poor + first)
    rep = apistats.fill(DAY8, scan)
    q = store.load("stats_quota.json", {})
    assert q["n"] == len(api.calls) == q["aps"] == C.APISTATS_DAILY == 55, (q, rep)
    assert stats.left_today() == 35 >= C.FAIR_API_RESERVE and rep["stop"] == "budget" and api.refused == []
    assert api.count("/fixtures") == 2 and api.count("/fixtures/statistics") == 53 and rep["requests"] == 55
    assert set(asked(api)) <= {m["id"] for m in first}, "les ligues du catalogue d'abord, ni Colombie ni Afrique aujourd'hui"
    assert rep["lost"] == 7 + 60 and rep["added"] == 53, rep      # 7 matchs du Brésil + 60 de Colombie d'hier ne reviendront pas (Mashujaa, d'aujourd'hui, si)
    line = apistats.report_lines(rep)[0]
    assert "budget du jour atteint (55 requêtes)" in line and "perdus faute de budget" in line, line
    # même jour : plus rien à dépenser, zéro requête
    n = len(api.calls)
    again = apistats.fill(DAY8, scan)
    assert len(api.calls) == n and again["requests"] == 0 and again["stop"] == "budget" and again["added"] == 0, again
    # le règlement a déjà consommé 20 requêtes ce matin : les profils s'arrêtent à 90 - 30 (réserve) - 20 = 40
    api, scan = world()
    api.add_day(DAY7, [mk(newteam(), newteam(), DAY7) for _ in range(60)])
    store.save("stats_quota.json", {"day": stats._today(), "n": 20})
    apistats.fill(DAY8, scan)
    q = store.load("stats_quota.json", {})
    assert q["aps"] == q["n"] - 20 == 40 and stats.left_today() == C.FAIR_API_RESERVE, q
    # APISTATS_DAILY=0 : désactivé
    C.APISTATS_DAILY = 0
    try:
        assert apistats.fill(DAY8, scan)["stop"] == "off" and not apistats.active()
    finally:
        C.APISTATS_DAILY = 55


def test_apistats_forever_cache():
    """Un match déjà ajouté (ou dont les statistiques sont en cache) ne coûte plus rien : le lendemain, seuls les nouveaux matchs sont
    payés ; même un cache de statistiques perdu ne fait pas redemander un match déjà vu ; la liste d'hier est celle du règlement."""
    api, scan = world()
    plain = [mk(newteam(), newteam(), DAY7) for _ in range(3)]
    cached = mk(newteam(), newteam(), DAY7)
    api.add_day(DAY7, plain + [cached])
    # le règlement du matin a déjà lu la liste d'hier et les statistiques d'un match (stats.fixture_stats : cache pour toujours)
    stats._day_fixtures(DAY7)
    assert stats.fixture_stats(cached["id"], players=False)
    rep = apistats.fill(DAY8, scan)
    assert api.count("/fixtures", date=DAY7) == 1 and api.count("/fixtures", date=DAY8) == 1, "chaque liste n'est lue qu'une fois"
    assert asked(api).count(cached["id"]) == 1, "statistiques en cache : zéro requête de plus"
    assert sorted(asked(api)) == sorted([m["id"] for m in plain + [cached]] + [1600780]), asked(api)         # 1600780 : Mashujaa (nuit)
    assert rep["added"] == 5 and rep["requests"] == 1 + 4, rep                                              # le match en cache est ajouté gratuitement
    seen = apistats.load()["seen"]
    assert all(str(m["id"]) in seen for m in plain + [cached])
    day1 = set(asked(api))
    # --- le lendemain : les matchs de la veille au soir sont terminés ; seuls les NOUVEAUX coûtent une requête
    new = [mk(newteam(), newteam(), DAY8, hour=h) for h in (14, 16, 18)]
    api.add_day(DAY8, new)
    with on_day(DAY9, api):
        before = len(api.calls)
        rep2 = apistats.fill(DAY9, scan)
        day2 = [p["fixture"] for path, p in api.calls[before:] if path == "/fixtures/statistics"]
        assert sorted(day2) == sorted(m["id"] for m in new) and not set(day2) & day1, day2           # jamais un match de la veille
        assert rep2["added"] == 3 and api.refused == []
        assert api.count("/fixtures", date=DAY8) == 2 and api.count("/fixtures", date=DAY9) == 1      # liste d'hier relue (elle a changé), pas deux fois
        assert rep2["requests"] == 2 + 3
    # --- cache des statistiques perdu (cache d'Actions expiré) : le fichier de profils suffit à ne rien redemander
    os.remove(stats._stats_path())
    with on_day(DAY9, api):
        before = len(api.calls)
        rep3 = apistats.fill(DAY9, scan)
        assert [c for c in api.calls[before:] if c[0] == "/fixtures/statistics"] == [] and rep3["added"] == 0, rep3


def test_apistats_pruning_and_size():
    """15 matchs au plus par équipe (les plus récents), 300 équipes au plus, fichier < 300 Ko, équipes oubliées après 120 jours."""
    api, scan = world()
    A, tag = newteam(), "soccer_brazil_campeonato"
    d = apistats.load()
    for k in range(20):
        e = mk(A, newteam(), "2026-09-%02d" % (k + 1))
        e["lk"], e["ln"] = tag, "Brasileirão"
        api.by_id[e["id"]] = e
        apistats.record(d, e, stats._entry_from(api.stats_for(e["id"])))
    rows = d["teams"][str(A[0])]["m"]
    assert len(rows) == C.APISTATS_KEEP_MATCHES == 15 and [apistats._i2d(r[0]) for r in rows][0] == "2026-09-06" and rows[-1][0] == 260920
    assert apistats.record(d, e, stats._entry_from(api.stats_for(e["id"]))) == 0, "un match déjà vu n'est jamais ajouté deux fois"
    # un fichier plein : 400 équipes de 15 matchs (valeurs réalistes) -> 300 équipes, moins de 300 Ko
    rnd = random.Random(7)
    row = lambda day: [day, rnd.randint(0, 40), rnd.randint(0, 1)] + [x for _ in range(6) for x in (rnd.randint(0, 25), rnd.randint(0, 25))]
    d["teams"] = {str(1000 + i): {"nm": "Equipe %d" % (1000 + i), "al": ["Alias %d" % i], "sk": ["soccer_brazil_campeonato", "soccer_conmebol_copa_libertadores"],
                                   "lg": "Brasileirão Série B", "d": "2026-10-%02d" % (1 + i % 8),
                                   "m": [row(260901 + k) for k in range(15)]} for i in range(400)}
    d["seen"] = {str(i): 260000 + i for i in range(600)}
    apistats.save(d)
    size = store.size("apistats_profiles.json")
    loaded = apistats.load()
    assert len(loaded["teams"]) == C.APISTATS_MAX_TEAMS == 300 and size < 300_000, size
    assert all(len(t["m"]) <= 15 for t in loaded["teams"].values()) and loaded["seen"] == {}, "matchs « vus » trop anciens oubliés"
    old = dict(next(iter(loaded["teams"].values())), d="2020-01-01")
    loaded["teams"] = {"1": old, "2": dict(old, d=stats._today())}
    apistats.save(loaded)
    assert set(apistats.load()["teams"]) == {"2"}                      # équipe non renouvelée depuis 120 jours : oubliée
    print(f"   apistats_profiles.json : 300 équipes x 15 matchs = {size // 1000} Ko")


def test_apistats_progress_line():
    """Journal du matin : une ligne d'avancement honnête, avec les seuils de qualité ; même ligne (dernier passage) dans --check."""
    api, scan = world()
    # 4 équipes du jour déjà suivies avec 6 matchs ("bonnes"), 2 avec 3 ("utilisables")
    for tid, name in ((128, "Santos"), (127, "Flamengo"), (129, "Ceara"), (140, "Criciuma")):
        seed(api, (tid, name), 6)
    for tid, name in ((755, "Nautico Recife"), (7834, "Novorizontino")):
        seed(api, (tid, name), 3, ("Serie B", "Brazil"), "soccer_brazil_serie_b", "Brasileirão Série B")
    ysd = [mk(newteam(), newteam(), DAY7) for _ in range(12)]
    api.add_day(DAY7, ysd)
    rep = apistats.fill(DAY8, scan)
    n_added, req = rep["added"], rep["requests"]
    assert n_added == 13 and req == 15 and rep["tracked"] == 6 + 26 == 32 and rep["usable"] == 6 and rep["good"] == 4, rep
    assert rep["tot"] == 30 and rep["cov"] == 3 and rep["unmatched"] == 0, rep      # 7 matchs de ligues + 23 africains (Mashujaa est fini)
    lines = apistats.report_lines(rep)
    assert lines[0] == ("🌍 Stats mondiales (accumulation jour après jour, forfait gratuit) : 13 matchs d'hier ajoutés (15 requêtes) · "
                        f"32 équipes suivies, 6 avec 3+ matchs (utilisables), 4 avec 6+ (bonnes) · couverture des matchs du jour : 3/30"), lines
    assert "3 à 5 matchs = utilisable" in lines[1] and "6+ = bon" in lines[1] and "30 gardées pour le règlement" in lines[1] and \
        f"profils 15/{C.APISTATS_DAILY}" in lines[1], lines
    assert len(lines) == 2 and "forfait payant" not in "".join(lines)
    chk = apistats.check_lines()
    assert chk[0].startswith("   Stats mondiales (accumulation jour après jour, forfait gratuit) : 32 équipes suivies, 6 avec 3+ matchs") and \
        f"dernier passage du matin ({DAY8}) : 13 matchs ajoutés (15 requêtes), couverture des matchs du jour 3/30" in chk[0], chk
    assert chk[1] == f"   15/{C.APISTATS_DAILY} requêtes de profils aujourd'hui, 30 gardées pour le règlement"
    # singulier, et rien de neuf : formulation honnête
    assert "1 match d'hier ajouté (2 requêtes)" in apistats.progress_line(dict(rep, added=1, requests=2, nostats=0, stop=None, lost=0))
    assert "aucun nouveau match d'hier" in apistats.progress_line(dict(rep, added=0, stop=None, lost=0)) and \
        "aucun match du jour à couvrir" in apistats.progress_line(dict(rep, tot=0, stop=None, lost=0))
    # le lendemain à vide : liste d'hier connue, rien à ajouter
    with on_day(DAY9, api):
        rep2 = apistats.fill(DAY9, scan)
        assert rep2["added"] >= 0 and apistats.report_lines(rep2)[0].startswith("🌍 Stats mondiales (accumulation jour après jour, forfait gratuit)")


def test_apistats_profile_format_and_quality():
    """3 matchs = profil utilisable (confiance réduite), 6+ = bon ; le même seuil règle sources.features, prematch (poids) et fair."""
    api, scan = world()
    from kelly import fair
    aq = features.aps_quality
    assert [aq(n) for n in (0, 2, 3, 4, 5, 6, 8, 10, 12)] == [0.0, 0.0, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.6]
    assert aq(C.APISTATS_GOOD_N) >= C.FAIR_COMBO_MIN_Q > aq(C.APISTATS_GOOD_N - 1), "bon = admis dans les combinés à cote juste"
    seed(api, (128, "Santos"), 2)
    seed(api, (127, "Flamengo"), 6)
    assert "Santos" not in apistats.profiles()["teams"] and apistats.counts()[:2] == (2, 1)           # 2 matchs : pas servi
    assert features.match_features("Santos", "Flamengo", "soccer_brazil_campeonato")["sources"] == []
    seed(api, (128, "Santos"), 1, first=3)                                                            # 3 matchs au total
    prof = apistats.profiles()
    t = prof["teams"]["Santos"]
    assert t["all"]["n"] == 3, t["all"]["n"]
    f = features.match_features("Santos", "Flamengo", "soccer_brazil_campeonato")
    assert f["sources"] == ["API-Football"] and f["aps_n"] == (t["all"]["n"], 6) and f["aps_q"] == aq(t["all"]["n"]), f
    # format football-data : même clés (sans les jaunes), mêmes moyennes, vues domicile / extérieur
    ms = [{"date": "2026-10-0%d" % (d + 1), "home": "A", "away": "B", "hg": 1, "ag": 0, "hs": 10, "as": 8, "hst": 4, "ast": 3, "hf": 11, "af": 12,
           "hc": 5, "ac": 4, "hy": 2, "ay": 1, "hr": 0, "ar": 0, "div": "E0"} for d in range(6)]
    ref = footballdata.team_profiles(ms)["teams"]["A"]["all"]
    assert set(t["all"]) - {"xg_for", "xg_against", "xg_n"} <= set(ref) and "yellow_for" not in t["all"] and t["all"]["xg_n"] == 0 and {"corners_for", "cards_against", "sot_for", "fouls_for"} <= set(t["all"])
    assert t["home"]["n"] + t["away"]["n"] == t["all"]["n"] and t["sk"] == ["soccer_brazil_campeonato"] and t["id"] == 128
    for k in ("exp_corners", "exp_cards", "exp_shots", "exp_sot", "exp_fouls", "exp_goals_home", "exp_shots_home", "exp_sot_away"):
        assert f.get(k) is not None and k in f["aps_keys"], k
    h, a = prof["teams"]["Santos"]["home"], prof["teams"]["Flamengo"]["away"]
    hv = h if h["n"] >= 3 else t["all"]
    av = a if a["n"] >= 3 else prof["teams"]["Flamengo"]["all"]
    assert abs(f["exp_corners_home"] - round((hv["corners_for"] + av["corners_against"]) / 2, 2)) < 0.011
    # fair : dès 3 matchs pour un profil API-Football, 5 pour les autres sources
    g = {"n_home": 3, "n_away": 3, "exp_corners": 10.2, "exp_cards": 4.9}
    assert fair.options(g, "A", "B") == [] and fair.options(dict(g, aps_keys=["exp_corners", "exp_cards"], aps_q=0.2), "A", "B")
    assert fair.options(dict(g, n_home=2, aps_keys=["exp_corners"]), "A", "B") == []
    # une équipe sans profil : rien d'inventé ; un autre championnat : pas de profil non plus ; nom de The Odds API retrouvé (alias)
    assert features.match_features("Inconnu", "Flamengo", "soccer_brazil_campeonato")["sources"] == []
    assert features.match_features("Santos", "Flamengo", "soccer_epl")["sources"] == []
    seed(api, (755, "Nautico Recife"), 4, ("Serie B", "Brazil"), "soccer_brazil_serie_b", "Brasileirão Série B")
    apistats.fill(DAY8, scan)
    prof = apistats.profiles()
    assert "Nautico PE" in prof["teams"] and prof["teams"]["Nautico PE"]["id"] == 755
    p = features.team_profile("Nautico")
    assert p["team"] == "Nautico Recife" and p["fd"] is None and p["aps"]["all"]["n"] >= 4
    # Afrique : seul le repli API-Football répond, jamais football-data
    seed(api, (25468, "Masar"), 4, ("Second League", "Egypt"), "africa", "Second League (Egypt)")
    seed(api, (1032, "El Dakhleya"), 4, ("Second League", "Egypt"), "africa", "Second League (Egypt)")
    g = features.match_features("Masar", "El Dakhleya", "africa")
    assert g["sources"] == ["API-Football"] and "exp_corners" in g and g["aps_q"] == aq(4)


def test_apistats_degrades():
    """Erreur de plan sur les statistiques -> arrêt net pour la journée, dit une fois en français ; statistiques null -> ligue abandonnée
    après quelques essais ; sans clé -> rien."""
    # 1) le forfait refuse les statistiques : une seule requête perdue, plus rien ce jour-là (même en relançant), puis reprise demain
    api, scan = world({"stats_plan_error": True})
    api.add_day(DAY7, [mk(newteam(), newteam(), DAY7) for _ in range(8)])
    rep = apistats.fill(DAY8, scan)
    assert api.count("/fixtures/statistics") == 1 and rep["stop"] == "halt" and stats._halted and rep["added"] == 0, (rep, api.calls)
    lines = apistats.report_lines(rep)
    assert len(lines) == 2 and sum("forfait" in l for l in lines) == 1, lines
    assert lines[0].startswith("⚠️ API-Football : ton forfait n'a pas accès aux statistiques de match (plan: Free plans do not have access") and \
        "football-data et les cotes" in lines[0] and "s'arrête pour aujourd'hui" in lines[0], lines
    assert lines[1].startswith("🌍 Stats mondiales suspendues : 0 équipes suivies") and "rien d'ajouté aujourd'hui" in lines[1]
    assert any("suspendues" in l for l in apistats.check_lines())
    n = len(api.calls)
    stats._halted, stats._calls = False, 0                       # nouvelle exécution le même jour : l'erreur du jour est relue, zéro requête
    assert apistats.fill(DAY8, scan)["stop"] == "halt" and len(api.calls) == n
    assert features.match_features("Santos", "Flamengo", "soccer_brazil_campeonato")["sources"] == []      # le reste du bot continue
    api.stats_plan_error = False
    with on_day(DAY9, api):                                      # le lendemain : tout reprend
        api.add_day(DAY8, [mk(newteam(), newteam(), DAY8, hour=15)])
        rep = apistats.fill(DAY9, scan)
        assert rep["added"] == 2 and rep["stop"] is None and "forfait payant" not in "".join(apistats.report_lines(rep))
    # 2) plan refusé dès la liste des matchs : une seule requête
    api, scan = world({"plan_error": True})
    rep = apistats.fill(DAY8, scan)
    assert len(api.calls) == 1 and rep["stop"] == "halt" and stats._halted
    assert "forfait" in apistats.report_lines(rep)[0] and "à ces données" in apistats.report_lines(rep)[0]
    # 3) statistiques null (ligue que l'API ne suit pas) : la ligue est abandonnée après MISS_STOP essais sans succès
    bad = [newteam() for _ in range(12)]
    api, scan = world({"null_teams": {t[0] for t in bad}})
    api.add_day(DAY7, [mk(bad[2 * i], bad[2 * i + 1], DAY7) for i in range(6)])
    rep = apistats.fill(DAY8, scan)
    assert api.count("/fixtures/statistics") == apistats.MISS_STOP + 1 == 4 and rep["nostats"] == 3 and rep["added"] == 1, rep   # + Mashujaa
    assert "3 sans statistiques chez l'API" in apistats.report_lines(rep)[0] and not any(str(t[0]) in apistats.load()["teams"] for t in bad)
    # 4) sans clé : rien, sans erreur
    os.environ.pop("STATS_API_KEY", None)
    rep = apistats.fill(DAY8, scan)
    assert rep["stop"] == "nokey" and apistats.report_lines(rep) == ["⚪ Stats mondiales : pas de clé STATS_API_KEY"]
    assert apistats.check_lines() == ["   Stats mondiales : inactives (STATS_API_KEY absente)"]


def schedule(teams, weeks, days=(2, 6), start="2026-09-07"):
    """Calendrier d'un championnat à 20 clubs : une journée de 10 matchs aux jours `days` de chaque semaine (0 = lundi), méthode des cercles."""
    ids = list(range(len(teams)))
    rounds = []
    for r in range(weeks * len(days)):
        pairs = [(ids[i], ids[-1 - i]) for i in range(len(ids) // 2)]
        rounds.append([(teams[b], teams[a]) if (r + i) % 2 else (teams[a], teams[b]) for i, (a, b) in enumerate(pairs)])
        ids = [ids[0]] + [ids[-1]] + ids[1:-1]
    out = []
    for r, ms in enumerate(rounds):
        day = str(date.fromisoformat(start) + timedelta(days=(r // len(days)) * 7 + days[r % len(days)]))
        out.append((day, ms))
    return out


def growth(days_of_week, weeks=5):
    """Simule le Brasileirão (20 clubs) jour après jour avec le vrai remplissage du matin. -> [(jour, équipes utilisables, bonnes, requêtes)]."""
    reset(None, today="2026-09-07")
    api = FakeFootballAPI(today="2026-09-07")
    stats.requests.get = api
    features.reload()
    teams = [(9000 + i, "Time %d" % i) for i in range(20)]
    cal = schedule(teams, weeks, days_of_week)
    for day, ms in cal:
        api.add_day(day, [mk(h, a, day) for h, a in ms])
    out, start = [], date.fromisoformat("2026-09-07")
    for k in range(weeks * 7 + 1):
        day = str(start + timedelta(days=k))
        with on_day(day, api):
            rep = apistats.fill(day, {"leagues": {}})
        s = apistats.summary()
        out.append((day, s["usable"], s["good"], rep["requests"], rep["added"]))
    assert api.refused == [] and api.count("/fixtures/players") == 0
    return out


def test_apistats_brazil_growth():
    """Brasileirão Série A (20 clubs, 10 matchs par journée) : combien de semaines pour que tout le monde soit utilisable (3 matchs), puis
    bon (6 matchs) avec le forfait gratuit. Le budget (55 requêtes par jour) n'est jamais le facteur limitant : c'est le calendrier."""
    two = growth((2, 6))                                    # mercredi + dimanche : 2 journées par semaine
    at = lambda day, rows=two: next(r for r in rows if r[0] == day)                                # noqa: E731
    wk = lambda n, rows=two: at(str(date.fromisoformat("2026-09-07") + timedelta(days=7 * n)), rows)   # lundi matin de la semaine n   # noqa: E731
    assert wk(1)[1:3] == (0, 0), wk(1)                       # 1 semaine : 2 matchs par équipe, pas encore utilisable
    assert wk(2)[1:3] == (20, 0), wk(2)                      # 2 semaines : 4 matchs -> les 20 clubs sont utilisables (dès le jeudi de la semaine 2)
    assert at("2026-09-17")[1] == 20 and at("2026-09-16")[1] == 0
    assert wk(3)[1:3] == (20, 20), wk(3)                     # 3 semaines : 6 matchs -> les 20 clubs sont bons
    assert sum(r[4] for r in two) == 10 * 2 * 5 and max(r[3] for r in two) <= 12                   # 10 requêtes par journée + 2 listes
    one = growth((5,), weeks=8)                              # une journée par semaine (samedi)
    assert wk(3, one)[1:3] == (20, 0) and wk(5, one)[1:3] == (20, 0) and wk(6, one)[1:3] == (20, 20), [wk(n, one) for n in range(1, 8)]
    print("   Brasileirão, 2 journées par semaine : " + " | ".join(f"S{n}: {wk(n)[1]} utilisables, {wk(n)[2]} bons" for n in range(1, 5)))
    print("   Brasileirão, 1 journée par semaine : " + " | ".join(f"S{n}: {wk(n, one)[1]} utilisables, {wk(n, one)[2]} bons" for n in range(1, 8)))


def test_fixture_stats_new_fields():
    """Tirs cadrés, jaunes, nuls : un null d'API-Football n'est jamais compté comme zéro dans les profils."""
    teams = [{"team": {"id": 1}, "statistics": [{"type": "Corner Kicks", "value": 6}, {"type": "Total Shots", "value": 14},
                                                {"type": "Shots on Goal", "value": None}, {"type": "Fouls", "value": None},
                                                {"type": "Yellow Cards", "value": 2}, {"type": "Red Cards", "value": None}]},
             {"team": {"id": 2}, "statistics": [{"type": "Corner Kicks", "value": None}, {"type": "Total Shots", "value": 9},
                                                {"type": "Shots on Goal", "value": 4}, {"type": "Fouls", "value": 13},
                                                {"type": "Yellow Cards", "value": None}, {"type": "Red Cards", "value": None}]}]
    e = stats._entry_from(teams)
    a, b = e["teams"]["1"], e["teams"]["2"]
    assert a["sot"] == 0 and "sot" not in a["nul"] and a["nul"] == ["fouls"] and a["cards"] == 2 and a["yel"] == 2
    assert b["nul"] == [] and b["corners"] == 0 and b["cards"] == 0 and b["yel"] == 0 and b["sot"] == 4   # null corners/jaunes/rouges + autres stats = 0
    assert e["shots"] == 23 and e["sot"] == 4
    assert stats._entry_from([]) is None and stats._entry_from([{"team": {"id": 1}, "statistics": [{"type": "Fouls", "value": 3}]}] * 2) is None



def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            stats._today = lambda: PIN
            fn()
            print(f"ok {name}")
    print("tous les tests stats passent")


if __name__ == "__main__":
    main()
