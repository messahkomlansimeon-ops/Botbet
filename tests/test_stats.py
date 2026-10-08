"""Tests hors ligne de kelly/stats.py (aucun réseau). Lancer : python -m tests.test_stats"""
import os
import tempfile
from datetime import date, datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-stats-test-")
os.environ.pop("STATS_API_KEY", None)
os.environ.pop("STATS_DAILY_CAP", None)

from kelly import stats, store  # noqa: E402


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


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok {name}")
    print("tous les tests stats passent")


if __name__ == "__main__":
    main()
