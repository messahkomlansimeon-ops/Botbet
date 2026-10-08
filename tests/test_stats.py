"""Tests hors ligne de kelly/stats.py (aucun réseau). Lancer : python -m tests.test_stats"""
import math
import os
import tempfile
from datetime import date, datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-stats-test-")
os.environ.pop("STATS_API_KEY", None)
os.environ.pop("STATS_DAILY_CAP", None)

from kelly import learn, stats, store  # noqa: E402
from kelly.sources import features  # noqa: E402


class Resp:
    def __init__(self, data, status=200, headers=None):
        self._d, self.status_code, self.headers = data, status, headers or {}

    def json(self):
        return self._d


def reset(fake=None, cap=None):
    stats._calls, stats._halted = 0, False
    stats._refreshed.clear()
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
    assert stats.get("/fixtures", date="2026-10-11") is None
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
                {"player": {"name": "K. Mbappé"}, "statistics": [{"shots": {"total": None, "on": 2}, "fouls": {}}]}]}]})
        if path == "/fixtures":
            n = 5 if p["team"] == 10 else 2
            return Resp({"errors": [], "response": [
                fx(10 + i, "A", "B", T0 - i * 86400, "FT") | {"teams": {"home": {"id": p["team"], "name": "A"},
                                                                        "away": {"id": 99, "name": "B"}}}
                for i in range(n)]})
    calls = reset(fake)
    st = stats.fixture_stats(7)
    assert st["corners"] == 10 and st["cards"] == 2 and st["players"]["K. Mbappé"] == {
        "shots": 0.0, "shots_on_target": 2.0, "fouls": 0.0}
    n = len(calls)
    assert stats.fixture_stats(7) is st or stats.fixture_stats(7) == st and len(calls) == n   # cache
    # moyennes : pas d'appel /players, et None sous 3 matchs
    calls.clear()
    assert stats.team_averages(10, "d")["n"] == 5
    assert not any("/players" in u for u, _ in calls)
    assert stats.team_averages(20, "d") is None                  # 2 matchs seulement
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
    stats._refreshed.clear()                                           # exécution suivante
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
    stat = lambda tid, corners: {"team": {"id": tid}, "statistics": [                                           # noqa: E731
        {"type": "Corner Kicks", "value": corners}, {"type": "Yellow Cards", "value": cards_yellow},
        {"type": "Red Cards", "value": 0}, {"type": "Total Shots", "value": 10}, {"type": "Fouls", "value": 10}]}

    def fake(url, p):
        path = url.replace(stats.BASE, "")
        if path == "/fixtures" and p.get("date"):
            return Resp({"errors": [], "response": [fx(1, "Arsenal", "Chelsea", T0)]})
        if path == "/fixtures":
            base = 100 if p["team"] == 10 else 200
            return Resp({"errors": [], "response": [fx(base + i, "A", "B", T0 - (i + 1) * 86400, "FT") for i in range(5)]})
        if path == "/fixtures/statistics":
            return Resp({"errors": [], "response": [stat(10, 6), stat(11, 4)]})
        return Resp({"errors": [], "response": []})
    return fake


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
    # repli API-Football : sans profil de source, mêmes calculs avec les moyennes des 5 derniers matchs
    calls = reset(api_fake())
    features.reload()
    a = mk_pick("alternate_totals_corners", "Over", 8.5)
    stats.enrich([a], "2026-10-10")
    assert calls and a["pm"]["src"] == "api" and a["pm"]["exp"] == 10.0 and "10.0 corners attendus vs ligne 8.5" in a["why"], (a, calls)
    assert abs(a["prob"] - (0.6 * 0.5 + 0.4 * stats.poisson_over(10.0, 8.5))) < 1e-9
    # sources présentes pour les corners mais pas les cartons : repli API seulement pour ce que les sources n'ont pas
    calls = reset(api_fake())
    put_profiles(us=False)
    store.save("src_footballdata.json", {"teams": {"Arsenal": {"div": "E0", "all": {"n": 10, "corners_for": 7, "corners_against": 4}},
                                                    "Chelsea": {"div": "E0", "all": {"n": 10, "corners_for": 5, "corners_against": 6}}}})
    features.reload()
    co, ca = mk_pick("alternate_totals_corners", "Over", 9.5), mk_pick("alternate_totals_cards", "Over", 3.5)
    stats.enrich([co, ca], "2026-10-10")
    assert co["pm"]["src"] == "sources" and ca["pm"]["src"] == "api" and ca["pm"]["exp"] == 4.0, (co["pm"], ca["pm"])
    assert calls                                                    # l'API n'est interrogée que pour les cartons
    # plafond d'événements pour le repli API
    calls = reset(api_fake())
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


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok {name}")
    print("tous les tests stats passent")


if __name__ == "__main__":
    main()
