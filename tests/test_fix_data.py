"""Tests hors ligne des correctifs de données (aucun réseau). Lancer : python -m tests.test_fix_data

1. cotes de clôture BetExplorer : dates lues (année déduite), une ligne sans date n'arrête plus le remplissage gratuit ;
2. rapprochement des noms d'équipes (translittération, apostrophes, initiales, Saint/Sint, tirets, égalités) ;
3. un seul combiné « à vérifier » par palier et par jour, un seul pari du jour par date ;
4. tracked.json court : les lignes réglées anciennes vont dans data/archive/tracked-AAAA.json ;
5. /monde entre 00:00 et 08:00 UTC lit le relevé du jour Kelly de la veille ;
6. bilan de la semaine : envoyé = r["sent"], quelle que soit la source de la cote finale ;
7. liste des matchs du lendemain téléchargée la veille : rafraîchie une fois ce jour commencé.
"""
import os
import tempfile
import time
from datetime import date, datetime, timedelta, timezone

TMP = tempfile.mkdtemp(prefix="kelly-fixdata-test-")
os.environ["DATA_DIR"] = os.path.join(TMP, "data")
os.environ["SOURCES_CACHE_DIR"] = os.path.join(TMP, "cache")
os.environ["ODDS_CACHE_DIR"] = os.path.join(TMP, "odds")
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ.pop("STATS_API_KEY", None)

from kelly import config as C, fair, freeclose, monde, stats, store, track, weekly  # noqa: E402
from kelly.sources import betexplorer, names, state  # noqa: E402

UTC = timezone.utc


def wipe():
    import shutil
    shutil.rmtree(store.DATA_DIR, ignore_errors=True)


# --- 1. BetExplorer ----------------------------------------------------------------------

def bx_row(home, away, label, score="1:0", odds=(2.0, 3.0, 4.0)):
    o = "".join(f'<td data-odd="{x}"></td>' for x in odds)
    return (f'<tr><td><a href="/football/x/y/{home.lower()}-{away.lower()}/AAAA1111/" class="in-match">'
            f"<span>{home}</span> - <span>{away}</span></a></td><td><a href=\"/x/\">{score}</a></td>{o}"
            f'<td class="h-text-right">{label}</td></tr>')


def test_betexplorer_dates():
    d = betexplorer._date
    assert d("06.10.", date(2026, 10, 8)) == "2026-10-06" and d("07.10. 18:00", date(2026, 10, 8)) == "2026-10-07"
    assert d("Yesterday", date(2026, 10, 8)) == "2026-10-07" and d("Today", date(2026, 10, 8)) == "2026-10-08"
    assert d("30.12.", date(2027, 1, 3)) == "2026-12-30"               # relevé début janvier : décembre de l'année précédente
    assert d("02.01.", date(2027, 1, 3)) == "2027-01-02" and d("05.01.", date(2027, 1, 3)) == "2026-01-05"
    assert d("06.10.2025", date(2026, 10, 8)) == "2025-10-06"          # date complète (anciens matchs)
    assert d("29.02.", date(2026, 3, 1)) == "2025-02-29" or d("29.02.", date(2026, 3, 1)) is None   # jamais d'exception
    assert d("06.10.", None) is None and d("n'importe quoi", date(2026, 10, 8)) is None
    ms = betexplorer.parse_results(bx_row("Zoman", "Korhogo", "30.12.") + bx_row("Isca", "Bouake", "06.10.2025"), today=date(2027, 1, 3))
    assert [m["d"] for m in ms] == ["2026-12-30", "2025-10-06"], ms


def test_betexplorer_refresh_dates_rows():
    """refresh() passait parse_results sans date de relevé : toutes les lignes avaient d=None."""
    state.clock = lambda: datetime(2026, 10, 8, 12, tzinfo=UTC).timestamp()
    html = "<table>" + bx_row("Isca", "Bouake", "06.10.") + bx_row("Zoman", "Korhogo", "Yesterday") + "</table>"

    class Res:
        ok, text, error = True, html, None
    real = betexplorer.http.fetch
    betexplorer.http.fetch = lambda u, name, ttl: Res()
    try:
        betexplorer.refresh(["soccer_epl"])
    finally:
        betexplorer.http.fetch = real
        state.clock = time.time
    ms = betexplorer.profiles()["leagues"]["soccer_epl"]["matches"]
    assert [m["d"] for m in ms] == ["2026-10-06", "2026-10-07"], ms


NOW = datetime(2026, 10, 12, 8, tzinfo=UTC)


def row(key, name, sk, home, away, days=1, prob_book=0.5):
    ko = (NOW - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    return {"key": key, "id": key, "sport_key": sk, "group": "soccer", "home": home, "away": away, "commence": ko, "market": "h2h",
            "name": name, "point": None, "odds": 2.1, "prob": prob_book, "prob_book": prob_book, "book": "Pinnacle", "sent": False,
            "closed_at": None, "clv": None, "date": ko[:10], "result": None}


class FakeFD:
    def match(self, b):
        if b["home"] != "Arsenal":
            return None
        return {"odds": {"1x2": {"close": {"ps": [1.8, 3.8, 4.6]}}}}


def test_freeclose_survives_bad_dates():
    """Une ligne BetExplorer sans date (d=None) levait TypeError : tout le remplissage s'arrêtait, football-data compris."""
    bx_first = row("b", "Draw", "soccer_brazil_campeonato", "Santos", "Flamengo", prob_book=0.3)
    fd_after = row("h", "Arsenal", "soccer_epl", "Arsenal", "Chelsea")
    rows = [bx_first, fd_after]
    day = bx_first["commence"][:10]
    bx = {"leagues": {"soccer_brazil_campeonato": {"matches": [
        {"d": None, "home": "Santos", "away": "Flamengo", "o1": 2.9, "ox": 3.3, "o2": 2.5},
        {"home": "Santos", "away": "Flamengo", "o1": 2.9, "ox": 3.3, "o2": 2.5},                  # sans clé « d »
        {"d": 20261012, "home": "Santos", "away": "Flamengo", "o1": 2.9, "ox": 3.3, "o2": 2.5},    # type absurde
        {"d": "pas une date", "home": "Santos", "away": "Flamengo", "o1": 2.9, "ox": 3.3, "o2": 2.5},
        {"d": day, "home": "Santos", "away": "Flamengo", "o1": 2.9, "ox": 3.3, "o2": 2.5}]}}}
    assert freeclose.fill(rows, NOW, FakeFD(), bx) == 2
    assert bx_first["close_src"] == "betexplorer" and fd_after["close_src"] == "football-data/ps"
    # que des lignes cassées : rien de rempli côté BetExplorer, mais football-data passe quand même
    rows = [row("b2", "Draw", "soccer_brazil_campeonato", "Santos", "Flamengo"), row("h2", "Arsenal", "soccer_epl", "Arsenal", "Chelsea")]
    bx = {"leagues": {"soccer_brazil_campeonato": {"matches": [{"d": None, "home": "Santos", "away": "Flamengo", "o1": 2, "ox": 3, "o2": 4}]}}}
    assert freeclose.fill(rows, NOW, FakeFD(), bx) == 1 and rows[0]["clv"] is None and rows[1]["clv"] is not None


# --- 2. noms d'équipes ---------------------------------------------------------------------

PAIRS = [("Bodø/Glimt", "Bodo/Glimt"), ("Young Boys", "BSC Young Boys"), ("Newell's Old Boys", "Newells Old Boys"),
         ("Estudiantes de La Plata", "Estudiantes L.P."), ("Gimnasia La Plata", "Gimnasia L.P."), ("Cercle Brugge KSV", "Cercle Brugge"),
         ("Sint Truiden", "St Truiden"), ("Saint-Etienne", "St Etienne"), ("Nott'm Forest", "Nottingham Forest"),
         ("Sp Lisbon", "Sporting Lisbon"), ("Sp Lisbon", "Sporting CP"), ("QPR", "Queens Park Rangers"),
         ("Brøndby IF", "Brondby"), ("Lech Poznań", "Lech Poznan"), ("Fenerbahçe", "Fenerbahce"), ("Hertha BSC", "Hertha Berlin")]


def test_team_matching_regressions():
    for a, b in PAIRS:
        for sim in (stats.team_sim, names.team_sim):
            assert sim(a, b) >= 0.8 and sim(b, a) >= 0.8, (a, b, sim(a, b), sim.__module__)
    # ce qui reste séparé
    for a, b in (("Atlético-GO", "Ath Madrid"), ("Grêmio", "Remo"),
                 ("UTS Rabat", "FUS Rabat"), ("Young Boys", "Young Africans"), ("St Johnstone", "St Mirren"),
                 ("Sint Truiden", "Sint Niklaas"), ("Inter Miami", "Inter"), ("Real Madrid", "Real Betis")):
        assert stats.team_sim(a, b) < 0.8 and names.team_sim(a, b) < 0.8, (a, b, stats.team_sim(a, b), names.team_sim(a, b))
    best = names.best_team
    assert best("Cercle Brugge KSV", ["Club Brugge", "Cercle Brugge"]) == "Cercle Brugge"
    assert best("Cercle Brugge KSV", ["Club Brugge KV", "Cercle Brugge"]) == "Cercle Brugge"
    assert best("Club Brugge", ["Club Brugge KV", "Cercle Brugge KSV"]) == "Club Brugge KV"
    assert best("Estudiantes de La Plata", ["Estudiantes L.P.", "Estudiantes Rio Cuarto"]) == "Estudiantes L.P."
    assert best("Gimnasia La Plata", ["Gimnasia L.P.", "Gimnasia Jujuy"]) == "Gimnasia L.P."
    assert best("Estudiantes", ["Estudiantes L.P.", "Estudiantes Rio Cuarto"]) is None            # toujours ambigu
    assert best("Tokyo Verdy", ["FC Tokyo", "Verdy", "Gamba Osaka"]) is None
    assert best("Young Boys", ["BSC Young Boys", "Basel"]) == "BSC Young Boys"
    assert best("Bodø/Glimt", ["Bodo/Glimt", "Molde"]) == "Bodo/Glimt"


# --- 3. un seul combiné / un seul pari du jour par jour ---------------------------------------

def combo(*keys, odds=5.0):
    legs = [{"key": k, "id": k, "market": "h2h", "name": "X", "odds": 2.0, "pick": k} for k in keys]
    return legs, odds, 0.2


def test_fair_combos_one_per_tier_and_day():
    wipe()
    assert fair.record_combos([("🛡️ SÛR", combo("a", "b"))], "2026-10-08", stakes={"🛡️ SÛR": 500}, cur="FCFA") == 1
    # relance du matin : autres jambes, même palier, même jour -> pas de 2e ligne
    assert fair.record_combos([("🛡️ SÛR", combo("a", "c")), ("🎯 ÉQUILIBRÉ", combo("d", "e"))], "2026-10-08") == 1
    rows = store.load("fair_combos.json", [])
    assert [(r["tier"], r["keys"]) for r in rows] == [("🛡️ SÛR", ["a", "b"]), ("🎯 ÉQUILIBRÉ", ["d", "e"])], rows
    assert rows[0]["stake"] == 500
    assert fair.record_combos([("🛡️ SÛR", combo("a", "c"))], "2026-10-09") == 1                  # un autre jour : accepté
    assert fair.record_combos([("🛡️ SÛR", combo("a", "b"))], "2026-10-09") == 0


def pick(i, score=1.0):
    return {"key": f"k{i}", "id": f"e{i}", "sport_key": "soccer_epl", "group": "soccer", "odds": 2.0, "best_odds": 2.0,
            "prob": 0.55, "prob_book": 0.53, "edge": 0.1, "n_books": 14, "weight": 1.0, "score": score, "market": "h2h", "name": "Over",
            "who": None, "point": None, "why": "", "pm": None, "book": "bk", "commence": "2026-10-08T15:00:00Z", "home": f"H{i}",
            "away": f"A{i}", "event": f"H{i} - A{i}", "league": "L", "start": "15:00", "pick": f"Pari {i}"}


def test_single_bet_once_per_day():
    wipe()
    day, now = date(2026, 10, 8), datetime(2026, 10, 8, 8, tzinfo=UTC)
    picks = [pick(0, 5.0), pick(1, 4.0), pick(2, 0.1)]
    track.record(picks, {"k0"}, day, now, single_key="k0")
    # relance du matin : un autre « top » (avec sa mise du budget)
    second = pick(1, 4.0) | {"stake": 800, "cur": "FCFA"}
    track.record([picks[0], second, picks[2]], {"k0", "k1"}, day, now, single_key="k1")
    rows = {r["key"]: r for r in track.load()}
    assert [k for k, r in rows.items() if r.get("single")] == ["k0"], {k: r.get("single") for k, r in rows.items()}
    assert rows["k1"]["sent"] and rows["k1"].get("stake") is None and rows["k1"].get("cur") is None
    from kelly import played
    assert [r["key"] for r in played.single_rows(list(rows.values()), [])] == ["k0"]        # k1 n'est pas retombé sur le repli
    # le second « top » est déjà suivi comme fantôme (ligne connue) : même résultat
    wipe()
    track.record(picks, {"k0"}, day, now, single_key="k0")
    track.record(picks, {"k0", "k2"}, day, now, single_key="k2")
    assert [r["key"] for r in track.load() if r.get("single")] == ["k0"]
    # un autre jour : son propre pari du jour
    track.record([pick(5, 3.0)], {"k5"}, date(2026, 10, 9), now, single_key="k5")
    assert sorted((r["date"], r["key"]) for r in track.load() if r.get("single")) == [("2026-10-08", "k0"), ("2026-10-09", "k5")]


# --- 4. tracked.json court -------------------------------------------------------------------

def old_row(key, d, result="win"):
    return {"key": key, "date": d, "sent": False, "result": result, "commence": d + "T15:00:00Z", "odds": 2.0}


def test_tracked_pruned_to_archive():
    wipe()
    assert C.TRACK_KEEP_DAYS == 180
    rows = [old_row("a", "2025-12-30"), old_row("b", "2026-01-02"), old_row("c", "2025-12-31", result=None),    # non réglé : reste
            old_row("d", "2026-06-01"), old_row("e", "2026-10-08", result="loss"), old_row("f", "2026-10-08", result=None)]
    track.save(rows)
    live = track.load()
    assert [r["key"] for r in live] == ["c", "d", "e", "f"], live
    a25 = store.load("archive/tracked-2025.json", None)
    a26 = store.load("archive/tracked-2026.json", None)
    assert [r["key"] for r in a25] == ["a"] and [r["key"] for r in a26] == ["b"], (a25, a26)
    # idempotent et cumulatif : un second passage n'archive pas deux fois
    track.save(live + [old_row("a", "2025-12-30")])
    assert [r["key"] for r in store.load("archive/tracked-2025.json", [])] == ["a"]
    track.save(live + [old_row("g", "2025-12-29")])
    assert [r["key"] for r in store.load("archive/tracked-2025.json", [])] == ["a", "g"]
    assert [r["key"] for r in track.load()] == ["c", "d", "e", "f"]
    assert not [f for f in os.listdir(os.path.join(store.DATA_DIR, "archive")) if f.endswith(".tmp")]
    # rien d'ancien : fichier courant intact, pas de dossier créé
    wipe()
    track.save([old_row("x", "2026-10-07"), old_row("y", "2026-10-08")])
    assert len(track.load()) == 2 and not os.path.exists(os.path.join(store.DATA_DIR, "archive"))
    # lignes sans date : jamais perdues
    track.save([{"key": "z", "result": "win"}, old_row("y", "2026-10-08")])
    assert len(track.load()) == 2


# --- 5. /monde avant 08:00 UTC ------------------------------------------------------------------

def test_monde_uses_kelly_day():
    wipe()
    assert C.DAY_END_HOUR == 8
    monde.save({"day": "2026-10-08", "at": "08:00", "leagues": {}, "africa": []})
    k = monde.kelly_day
    assert k(datetime(2026, 10, 9, 3, 30, tzinfo=UTC)) == date(2026, 10, 8)                  # nuit : toujours le jour Kelly d'hier
    assert k(datetime(2026, 10, 9, 7, 59, tzinfo=UTC)) == date(2026, 10, 8)
    assert k(datetime(2026, 10, 9, 8, 0, tzinfo=UTC)) == date(2026, 10, 9) and k(datetime(2026, 10, 8, 23, tzinfo=UTC)) == date(2026, 10, 8)
    assert k(datetime(2026, 1, 1, 2, tzinfo=UTC)) == date(2025, 12, 31)
    assert monde.load(k(datetime(2026, 10, 9, 3, 30, tzinfo=UTC)))["day"] == "2026-10-08"
    assert monde.load(k(datetime(2026, 10, 9, 9, tzinfo=UTC))) is None                      # nouveau jour, pas encore de relevé
    from kelly import app
    real = app.now_utc
    app.now_utc = lambda: datetime(2026, 10, 9, 3, 30, tzinfo=UTC)
    try:
        out = app._handle_command("/monde")
    finally:
        app.now_utc = real
    assert "Pas encore de relevé" not in str(out), out


# --- 6. bilan de la semaine -----------------------------------------------------------------------

def test_weekly_sent_vs_silent():
    now = datetime(2026, 10, 12, 8, tzinfo=UTC)
    wipe()
    for f, v in (("weekly.json", {}), ("bets.json", {"picks": [], "combos": []}), ("fair.json", []), ("fair_combos.json", [])):
        store.save(f, v)
    mk = lambda key, sent, **kw: row(key, "Arsenal", "soccer_epl", "Arsenal", "Chelsea") | {"sent": sent, "clv": 0.02, "closed_at": "x"} | kw   # noqa: E731
    store.save("tracked.json", [mk("s1", True), mk("s2", True, close_src="betexplorer", closed_at="free:betexplorer", clv=-0.01),
                                mk("g1", False), mk("g2", False, close_src="football-data/ps", closed_at="free:football-data")])
    msg = weekly.message(now)
    assert "Paris envoyés : cote finale battue 1 fois sur 2 (1 mesurés gratuitement)" in msg, msg
    assert "Paris suivis en silence : 2 sur 2 (1 mesurés gratuitement)" in msg, msg


# --- 7. liste du lendemain préchargée la veille ---------------------------------------------------------

def fx(i, home, away, ts, status="NS"):
    return {"fixture": {"id": i, "timestamp": ts, "status": {"short": status}},
            "teams": {"home": {"id": i * 10, "name": home}, "away": {"id": i * 10 + 1, "name": away}},
            "goals": {"home": None, "away": None},
            "league": {"name": "Ligue 1", "country": "Ivory-Coast"}}


class Resp:
    def __init__(self, data):
        self._d, self.status_code, self.headers = data, 200, {}

    def json(self):
        return self._d


def test_next_day_list_refreshed_once_started():
    wipe()
    D, D1 = "2026-10-08", "2026-10-09"
    t0 = datetime(2026, 10, 9, 15, tzinfo=UTC).timestamp()
    t1 = datetime(2026, 10, 9, 17, tzinfo=UTC).timestamp()
    served = {"v": [fx(1, "Asec", "Africa Sports", int(t0))]}
    calls = []
    os.environ["STATS_API_KEY"] = "test-key"
    stats.requests.get = lambda url, **kw: (calls.append(kw.get("params")), Resp({"errors": [], "response": served["v"]}))[1]

    def run(today):
        stats._today = lambda: today
        stats._calls, stats._halted = 0, False
        stats._refreshed.clear()
        stats._neg.clear()
        stats.BUDGET = 60
    try:
        run(D)
        first = stats._day_fixtures(D1)                                       # préchargement du matin du 8 (D+1)
        assert [f["id"] for f in first] == [1] and len(calls) == 1
        assert [p["date"] for p in calls] == [D1]
        # même jour : relu tel quel
        run(D)
        assert stats._day_fixtures(D1) == first and len(calls) == 1
        # le 9 : la liste a été téléchargée avant le début du 9 -> rafraîchie (horaire décalé), mais seulement après RETRY_HOURS
        served["v"] = [fx(1, "Asec", "Africa Sports", int(t1))]
        meta = store.load("fixtures_day.json", {})
        meta["_fetched"][D1] = datetime(2026, 10, 8, 8, tzinfo=UTC).timestamp()           # téléchargée le 8 à 08:00
        store.save("fixtures_day.json", meta)
        run(D1)
        got = stats._day_fixtures(D1)
        assert len(calls) == 2 and calls[1]["date"] == D1 and got[0]["ts"] == int(t1), (calls, got)
        # une fois par exécution, et plus du tout une fois la liste du jour téléchargée le jour même
        assert stats._day_fixtures(D1) == got and len(calls) == 2
        run(D1)
        assert stats._day_fixtures(D1) == got and len(calls) == 2
        # anti-boucle : liste de la veille récente (moins de FIXTURES_RETRY_HOURS) -> pas de nouvelle requête
        meta = store.load("fixtures_day.json", {})
        meta["_fetched"][D1] = datetime(2026, 10, 8, 23, 59, tzinfo=UTC).timestamp()
        store.save("fixtures_day.json", meta)
        stats.time.time, real = (lambda: datetime(2026, 10, 9, 0, 10, tzinfo=UTC).timestamp()), stats.time.time
        try:
            run(D1)
            stats._day_fixtures(D1)
        finally:
            stats.time.time = real
        assert len(calls) == 2
        # ancien cache sans heure de téléchargement : on ne devine pas
        wipe()
        store.save("fixtures_day.json", {D1: [stats._entry(fx(1, "Asec", "Africa Sports", int(t0)))]})
        run(D1)
        stats._day_fixtures(D1)
        assert len(calls) == 2
        # jamais de date hors fenêtre du forfait gratuit
        run(D)
        n = len(calls)
        stats._day_fixtures("2026-10-12")
        assert stats.refused("/fixtures", {"date": "2026-10-12"}) and len(calls) == n
    finally:
        os.environ.pop("STATS_API_KEY", None)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} tests fix_data : OK")


if __name__ == "__main__":
    main()
