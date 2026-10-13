"""Tests hors ligne du lot J (sources externes : API-Football, FotMob, BetExplorer, football-data, caches). Aucun réseau, horloge simulée.
Lancer : python -m tests.test_sources_fix

 1. API-Football : écart de 6,2 s entre deux appels, un 429 = attendre 60 s puis réessayer une fois dans le délai du passage, jamais plus d'un arrêt
    « pour ce passage », `lost` juste, message honnête ;
 2. démarrage à froid : les ligues qui jouent bientôt d'abord, puis la page la plus ancienne, partage du délai entre les sources, pages périmées signalées ;
 3. BetExplorer garde les matchs les plus RÉCENTS ;
 4. FotMob : phase en cours des classements à plusieurs phases (Colombie, Finlande...) ; 5. doublons : les duels rejoués sont tous gardés ;
 6. avantage du terrain sur l'xG du classement FotMob pour toutes les ligues ;
 7. noms : sigles (Atalanta BC / Bristol City), équipes de jeunes, alias sûrs, Qingdao jamais fusionné ;
 8. réponses d'API-Football de type inattendu : jamais d'exception ; 9. /events en panne : relevé partiel, jamais « aucun match » ;
10. caches mémoire du serveur relus quand la base change ; 11. verrou des données non tenu pendant les téléchargements FotMob (--track --own-lock) ;
12. football-data : 3 saisons, réglage : le dernier réglage mesuré n'est pas effacé ; 13. cache des stats atomique, .cache/sources élagué ;
14. cote de clôture gratuite dé-margée comme la proba à la prise (Shin) ; 15. README et configuration d'accord sur les durées.
"""
import contextlib
import io
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone

TMP = tempfile.mkdtemp(prefix="kelly-sources-fix-")
os.environ["DATA_DIR"] = os.path.join(TMP, "data")
os.environ["SOURCES_CACHE_DIR"] = os.path.join(TMP, "cache")
os.environ["STATS_CACHE_DIR"] = os.path.join(TMP, "apistats")
os.environ["ODDS_CACHE_DIR"] = os.path.join(TMP, "odds")
os.environ["XDG_RUNTIME_DIR"] = os.path.join(TMP, "run")
for _k in ("STATS_API_KEY", "STATS_DAILY_CAP", "ODDS_API_KEY", "TELEGRAM_TOKEN", "KELLY_OWN_LOCK", "FOTMOB_ENABLED"):
    os.environ.pop(_k, None)

import requests  # noqa: E402

from kelly import apistats, calib, config as C, freeclose, monde, odds, settle, stats, store, strength, tune  # noqa: E402
from kelly import sources  # noqa: E402
from kelly.sources import betexplorer, features, fmmatch, footballdata as fd, fotmob, http, names, state  # noqa: E402
from tests.fake_api import FakeFootballAPI, match as fake_match, ts_of  # noqa: E402

UTC = timezone.utc
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "sources")
PIN = "2026-10-09"                                  # « aujourd'hui » des tests API-Football
DAY0 = datetime(2026, 10, 8, 4, 0, tzinfo=UTC).timestamp()


# --- outils : horloge simulée, faux réseau, données neuves ------------------------------------------------------------

class Clock:
    """Horloge simulée. Sert de state.clock, de module `time` à http (monotonic, time, sleep) et d'horloge à stats ; `sleep` l'avance."""

    def __init__(self, t=DAY0):
        self.t = t

    def __call__(self):
        return self.t

    def monotonic(self):
        return self.t

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Resp:
    def __init__(self, status=200, body=b"", headers=None, data=None, text=""):
        self.status_code, self.content, self.headers, self._data, self.text = status, body, headers or {}, data, text

    def json(self):
        if self._data is None:
            raise ValueError("pas du JSON")
        return self._data


class Net:
    """requests.get factice pour les sources : routes {url: Resp} ; chaque appel coûte 0,2 s simulée."""

    def __init__(self, clk):
        self.clk, self.routes, self.log = clk, {}, []

    def get(self, url, headers=None, timeout=None, **kw):
        self.clk.t += 0.2
        self.log.append(url)
        return self.routes.get(url) or Resp(404, b"not found")

    def league_hits(self):
        return [int(u.rsplit("/", 1)[1]) for u in self.log if "/leagues/" in u]


def wipe():
    shutil.rmtree(store.DATA_DIR, ignore_errors=True)
    shutil.rmtree(os.environ["SOURCES_CACHE_DIR"], ignore_errors=True)
    shutil.rmtree(os.environ["STATS_CACHE_DIR"], ignore_errors=True)


@contextlib.contextmanager
def sim(t0=DAY0):
    """Horloge, sommeil et réseau simulés pour les sources et pour stats ; tout est remis en place ensuite."""
    clk, net = Clock(t0), None
    saved = (state.clock, http._sleep, http.time, requests.get, stats._clock, stats._sleep, stats._last_call, stats.MIN_GAP, stats.RATE_WAIT,
             stats._today, http._deadline)
    wipe()
    state.clock, http._sleep, http.time = clk, clk.sleep, clk
    stats._clock, stats._sleep, stats._last_call = clk.monotonic, clk.sleep, None
    stats._calls, stats._halted, stats._deferred = 0, False, False
    stats._refreshed.clear(), stats._neg.clear(), stats._refused.clear()
    stats._today = lambda: PIN
    http.reset()
    http.set_deadline(None)
    features.reload()
    net = Net(clk)
    requests.get = net.get
    try:
        yield clk, net
    finally:
        (state.clock, http._sleep, http.time, requests.get, stats._clock, stats._sleep, stats._last_call, stats.MIN_GAP, stats.RATE_WAIT,
         stats._today, http._deadline) = saved
        stats._halted, stats._deferred, stats._calls = False, False, 0
        stats.set_deadline(None)
        http.reset()
        features.reload()


def stderr_of(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        out = fn(*a, **kw)
    return out, buf.getvalue()


# --- 1. API-Football : cadence, 429, lost ---------------------------------------------------------------------------------

class RateApi:
    """requests.get de api-sports.io avec la limite du forfait gratuit : 10 requêtes par minute glissante simulée, 429 au-delà ; `inner`
    répond pour les requêtes admises (FakeFootballAPI) ; `ok_first` : après autant d'appels, tout est refusé en 429."""

    def __init__(self, clk, inner=None, limit=10, ok_first=None, body='{"message":"Too many requests. Your rate limit is 10 requests per minute."}'):
        self.clk, self.inner, self.limit, self.ok_first, self.body = clk, inner, limit, ok_first, body
        self.times, self.n429 = [], 0

    def __call__(self, url, params=None, headers=None, timeout=None, **kw):
        self.clk.t += 0.3
        self.times.append(self.clk.t)
        recent = [t for t in self.times if self.clk.t - t < 60]
        if len(recent) > self.limit or (self.ok_first is not None and len(self.times) > self.ok_first):
            self.n429 += 1
            return Resp(429, data={"message": "Too many requests"}, text=self.body)
        if self.inner:
            return self.inner(url, params=params)
        return Resp(200, data={"errors": [], "response": [{"i": len(self.times)}]})


def start_stats(clk, api, gap=6.2, wait=60):
    stats.requests.get = api
    stats.MIN_GAP, stats.RATE_WAIT = gap, wait
    os.environ["STATS_API_KEY"] = "test-key"
    stats.BUDGET = 500
    stats._calls, stats._halted, stats._last_call, stats._deferred = 0, False, None, False
    stats.set_deadline(None)
    stats._neg.clear()
    store.save("stats_quota.json", {"day": PIN, "n": 0})


def test_api_football_pacing_and_429():
    with sim() as (clk, net):
        # 1) l'écart minimal entre deux appels tient la limite de 10 requêtes par minute : 25 appels, aucun 429
        api = RateApi(clk)
        start_stats(clk, api)
        t0 = clk.t
        for i in range(25):
            assert stats.get("/x", n=i) == [{"i": i + 1}], i
        gaps = [b - a for a, b in zip(api.times, api.times[1:])]
        assert api.n429 == 0 and not stats._halted and min(gaps) >= 6.2, (api.n429, min(gaps))
        assert clk.t - t0 >= 24 * 6.2, clk.t - t0                      # le temps simulé a réellement passé (aucun sommeil réel)
        # 2) un 429 malgré tout (autre appelant sur la même clé) : corps journalisé, attente de 60 s, UN nouvel essai, rien d'arrêté
        api = RateApi(clk, limit=0, ok_first=None)
        calls = {"n": 0}

        def once(url, params=None, **kw):
            calls["n"] += 1
            clk.t += 0.3
            if calls["n"] == 1:
                return Resp(429, data={"message": "Too many requests"}, text='{"message":"Too many requests, rate limit 10/min"}')
            return Resp(200, data={"errors": [], "response": [{"ok": 1}]})
        start_stats(clk, once)
        t0 = clk.t
        out, err = stderr_of(stats.get, "/fixtures/statistics", fixture=7)
        assert out == [{"ok": 1}] and calls["n"] == 2 and not stats._halted and not stats.deferred(), (out, calls)
        assert clk.t - t0 >= 60, clk.t - t0
        assert "limite de fréquence" in err and "Too many requests, rate limit 10/min" in err, err      # le corps du 429 est dans le journal
        # 3) un second 429 : arrêt « pour cette exécution » (le passage suivant repart), pas de troisième essai
        calls["n"] = -10
        start_stats(clk, lambda url, params=None, **kw: (calls.__setitem__("n", calls["n"] + 1), Resp(429, data={}, text="slow down"))[1])
        out, err = stderr_of(stats.get, "/x", z=1)
        assert out is None and stats._halted and calls["n"] == -8 and "pour cette exécution" in err, (out, calls, err)
        stats._halted = False                                           # le processus suivant repart à zéro
        start_stats(clk, RateApi(clk))
        assert stats.get("/x", z=2) is not None and not stats._halted
        # 4) la limite annoncée dans `errors` (HTTP 200) est traitée comme un 429 ; une erreur de plan, elle, arrête tout de suite
        seq = [Resp(200, data={"errors": {"rateLimit": "Too many requests. Your rate limit is 10 requests per minute."}, "response": []}),
               Resp(200, data={"errors": [], "response": [{"ok": 2}]})]
        seen = []
        start_stats(clk, lambda url, params=None, **kw: (seen.append(1), seq.pop(0))[1])
        assert stats.get("/x", z=3) == [{"ok": 2}] and len(seen) == 2 and not stats._halted
        start_stats(clk, lambda url, params=None, **kw: (seen.append(2), Resp(200, data={"errors": {"plan": "Free plans do not have access"}, "response": []}))[1])
        del seen[:]
        assert stats.get("/x", z=4) is None and stats._halted and len(seen) == 1
        # 5) le délai du passage borne les attentes : ni écart ni pause de 60 s au-delà ; ce n'est pas un arrêt de l'API
        api = RateApi(clk, limit=0)
        start_stats(clk, api)
        stats.set_deadline(clk.t + 30)
        t0 = clk.t
        assert stats.get("/x", z=5) is None and stats.deferred() and not stats._halted
        assert clk.t - t0 < 1 and len(api.times) == 1, (clk.t - t0, len(api.times))     # un seul essai, pas de pause de 60 s
        start_stats(clk, RateApi(clk))
        stats._last_call = clk.t
        stats.set_deadline(clk.t + 3)
        assert stats.get("/x", z=6) is None and stats.deferred() and not stats._halted        # l'écart de 6,2 s dépasserait le délai
        stats.set_deadline(None)
        assert stats.get("/x", z=7) is not None and not stats.deferred()


def world_fill(n_matches=40, **api_kw):
    """Un Brésil fictif : `n_matches` matchs terminés hier (équipes toutes différentes) servis par une API-Football qui applique la limite."""
    inner = FakeFootballAPI(today=PIN)
    yesterday = str(date.fromisoformat(PIN) - timedelta(days=1))
    ms = [fake_match(9000 + i, (100 + 2 * i, f"Time {2 * i}"), (101 + 2 * i, f"Time {2 * i + 1}"), ts_of(yesterday, 21)) for i in range(n_matches)]
    inner.add_day(yesterday, ms)
    return inner, ms


def test_apistats_fill_under_rate_limit():
    """Le même passage que l'audit (exp1) : 17 requêtes sur 90 puis arrêt. Maintenant la cadence tient, un 429 attend, `lost` est juste."""
    # a) cadence de 6,2 s : les 40 matchs d'hier passent sans un seul 429, dans les APISTATS_SECONDS, rien de perdu
    with sim() as (clk, net):
        inner, ms = world_fill(40)
        api = RateApi(clk, inner)
        start_stats(clk, api)
        t0 = clk.t
        rep = apistats.fill(PIN, {"leagues": {}})
        assert api.n429 == 0 and rep["added"] == 40 and rep["stop"] is None and rep["lost"] == 0, rep
        assert clk.t - t0 <= C.APISTATS_SECONDS, (clk.t - t0, C.APISTATS_SECONDS)
        assert max(len([t for t in api.times if 0 <= x - t < 60]) for x in api.times) <= 10
    # b) sans cadence (MIN_GAP=0) le forfait renvoie 429 : on attend 60 s et on réessaie, jusqu'au délai du passage ; le reste est « perdu » (hier)
    with sim() as (clk, net):
        inner, ms = world_fill(40)
        api = RateApi(clk, inner)
        start_stats(clk, api, gap=0)
        rep = apistats.fill(PIN, {"leagues": {}}, deadline=clk.t + 100)
        assert api.n429 >= 1 and rep["stop"] == "time" and not stats._halted, (rep, api.n429)
        assert rep["added"] > 8 and rep["lost"] == 40 - rep["added"] > 0, rep          # chaque match d'hier non ajouté est compté perdu
        line = apistats.progress_line(rep)
        assert "délai atteint" in line and "perdus faute de temps" in line and "aujourd'hui" not in line, line
    # c) l'API se ferme en route et ne revient pas : arrêt « pour ce passage » ; `lost` compte tout ce qui restait (avant : 0)
    with sim() as (clk, net):
        inner, ms = world_fill(40)
        api = RateApi(clk, inner, ok_first=10)
        start_stats(clk, api)
        rep = apistats.fill(PIN, {"leagues": {}})
        assert rep["stop"] == "halt" and stats._halted and rep["added"] == 8, rep      # 2 listes + 8 statistiques, puis 429, 60 s, 429
        assert rep["lost"] == 40 - 8, rep
        line = apistats.progress_line(rep)
        assert "API arrêtée pour ce passage" in line and "pour aujourd'hui" not in line and "l'API s'étant arrêtée" in line, line
    # d) le budget du jour garde son message d'origine
    with sim() as (clk, net):
        inner, ms = world_fill(70)
        start_stats(clk, RateApi(clk, inner))
        rep = apistats.fill(PIN, {"leagues": {}}, deadline=clk.t + 100000)
        assert rep["stop"] == "budget" and rep["lost"] > 0 and "perdus faute de budget" in apistats.progress_line(rep)


# --- 2. démarrage à froid : priorité, rotation, partage du délai, pages périmées ------------------------------------------------

KEYS6 = ["soccer_epl", "soccer_france_ligue_one", "soccer_germany_bundesliga", "soccer_italy_serie_a", "soccer_netherlands_eredivisie",
         "soccer_spain_la_liga"]
ROBOTS = Resp(200, b"User-agent: *\nDisallow: /api/\n")


def fm_page(teams, upcoming, n=5):
    rows = [{"name": t, "idx": i + 1, "played": n, "scoresStr": "8-4", "wins": 3, "pts": 10} for i, t in enumerate(teams)]
    am = [{"id": "1", "roundName": 5, "status": {"finished": True, "utcTime": "2026-09-27T19:00:00Z", "scoreStr": "1 - 0"},
           "home": {"name": teams[0], "id": 1}, "away": {"name": teams[1], "id": 2}},
          {"id": "2", "roundName": 6, "status": {"finished": False, "utcTime": upcoming + "T19:00:00Z"},
           "home": {"name": teams[1], "id": 2}, "away": {"name": teams[0], "id": 1}}]
    nd = {"props": {"pageProps": {"table": [{"data": {"table": {"all": rows}}}], "fixtures": {"allMatches": am}}}}
    return ('<html><script id="__NEXT_DATA__" type="application/json">' + json.dumps(nd) + "</script></html>").encode()


@contextlib.contextmanager
def fotmob_world(net, soon="soccer_spain_la_liga"):
    """FotMob réduit à 6 ligues (pas de championnats 1xBet) ; la ligue `soon` a un match demain (10/10/2026)."""
    saved = (fotmob.EXTRA_LEAGUES, fotmob.LEAGUES, sources.SOURCES)
    fotmob.EXTRA_LEAGUES, fotmob.LEAGUES, sources.SOURCES = {}, set(KEYS6), (fotmob,)
    net.routes["https://www.fotmob.com/robots.txt"] = ROBOTS
    for k in KEYS6:
        net.routes[f"https://www.fotmob.com/leagues/{fotmob.SPORT_TO_LEAGUE[k]}"] = Resp(
            200, fm_page([f"{k[7:12]} A", f"{k[7:12]} B"], "2026-10-10" if k == soon else "2026-10-25"))
    try:
        yield
    finally:
        fotmob.EXTRA_LEAGUES, fotmob.LEAGUES, sources.SOURCES = saved


def test_cold_start_priority_rotation_and_stale():
    ids = {k: fotmob.SPORT_TO_LEAGUE[k] for k in KEYS6}
    with sim() as (clk, net), fotmob_world(net):
        http.set_deadline(None)
        sources.refresh_all(KEYS6)                                       # jour 0 : tout est téléchargé
        assert sorted(net.league_hits()) == sorted(ids.values()) and fotmob.health()["ok"]
        week = {}
        for day in (1, 2, 3):                                            # 3 matins avec un délai pour 3 pages seulement
            clk.t += 24 * 3600
            http.reset()
            del net.log[:]
            http.set_deadline(15)                                        # 6 s entre deux pages FotMob : 3 pages
            sources.refresh_all(KEYS6)
            hits = net.league_hits()
            week[day] = hits
            assert len(hits) == 3, (day, hits)
            # pages non rafraîchies : servies depuis le cache mais SIGNALÉES (profil, santé, rapport)
            prof = fotmob.profiles()["leagues"]
            stale = sorted(k for k, v in prof.items() if v.get("stale"))
            assert len(stale) == 3 and state.stale_count("fotmob") == 3, (day, stale)
            assert all(ids[k] not in hits for k in stale), (day, stale, hits)
            assert "3 ligue(s) servie(s) depuis un cache périmé" in (fotmob.health()["last_error"] or ""), fotmob.health()
            assert "cache périmé" in sources.report(), sources.report()
        # le match de demain (La Liga, 10/10) la fait passer en premier le matin du 9 puis du 10 ; ensuite la page la plus ancienne d'abord
        assert week[1][0] == ids["soccer_spain_la_liga"] and week[2][0] == ids["soccer_spain_la_liga"], week
        assert week[3][0] != ids["soccer_spain_la_liga"], week
        every = {i for hits in week.values() for i in hits}
        assert every == set(ids.values()), "chaque ligue est actualisée au moins une fois en 3 jours (avant : toujours les 3 mêmes)"
        assert set(week[1][1:]) != set(week[2][1:]) != set(week[3][1:]), week          # le début de liste tourne
        # une page fraîche n'est pas signalée : budget suffisant -> plus aucune page périmée
        clk.t += 24 * 3600
        http.reset()
        http.set_deadline(None)
        sources.refresh_all(KEYS6)
        assert state.stale_count("fotmob") == 0 and not any(v.get("stale") for v in fotmob.profiles()["leagues"].values())
        assert "cache périmé" not in (fotmob.health()["last_error"] or "")


def test_budget_is_shared_between_sources():
    """À froid, football-data (en tête) ne mange plus tout le délai : chaque source a sa part (SLICE_WEIGHTS), le reliquat passe aux suivantes."""
    class Stub:
        def __init__(self, name, spend):
            self.NAME, self.LABEL, self.spend, self.slice = name, name, spend, None

        def refresh(self, keys):
            self.slice = http.get_deadline() - http.monotonic()
            http.time.sleep(min(self.spend, self.slice))
            return state.record_run(self.NAME, 1, [])

        def health(self):
            return state.health(self.NAME)
    names_ = ["footballdata", "understat", "fbref", "fotmob", "betexplorer"]
    with sim() as (clk, net):
        stubs = [Stub(n, 10_000 if n in ("footballdata", "fotmob") else 1) for n in names_]
        saved = sources.SOURCES
        sources.SOURCES = tuple(stubs)
        try:
            http.set_deadline(720)
            sources.refresh_all(["soccer_epl"])
            assert http.get_deadline() is not None                      # le délai global est remis en place pour la suite du passage
        finally:
            sources.SOURCES = saved
        fd_, us_, fb_, fm_, be_ = (s.slice for s in stubs)
        assert abs(fd_ - 720 * 3 / 12) < 1, fd_                          # football-data : 3/12 du délai, pas 100 %
        assert be_ >= 100, stubs[-1].slice                               # BetExplorer (en queue, 0 requête avant) reçoit sa part
        assert fm_ > 4 / 12 * 720 * 0.9, fm_                             # le temps que understat / FBref n'utilisent pas passe à FotMob
        assert abs((http.get_deadline() - http.monotonic()) - (720 - (fd_ + 1 + 1 + fm_))) < 2, "délai global restitué"


def test_betexplorer_stale_flag_and_priority_order():
    bx_urls = {k: betexplorer.results_url(k) for k in ("soccer_epl", "soccer_spain_la_liga", "soccer_italy_serie_a")}
    html = fixture_bytes("betexplorer_ci_results.html")
    with sim() as (clk, net):
        saved = (betexplorer.EXTRA_PATHS, betexplorer._LEAGUE_PATHS)
        betexplorer.EXTRA_PATHS, betexplorer._LEAGUE_PATHS = {}, set(betexplorer.PATHS)
        try:
            net.routes["https://www.betexplorer.com/robots.txt"] = ROBOTS
            for u in bx_urls.values():
                net.routes[u] = Resp(200, html)
            keys = list(bx_urls)
            betexplorer.refresh(keys)
            assert sorted(u for u in net.log if "/results/" in u) == sorted(bx_urls.values())
            clk.t += 24 * 3600
            http.reset()
            del net.log[:]
            http.set_deadline(8)                                          # 5 s entre deux pages BetExplorer : 2 pages
            state.set_priority({"soccer_italy_serie_a"})
            try:
                betexplorer.refresh(keys)
            finally:
                state.set_priority(())
                http.set_deadline(None)
            got = [u for u in net.log if "/results/" in u]
            assert got == [bx_urls["soccer_italy_serie_a"], bx_urls["soccer_epl"]], got          # priorité d'abord, puis la page la plus ancienne
            prof = betexplorer.profiles()["leagues"]
            assert sorted(k for k, v in prof.items() if v.get("stale")) == ["soccer_spain_la_liga"], prof
            assert state.stale_count("betexplorer") == 1 and "cache périmé" in (betexplorer.health()["last_error"] or "")
        finally:
            betexplorer.EXTRA_PATHS, betexplorer._LEAGUE_PATHS = saved


def fixture_bytes(name):
    with open(os.path.join(FIX, name), "rb") as f:
        return f.read()


# --- 3. BetExplorer : les matchs les plus récents -------------------------------------------------------------------------

def bx_row(home, away, label, score="1:0", odds=(2.0, 3.0, 4.0)):
    o = "".join(f'<td data-odd="{x}"></td>' for x in odds)
    return (f'<tr><td><a href="/football/x/y/{home.lower()}-{away.lower()}/AAAA1111/" class="in-match">'
            f"<span>{home}</span> - <span>{away}</span></a></td><td><a href=\"/x/\">{score}</a></td>{o}"
            f'<td class="h-text-right">{label}</td></tr>')


def test_betexplorer_keeps_newest_matches():
    today = date(2026, 10, 8)
    # la page /results/ liste le plus récent EN PREMIER (289 matchs au Brésil) ; l'ancien code gardait matches[-80:] = les plus anciens
    rows = "".join(bx_row(f"Home{k}", f"Away{k}", (today - timedelta(days=k)).strftime("%d.%m.")) for k in range(120))
    ms = betexplorer.parse_results("<table>" + rows + "</table>", today)
    assert ms[0]["d"] == "2026-10-08" and ms[-1]["d"] == str(today - timedelta(days=119))
    kept = betexplorer._keep(ms)
    ds = [m["d"] for m in kept]
    assert len(kept) == betexplorer.BETEXPLORER_KEEP == 80
    assert ds[-1] == "2026-10-08" and ds[0] == str(today - timedelta(days=79)), (ds[0], ds[-1])    # les 80 DERNIERS jours, pas janvier-avril
    assert ds == sorted(ds), "rangés du plus ancien au plus récent, comme FotMob"
    assert str(today - timedelta(days=100)) not in ds
    # bout en bout : le profil écrit garde le match d'hier, et la cote de clôture gratuite le retrouve
    with sim(datetime(2026, 10, 8, 12, tzinfo=UTC).timestamp()) as (clk, net):
        net.routes["https://www.betexplorer.com/robots.txt"] = ROBOTS
        net.routes[betexplorer.results_url("soccer_epl")] = Resp(200, ("<table>" + rows + "</table>").encode())
        saved = (betexplorer.EXTRA_PATHS, betexplorer._LEAGUE_PATHS)
        betexplorer.EXTRA_PATHS, betexplorer._LEAGUE_PATHS = {}, set(betexplorer.PATHS)
        try:
            betexplorer.refresh(["soccer_epl"])
        finally:
            betexplorer.EXTRA_PATHS, betexplorer._LEAGUE_PATHS = saved
        stored = betexplorer.profiles()["leagues"]["soccer_epl"]["matches"]
        assert len(stored) == 80 and stored[-1]["home"] == "Home0" and "Home119" not in {m["home"] for m in stored}


# --- 4 et 5. FotMob : phase en cours, doublons ----------------------------------------------------------------------------

def next_data(page_props):
    return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({"props": {"pageProps": page_props}}, ensure_ascii=False) + "</script>"


def test_fotmob_current_stage_of_multi_stage_tables():
    pages = json.loads(open(os.path.join(FIX, "fotmob_stages_real.json"), encoding="utf-8").read())    # extraits de vraies pages (9/10/2026)
    # Colombie : tables = [Clausura (en cours, 11 à 14 matchs), Apertura (terminé, 19 matchs)] ; l'ancien code gardait la DERNIÈRE (Apertura)
    data, probs = fotmob.parse_league(next_data(pages["colombia"]))
    assert not probs
    t = data["teams"]
    assert t["América de Cali"]["n"] == 12 and t["América de Cali"]["gf"] == 24 and t["América de Cali"]["ga"] == 7, t["América de Cali"]
    assert 15.3 < t["América de Cali"]["xg"] < 15.4                       # xG de la MÊME sous-table (Clausura)
    assert all(v["n"] < 19 for k, v in t.items() if k in ("América de Cali", "Deportivo Cali", "Bucaramanga")), t
    assert data["stages"][0] == "Clausura"
    # Finlande : poules de titre / de relégation (26 et 25 matchs, cumul de la saison) devant la saison régulière (22 matchs)
    data, _ = fotmob.parse_league(next_data(pages["finland"]))
    assert data["teams"]["KuPS"]["n"] == 26 and data["teams"]["KuPS"]["gf"] == 41, data["teams"]["KuPS"]
    assert data["teams"]["FC Lahti"]["n"] == 25
    # la phase en cours se reconnaît aussi par les tours (« Clausura - Round 3 »), même si la page liste la plus ancienne en premier
    def sub(name, n, scores):
        return {"leagueName": name, "table": {"all": [{"name": "Alpha", "played": n, "scoresStr": scores, "wins": 1, "pts": 3},
                                                      {"name": "Beta", "played": n, "scoresStr": "5-5", "wins": 1, "pts": 3}]}}
    page = {"table": [{"data": {"composite": True, "tables": [sub("Apertura", 19, "40-20"), sub("Clausura", 4, "6-3")]}}],
            "fixtures": {"allMatches": [
                {"id": "1", "roundName": "Apertura - Final", "status": {"finished": True, "utcTime": "2026-06-01T20:00:00Z", "scoreStr": "1 - 0"},
                 "home": {"name": "Alpha"}, "away": {"name": "Beta"}},
                {"id": "2", "roundName": "Clausura - Round 4", "status": {"finished": True, "utcTime": "2026-10-04T20:00:00Z", "scoreStr": "2 - 0"},
                 "home": {"name": "Beta"}, "away": {"name": "Alpha"}}]}}
    data, _ = fotmob.parse_league(next_data(page))
    assert data["teams"]["Alpha"]["n"] == 4 and data["teams"]["Alpha"]["gf"] == 6, data["teams"]["Alpha"]
    # tout début de phase (1 match joué) : la phase précédente, plus fournie, sert de repli (un xG par match ne se calcule pas sur 1 match)
    page["table"][0]["data"]["tables"][1] = sub("Clausura", 1, "1-0")
    data, _ = fotmob.parse_league(next_data(page))
    assert data["teams"]["Alpha"]["n"] == 19, data["teams"]["Alpha"]
    # ligue à table unique : inchangée
    one = {"table": [{"data": {"table": {"all": sub("x", 7, "9-4")["table"]["all"]}}}], "fixtures": {"allMatches": []}}
    data, _ = fotmob.parse_league(next_data(one))
    assert data["teams"]["Alpha"]["n"] == 7 and "stages" not in data


def test_fotmob_keeps_recent_meetings():
    """Celtic - Rangers jouée 3 fois dans la saison : l'ancien code gardait la 1re (la plus ancienne) et ignorait les suivantes."""
    def m(i, d, h, a, s):
        return {"id": str(i), "roundName": i, "home": {"name": h}, "away": {"name": a},
                "status": {"finished": True, "utcTime": d + "T19:00:00Z", "scoreStr": s}}
    meetings = [m(1, "2026-08-10", "Celtic", "Rangers", "2 - 0"), m(2, "2026-09-20", "Celtic", "Rangers", "1 - 1"),
                m(3, "2026-10-04", "Celtic", "Rangers", "3 - 1")]
    filler = [m(100 + i, f"2026-09-{1 + i % 28:02d}", f"Team{i}", f"Other{i}", "1 - 0") for i in range(40)]
    page = {"table": [{"data": {"table": {"all": [{"name": "Celtic", "played": 3, "scoresStr": "6-2", "wins": 2, "pts": 7},
                                                  {"name": "Rangers", "played": 3, "scoresStr": "2-6", "wins": 0, "pts": 1}]}}}],
            "fixtures": {"allMatches": meetings[:1] + filler + meetings[1:]}}      # ordre de la page quelconque : trié par date ensuite
    data, _ = fotmob.parse_league(next_data(page))
    ms = data["matches"]
    cr = [x for x in ms if x["home"] == "Celtic"]
    assert [(x["d"], x["hg"], x["ag"]) for x in cr] == [("2026-08-10", 2, 0), ("2026-09-20", 1, 1), ("2026-10-04", 3, 1)], cr
    assert [x["d"] for x in ms] == sorted(x["d"] for x in ms)
    built = fotmob.build(data)["matches"]
    assert len(built) == fotmob.FOTMOB_KEEP == 30 and built[-1]["d"] == max(x["d"] for x in ms)
    assert any(x["home"] == "Celtic" and x["d"] == "2026-10-04" for x in built), "le duel le plus récent est gardé"
    # le règlement par FotMob (settle.fotmob_score) retrouve le dernier match, pas le premier
    b = {"id": "b1", "home": "Celtic", "away": "Rangers", "commence": "2026-10-04T19:00:00Z", "sport_key": "soccer_spl"}
    res = settle.fotmob_score(b, {"soccer_spl": {"matches": built}})
    assert [float(s["score"]) for s in res["scores"]] == [3.0, 1.0], res
    # deux matchs de même id : un seul (les coupes figurent dans plusieurs ligues)
    page["fixtures"]["allMatches"] = [meetings[2], dict(meetings[2])]
    assert len(fotmob.parse_league(next_data(page))[0]["matches"]) == 1


# --- 6. avantage du terrain sur l'xG du classement ------------------------------------------------------------------------

def test_standings_xg_gets_home_advantage_in_every_league():
    """Incheon - Pohang (K League, ligue « mappée » : pas de 1xBet) : le classement mêle domicile et extérieur, donc symétrique ; l'avantage du terrain
    du championnat n'était remis que pour les ligues 1xBet."""
    sk = "soccer_korea_kleague1"
    assert sk in fotmob.SPORT_TO_LEAGUE and sk not in fotmob.EXTRA_LEAGUES
    teams = {"Incheon United": {"n": 30, "gf": 36, "ga": 36, "xg": 36.0, "xga": 36.0}, "Pohang Steelers": {"n": 30, "gf": 36, "ga": 36, "xg": 36.0, "xga": 36.0}}
    ms = [{"d": "2026-09-0%d" % (1 + i % 9), "home": "A%d" % i, "away": "B%d" % i, "hg": 3, "ag": 1} for i in range(30)]       # 3/4 des buts à domicile
    features.reload()
    features._cache["fm"] = {"leagues": {sk: {"teams": teams, "matches": ms}}}
    f = features.match_features("Incheon United", "Pohang Steelers", sk)
    hs = features.home_share(ms)
    assert hs > 0.6
    assert f["exp_xg_home"] == round(1.2 * 2 * hs, 2) and f["exp_xg_away"] == round(1.2 * 2 * (1 - hs), 2), f
    assert f["exp_xg_home"] > f["exp_xg_away"] and abs(f["exp_xg_total"] - 2.4) < 0.02            # le total n'a pas bougé : seule la répartition
    # sans résultats récents : la moyenne mondiale (55,5 %) plutôt que le symétrique
    features._cache["fm"] = {"leagues": {sk: {"teams": teams, "matches": []}}}
    f = features.match_features("Incheon United", "Pohang Steelers", sk)
    assert f["exp_xg_home"] > f["exp_xg_away"]
    features.reload()


# --- 7. noms d'équipes ----------------------------------------------------------------------------------------------------

def test_team_names_false_positives_aliases_and_youth():
    sim = stats.team_sim
    # sigles : « bc » épelle Bristol City et Barracas Central sans que rien d'autre ne rapproche les noms (0,88 avant)
    for other in ("Bristol City", "Barracas Central"):
        assert sim("Atalanta BC", other) < 0.8 and names.team_sim("Atalanta BC", other) < 0.8, (other, sim("Atalanta BC", other))
    assert sim("Atalanta BC", "Atalanta") >= 0.8
    assert names.best_team("Atalanta BC", ["Bristol City", "Barracas Central", "Atalanta"]) == "Atalanta"     # le mot distinctif commun l'emporte
    assert names.best_team("Atalanta BC", ["Bristol City", "Barracas Central"]) is None
    # les vrais sigles marchent toujours (ancre commune)
    for a, b in (("Estudiantes LP", "Estudiantes La Plata"), ("Gimnasia L P", "Gimnasia La Plata"), ("Estudiantes L.P.", "Estudiantes de La Plata")):
        assert sim(a, b) >= 0.99, (a, b, sim(a, b))
    # équipes de jeunes / réserves : jamais l'équipe première
    for a, b in (("Jong Ajax", "Ajax"), ("Real Madrid Castilla", "Real Madrid"), ("Newcastle Next Gen", "Newcastle"), ("Barcelona B", "Barcelona"),
                 ("Bayern Munich II", "Bayern Munich"), ("Newcastle U21", "Newcastle"), ("Manchester United U-21", "Manchester United"),
                 ("Juventus Next Gen", "Juventus"), ("Jong PSV", "PSV"), ("Bayern München Amateure", "Bayern München")):
        assert sim(a, b) == 0 and names.team_sim(a, b) == 0, (a, b, sim(a, b))
    assert sim("Jong Ajax", "Jong Ajax") == 1.0 and sim("Jong PSV", "Jong Ajax") < 0.5
    # alias sûrs (même club, noms différents selon les sources)
    for a, b in (("Pau FC", "PAU"), ("Stade Lavallois", "Laval"), ("Al-Qadsiah", "Al-Qadisiyah FC"), ("Shanghai SIPG FC", "Shanghai Port"),
                 ("Shenzhen Peng City FC", "Shenzhen Xinpengcheng"), ("Henan FC", "Henan Songshan Longmen"),
                 ("Chongqing Tongliang Long", "Chongqing Tonglianglong")):
        assert sim(a, b) >= 0.99 and names.team_sim(a, b) >= 0.99, (a, b, sim(a, b))
    # Qingdao : trois clubs DIFFÉRENTS, jamais fusionnés
    for a, b in (("Qingdao Hainiu", "Qingdao Jonoon"), ("Qingdao Hainiu FC", "Qingdao West Coast"), ("Qingdao Jonoon", "Qingdao West Coast")):
        assert sim(a, b) < 0.8 and names.team_sim(a, b) < 0.8, (a, b, sim(a, b))
    assert names.best_team("Qingdao Jonoon", ["Qingdao Hainiu", "Qingdao West Coast"]) is None
    # deux Shanghai restent deux clubs
    assert sim("Shanghai Port", "Shanghai Shenhua") < 0.8


# --- 8. réponses d'API-Football de type inattendu -------------------------------------------------------------------------

def test_api_football_garbage_never_raises():
    cases = {"JSON liste": [], "JSON chaîne": "Service Unavailable", "JSON nombre": 7, "response = chaîne": {"errors": [], "response": "oops"},
             "response = dict": {"errors": [], "response": {"a": 1}}, "fixture sans teams": {"errors": [], "response": [
                 {"fixture": {"id": 1, "timestamp": 1, "status": {"short": "FT"}}, "league": {}, "goals": {}}]},
             "équipe sans nom": {"errors": [], "response": [{"fixture": {"id": 1, "timestamp": 1, "status": {"short": "FT"}}, "league": {}, "goals": {},
                                                             "teams": {"home": {"id": 1, "name": None}, "away": {"id": 2, "name": None}}}]},
             "élément = nombre": {"errors": [], "response": [1, 2, 3]}, "errors = chaîne": {"errors": "boom", "response": []}}
    tomorrow = "2026-10-10"
    for name, payload in cases.items():
        with sim() as (clk, net):
            os.environ["STATS_API_KEY"] = "test-key"
            stats.MIN_GAP = 0
            store.save("stats_quota.json", {"day": PIN, "n": 0})
            stats.requests.get = lambda *a, p=payload, **k: Resp(200, data=p)
            out, err = stderr_of(stats._day_fixtures, tomorrow)             # ne lève jamais
            assert out == [], (name, out)
            assert store.load("fixtures_day.json", {}).get(tomorrow) in (None, []), name    # le cache n'est pas empoisonné
            assert stats._halted == (name == "errors = chaîne"), name        # une erreur annoncée par l'API arrête ; une réponse illisible, non
            # statistiques d'un match : une réponse de type inattendu est une erreur (réessayée), pas « pas de statistiques » mémorisé
            stats._halted = False
            stats._neg.clear()
            stats.requests.get = lambda *a, p=payload, **k: Resp(200, data=p)
            assert stats.fixture_stats(4242, players=False) is None
            if name in ("JSON liste", "JSON chaîne", "JSON nombre", "response = chaîne", "response = dict", "élément = nombre"):
                assert "4242" not in stats._stats_cache(), name
    with sim() as (clk, net):                                               # deux éléments de type inattendu (pas des équipes)
        os.environ["STATS_API_KEY"] = "test-key"
        stats.MIN_GAP = 0
        store.save("stats_quota.json", {"day": PIN, "n": 0})
        stats.requests.get = lambda *a, **k: Resp(200, data={"errors": [], "response": [1, 2]})
        assert stats.fixture_stats(4343, players=False) is None and "4343" not in stats._stats_cache()
    # jusqu'au règlement du matin (exp3b de l'audit) : un JSON de type inattendu ne fait pas lever settle_pending
    with sim() as (clk, net):
        os.environ["STATS_API_KEY"] = "test-key"
        stats.MIN_GAP = 0
        now = datetime.now(UTC)
        yday = (now - timedelta(days=1)).replace(hour=19, minute=0, second=0, microsecond=0)
        row = {"key": "k1", "id": "e1", "sport_key": "soccer_epl", "group": "soccer", "event": "A - B", "home": "Arsenal", "away": "Chelsea",
               "commence": yday.strftime("%Y-%m-%dT%H:%M:%SZ"), "market": "h2h", "who": None, "point": None, "name": "Arsenal", "pick": "x",
               "odds": 2.0, "sent": True, "result": None, "closed_at": None, "date": str(yday.date())}
        store.save("tracked.json", [row])
        store.save("fixtures_day.json", {})
        store.save("fixtures.json", {})
        stats._today = lambda: str(now.date())
        store.save("stats_quota.json", {"day": str(now.date()), "n": 0})
        stats.requests.get = lambda *a, **k: Resp(200, data=[])
        _, err = stderr_of(settle.settle_pending, now)                      # AttributeError: 'list' object has no attribute 'get' avant
        assert "type inattendu" in err or "illisible" in err, err


# --- 9. /events en panne : relevé partiel, jamais « aucun match » ---------------------------------------------------------------

def test_events_failures_are_reported():
    day, now = date(2026, 10, 9), datetime(2026, 10, 9, 4, 5, tzinfo=UTC)
    sports = [{"key": k} for k in ("soccer_epl", "soccer_spain_la_liga", "soccer_italy_serie_a")]
    real = odds.league_events

    def events(down, found=()):
        def fake(key, window):
            if key in down:
                raise odds.OddsError("réseau : injoignable (simulé)")
            return [{"id": "e1", "home_team": "A", "away_team": "B", "commence_time": "2026-10-09T15:00:00Z"}] if key in found else []
        return fake
    try:
        # a) tout en panne : une erreur de relevé (comme /sports), pas « 0 match »
        odds.league_events = events({s["key"] for s in sports})
        scan, _ = stderr_of(odds.scan_today, day, now, sports)
        assert scan["error"] and scan["complete"] is False and len(scan["failed"]) == 3 and not scan["leagues"], scan
        text = monde.tour_du_monde(monde.build(scan, day, now))
        assert "Impossible de lister les matchs du jour" in text and "Aucun match aujourd'hui" not in text, text
        # b) une ligue sur trois en panne et aucun match ailleurs : relevé partiel, surtout pas « 😴 Aucun match aujourd'hui »
        odds.league_events = events({"soccer_epl"})
        scan, _ = stderr_of(odds.scan_today, day, now, sports)
        assert scan["error"] is None and scan["complete"] is False and scan["failed"] == ["soccer_epl"], scan
        data = monde.build(scan, day, now)
        assert data["fail"] == 1 and not data["complete"]
        text = str(monde.tour_du_monde(data))
        assert "Aucun match relevé" in text and "1 ligue(s)" in text and "😴" not in text, text
        assert "relevé partiel (1 ligue en erreur)" in monde.fmt_monde(data) and "Aucun match relevé : 1 ligue(s) n'ont pas pu" in monde.fmt_monde(data)
        # c) une ligue en panne, des matchs ailleurs : le message liste les matchs et prévient que le relevé est partiel
        odds.league_events = events({"soccer_epl"}, found={"soccer_italy_serie_a"})
        scan, _ = stderr_of(odds.scan_today, day, now, sports)
        text = str(monde.tour_du_monde(monde.build(scan, day, now)))
        assert "Relevé partiel (1 ligue en erreur" in text and "Serie A" in text, text
        # d) tout va bien et rien ne joue : le message de journée sans match reste
        odds.league_events = events(set())
        scan, _ = stderr_of(odds.scan_today, day, now, sports)
        assert scan["failed"] == [] and scan["complete"] is True and scan["error"] is None
        assert "😴 Aucun match aujourd'hui" in str(monde.tour_du_monde(monde.build(scan, day, now)))
    finally:
        odds.league_events = real


# --- 10. caches mémoire d'un serveur qui tourne en continu -------------------------------------------------------------------

def test_long_running_process_sees_what_other_processes_write():
    saved = (calib.RECHECK_SECONDS, features.RECHECK_SECONDS, strength.STAMP_RECHECK_SECONDS)
    calib.RECHECK_SECONDS = features.RECHECK_SECONDS = strength.STAMP_RECHECK_SECONDS = 0       # sans la limite d'une interrogation par seconde
    try:
        _long_running_checks()
        # avec elle (valeur de production) : une écriture d'un autre processus est vue dès la seconde suivante, sans interroger la base à chaque appel
        calib.RECHECK_SECONDS = features.RECHECK_SECONDS = strength.STAMP_RECHECK_SECONDS = 1.0
        t = {"now": 1000.0}
        calib._clock = features._clock = strength._stamp_clock = lambda: t["now"]
        calib._memo.clear()
        features.reload()
        store.save("calib.json", {"h2h": [100, 70, 65.0]})
        k1, calls = calib.factor("h2h"), []
        real_version = store.version
        store.version = lambda name: (calls.append(name), real_version(name))[1]
        try:
            for _ in range(500):
                calib.factor("h2h")
                features._p("fm", fmmatch)
            assert len(calls) <= 2, len(calls)                                    # 1000 appels, au plus une interrogation de la base par source
            store.save("calib.json", {"h2h": [400, 160, 260.0]})
            assert calib.factor("h2h") == k1                                       # moins d'une seconde : encore la mémoire
            t["now"] += 1.5
            assert calib.factor("h2h") != k1                                       # plus d'une seconde : relu
        finally:
            store.version = real_version
    finally:
        calib.RECHECK_SECONDS, features.RECHECK_SECONDS, strength.STAMP_RECHECK_SECONDS = saved
        calib._clock = features._clock = strength._stamp_clock = time.monotonic
        features.reload()


def _long_running_checks():
    with sim() as (clk, net):
        calib._memo.clear()
        store.save("calib.json", {"h2h": [100, 70, 65.0]})
        k1 = calib.factor("h2h")
        store.save("calib.json", {"h2h": [400, 160, 260.0]})              # ce que le règlement de 04:00 (un AUTRE processus) écrit
        k2 = calib.factor("h2h")
        assert abs(k1 - k2) > 0.05, (k1, k2)                                # avant : k2 == k1 tant que le processus tournait
        rows = [{"market": "h2h", "prob": 0.7, "p0": 0.7, "result": "win", "sport_key": "soccer_epl"}] * 3
        calib.add([dict(r) for r in rows])
        assert calib.load()["h2h"][0] == 403                                # ce que ce processus a écrit lui-même est déjà en mémoire
        # profils de sources
        row = [250101, 1] + [3, 1] * 10
        doc = {"teams": {"1": {"nm": "Alpha FC", "sk": ["x"], "d": "2026-10-09", "m": [row, row, row]}}, "seen": {}, "recent": {}}
        store.save("fm_matches.json", doc)
        p1 = features._p("fm", fmmatch)
        assert sorted(p1["teams"]) == ["Alpha FC"] and features._p("fm", fmmatch) is p1          # pas de relecture inutile
        doc2 = {"teams": {"1": doc["teams"]["1"], "2": {"nm": "Beta FC", "sk": ["x"], "d": "2026-10-09", "m": [row] * 4}}, "seen": {}, "recent": {}}
        store.save("fm_matches.json", doc2)
        assert sorted(features._p("fm", fmmatch)["teams"]) == ["Alpha FC", "Beta FC"], "profils FotMob relus après l'écriture d'un autre processus"
        features._cache["aps"] = {"teams": {"Posé à la main": {}}}                                 # profils fictifs des tests : jamais écrasés
        assert features._p("aps", apistats) == {"teams": {"Posé à la main": {}}}
        # empreinte des CSV du modèle de force
        strength.reload()
        store.save(fd.PROFILE_FILE, {"updated": "2026-10-09T04:00", "teams": {}})
        assert strength._stamp() == "2026-10-09T04:00"
        store.save(fd.PROFILE_FILE, {"updated": "2026-10-10T04:00", "teams": {}})
        assert strength._stamp() == "2026-10-10T04:00", "l'empreinte suit le document (avant : celle du démarrage du serveur)"
        features.reload()


# --- 11. verrou des données pendant les téléchargements FotMob -------------------------------------------------------------------

def lock_free():
    try:
        with store.data_lock(wait=0):
            return True
    except store.LockTimeout:
        return False


def fm_parsed(i):
    return {"id": f"m{i}", "d": "2026-10-01", "home": f"Home{i}", "away": f"Away{i}", "hid": str(10 * i), "aid": str(10 * i + 1),
            "s": {"goals": [1, 0], "corners": [3, 4]}}


def test_track_downloads_run_without_the_data_lock():
    with sim() as (clk, net):
        saved = (fmmatch.candidates, fmmatch._read_page, fmmatch.save, store.data_lock, os.environ.get(fmmatch.OWN_LOCK_ENV))
        todo = [("soccer_epl", {"id": f"m{i}", "url": f"https://www.fotmob.com/matches/x/{i}", "d": "2026-10-01"}) for i in (1, 2, 3)]
        fmmatch.candidates = lambda d, today=None: list(todo)
        free_during_download, held_during_write = [], []
        concurrent = {"done": False}

        def read_page(url, budget=None):
            free_during_download.append(lock_free())                          # le bot Telegram peut prendre le verrou pendant ce téléchargement
            if not concurrent["done"]:                                        # et il écrit dans le même document (commande /joue... ou autre)
                concurrent["done"] = True
                d = fmmatch.load()
                d["teams"]["999"] = {"nm": "Écrit pendant le téléchargement", "sk": ["x"], "d": "2099-01-01", "m": [[261001, 1, 0, 0]]}
                store.save(fmmatch.FILE, d)
            return fm_parsed(int(url.rsplit("/", 1)[1])), None

        def save(d):
            held_during_write.append(not lock_free())                        # la fusion et l'écriture se font verrou tenu
            return saved[2](d)
        fmmatch._read_page, fmmatch.save = read_page, save
        try:
            os.environ[fmmatch.OWN_LOCK_ENV] = "1"
            rep = fmmatch.fill(max_pages=10, seconds=600)
            assert rep["pages"] == 3 and rep["added"] == 3 and not rep["errors"], rep
            assert free_during_download == [True, True, True], free_during_download
            assert held_during_write == [True], held_during_write
            assert "999" in fmmatch.load()["teams"] and len(fmmatch.load()["teams"]) == 7, "rien d'écrit entre-temps n'est perdu"
            # sans mode own-lock (kelly-track.service lancé sous flock) : fill ne prend JAMAIS le verrou lui-même (inter-blocage sinon)
            os.environ.pop(fmmatch.OWN_LOCK_ENV)
            store.data_lock = lambda *a, **k: (_ for _ in ()).throw(AssertionError("data_lock appelé sous flock"))
            fmmatch._read_page, fmmatch.save = (lambda url, budget=None: (fm_parsed(9), None)), saved[2]
            todo[:] = [("soccer_epl", {"id": "m9", "url": "https://www.fotmob.com/matches/x/9", "d": "2026-10-02"})]
            rep = fmmatch.fill(max_pages=10, seconds=600)
            assert rep["added"] == 1 and not rep["errors"], rep
        finally:
            fmmatch.candidates, fmmatch._read_page, fmmatch.save, store.data_lock = saved[:4]
            os.environ.pop(fmmatch.OWN_LOCK_ENV, None)


def test_bot_track_own_lock():
    """bot.py --track --own-lock : clôture et alertes sous le verrou ; les stats de match FotMob (téléchargements) hors verrou. Sans l'option :
    comme avant (le flock de l'unité systemd tient déjà le verrou), aucun verrou pris par le processus."""
    from kelly import app
    argv, saved = sys.argv[:], (app.run_track, app.fill_match_stats, store.data_lock)
    sys.argv = ["bot.py", "--sample"]                 # --sample : bot.py ne lit pas le fichier .env à l'import
    try:
        ns = runpy.run_path(os.path.join(ROOT, "bot.py"), run_name="kelly_bot_under_test")
    finally:
        sys.argv = argv
    seen = []
    app.run_track = lambda *a, **k: seen.append(("track", "own" if os.environ.get(fmmatch.OWN_LOCK_ENV) else "plain", not lock_free()))
    app.fill_match_stats = lambda *a, **k: seen.append(("fill", "own" if os.environ.get(fmmatch.OWN_LOCK_ENV) else "plain", not lock_free()))
    try:
        ns["main"](["--track", "--own-lock"])
        assert seen == [("track", "own", True), ("fill", "own", False)], seen        # run_track sous le verrou, fill_match_stats hors verrou
        os.environ.pop(fmmatch.OWN_LOCK_ENV, None)
        del seen[:]
        ns["main"](["--track"])
        assert seen == [("track", "plain", False), ("fill", "plain", False)], seen   # inchangé : pas de verrou pris par le processus
        # le verrou occupé plus de 10 minutes : passage abandonné (code 1), pas d'attente infinie
        store.data_lock = lambda *a, **k: (_ for _ in ()).throw(store.LockTimeout("occupé"))
        del seen[:]
        try:
            _, err = stderr_of(ns["main"], ["--track", "--own-lock"])
            raise AssertionError("SystemExit(1) attendu")
        except SystemExit as e:
            assert e.code == 1 and seen == []
    finally:
        app.run_track, app.fill_match_stats, store.data_lock = saved
        os.environ.pop(fmmatch.OWN_LOCK_ENV, None)


# --- 12. football-data : 3 saisons ; réglage : le dernier réglage mesuré n'est pas effacé --------------------------------------

def test_football_data_downloads_three_seasons():
    assert fd.default_seasons(date(2026, 10, 9)) == ["2627", "2526", "2425"]
    csv = "Div,Date,HomeTeam,AwayTeam,FTHG,FTAG\nE0,16/08/2025,Arsenal,Chelsea,2,1\n"
    with sim(datetime(2026, 10, 9, 4, tzinfo=UTC).timestamp()) as (clk, net):
        net.routes["https://football-data.co.uk/robots.txt"] = ROBOTS
        for s in ("2627", "2526", "2425"):
            net.routes[f"https://football-data.co.uk/mmz4281/{s}/E0.csv"] = Resp(200, csv.encode())
        fd.refresh(["soccer_epl"])
        got = sorted(u for u in net.log if u.endswith("E0.csv"))
        assert got == [f"https://football-data.co.uk/mmz4281/{s}/E0.csv" for s in ("2425", "2526", "2627")], got
        # strength.load_matches lit ces trois saisons dans le cache, sans réseau : tout y est
        n = len(net.log)
        assert len(strength.load_matches("E0")) == 3 and len(net.log) == n


def test_tune_keeps_last_values_when_history_is_short():
    wipe()
    stamp = "2020-01-01T00:00:00+00:00"
    gw = {"n1": 300, "no": 300, "ll1": [0.0] * 11, "llo": [0.0] * 11}
    old = {"built": stamp, "half_life": {"E0": 330, "SP1": 220}, "goal_weight": {"_global": 0.15, "E0": 0.2, "SP1": 0.1},
           "report": {"E0": {"n": 300, "chosen": 330, "tuned": stamp, "gw": dict(gw)}, "SP1": {"n": 300, "chosen": 220, "tuned": stamp, "gw": dict(gw)}}}
    store.save(tune.FILE, old)

    def short(div):
        d0 = date(2025, 9, 3)
        return [{"date": str(d0 + timedelta(days=i)), "home": "A", "away": "B", "hg": 1, "ag": 0, "ftr": "H"} for i in range(0, 401, 7)]
    res = tune.run(divs=["E0", "SP1", "D1"], loader=short, seconds=60)
    new = store.load(tune.FILE, {})
    assert new["half_life"] == {"E0": 330, "SP1": 220}, new["half_life"]          # avant : {} (tout effacé, retour aux valeurs par défaut)
    assert new["report"]["E0"]["gw"] == gw and new["report"]["E0"]["chosen"] == 330 and "trop court" in new["report"]["E0"]["last_skip"]
    assert new["report"]["E0"]["tuned"] == stamp, "date de la dernière vraie mesure gardée : la ligue est retentée au prochain passage"
    assert "dernier réglage conservé" in res["skipped"]["E0"] and res["done"] == []
    assert new["goal_weight"].get("_global") is not None                          # les poids viennent toujours des courbes conservées
    assert "D1" not in new["half_life"] and "trop court" in res["skipped"]["D1"]  # une ligue jamais réglée reste simplement ignorée
    assert strength.hl_for("E0") == 330
    # une ligue réglée avec succès remplace bien l'ancien réglage
    def long_hist(div):
        return []                                                                   # historique vide : même règle
    res = tune.run(divs=["E0"], loader=long_hist, seconds=60, force=True)
    assert store.load(tune.FILE, {})["half_life"]["E0"] == 330 and "aucun historique" in res["skipped"]["E0"]


# --- 13. cache des stats atomique, cache des sources élagué -----------------------------------------------------------------------

def test_stats_cache_atomic_and_sources_cache_pruned():
    with sim():
        stats._save_stats_cache({"1": {"corners": 5, "d": PIN, "teams": {}}})
        path = stats._stats_path()
        before = open(path).read()
        assert json.loads(before)["1"]["corners"] == 5
        real_dump = json.dump

        def crash(obj, f, **kw):
            f.write('{"1": {"corn')                                                # écriture interrompue en route
            raise OSError("disque plein (simulé)")
        json.dump = crash
        try:
            _, err = stderr_of(stats._save_stats_cache, {"1": {"corners": 9, "d": PIN}, "2": {"corners": 1, "d": PIN}})
        finally:
            json.dump = real_dump
        assert open(path).read() == before, "le cache existant n'est jamais tronqué"
        assert not [f for f in os.listdir(os.path.dirname(path)) if f.endswith(".tmp")], os.listdir(os.path.dirname(path))
        assert "non écrit" in err
        # .cache/sources : les entrées inutilisées depuis plus de N jours disparaissent, pas celles qu'on relit chaque matin
        cache = http.cache_dir()
        os.makedirs(cache, exist_ok=True)
        now = http.time.time()                                                     # l'horloge simulée de prune, pas l'heure réelle

        old_url, used_url, young_url = "https://x.test/old", "https://x.test/used", "https://x.test/young"
        for u in (old_url, used_url, young_url):
            http._write(http._key(u), {"url": u, "fetched_at": 0, "status": 200}, b"corps " + u.encode())
        for u, age in ((old_url, 100), (used_url, 100), (young_url, 5)):
            for ext in (".json", ".bin"):
                os.utime(os.path.join(cache, http._key(u) + ext), (now - age * 86400, now - age * 86400))
        orphan = os.path.join(cache, "orphelin.bin")
        stale_tmp = os.path.join(cache, "abc.json.tmp")
        for p in (orphan, stale_tmp):
            open(p, "w").write("x")
            os.utime(p, (now - 5 * 86400, now - 5 * 86400))
        r = http.fetch(used_url, "test", http.FOREVER, check_robots=False)        # relu aujourd'hui (cache servi) -> « utilisée »
        assert r.ok and r.from_cache
        assert http.prune(45) == 1
        assert not os.path.exists(os.path.join(cache, http._key(old_url) + ".json")) and not os.path.exists(os.path.join(cache, http._key(old_url) + ".bin"))
        assert os.path.exists(os.path.join(cache, http._key(used_url) + ".json")) and os.path.exists(os.path.join(cache, http._key(young_url) + ".bin"))
        assert not os.path.exists(orphan) and not os.path.exists(stale_tmp)
        # refresh_all élague tout seul (une fois par passage)
        for ext in (".json", ".bin"):
            os.utime(os.path.join(cache, http._key(young_url) + ext), (now - 90 * 86400, now - 90 * 86400))
        saved = sources.SOURCES
        sources.SOURCES = ()
        try:
            _, err = stderr_of(sources.refresh_all, ["soccer_epl"])
        finally:
            sources.SOURCES = saved
        assert not os.path.exists(os.path.join(cache, http._key(young_url) + ".json")) and "effacée" in err, err
        assert C.SOURCES_CACHE_KEEP_DAYS == 45


# --- 14. cote de clôture gratuite : même dé-margeage que la proba à la prise --------------------------------------------------------

def test_free_close_uses_the_same_devig_as_the_take_probability():
    for prices in ([1.8, 3.8, 4.6], [1.2, 6.5, 15.0], [2.1, 3.3, 3.6]):
        take = odds.shin_probs(prices)                                             # ce que build_picks met dans prob_book (consensus de books Shin)
        for k, name in enumerate(("Arsenal", "Draw", "Chelsea")):
            row = {"key": "r", "sport_key": "soccer_epl", "group": "soccer", "home": "Arsenal", "away": "Chelsea", "market": "h2h", "name": name,
                   "point": None, "commence": "2026-10-11T15:00:00Z", "prob": take[k], "prob_book": take[k], "prob_src": "pinnacle", "sent": False, "closed_at": None,
                   "clv": None, "result": None}

            class FD:
                def match(self, b):
                    return {"odds": {"1x2": {"close": {"ps": prices}}}}               # le marché n'a pas bougé entre la prise et la clôture
            assert freeclose.fill([row], datetime(2026, 10, 12, 8, tzinfo=UTC), FD(), {}) == 1
            assert abs(row["clv"]) < 1e-4, (prices, name, row["clv"])                 # CLV nul : plus de biais de méthode (proportionnel ≠ Shin)
    # BetExplorer aussi, et un marché invalide n'arrête rien
    assert freeclose.devig([2.0, 3.0, 4.0]) == odds.shin_probs([2.0, 3.0, 4.0])
    assert freeclose.devig([2.0, None, 4.0]) is None and freeclose.devig([1.0, 3.0, 4.0]) is None and freeclose.devig(None) is None


# --- 15. README et configuration d'accord -------------------------------------------------------------------------------------------

def test_readme_and_config_agree_on_durations():
    readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    for name, val in (("SOURCES_SECONDS", C.SOURCES_SECONDS), ("APISTATS_SECONDS", C.APISTATS_SECONDS)):
        nums = {int(n) for n in re.findall(r"(\d+) s[^.\n]{0,40}`" + name + "`", readme)} | \
               {int(n) for n in re.findall(r"`" + name + r"`[^.\n]{0,60}?(\d+) s", readme)}
        assert nums == {val}, (name, nums, val)
    assert "`STATS_MIN_GAP`" in readme and "`STATS_RATE_WAIT`" in readme and "--own-lock" in readme
    # valeurs par défaut réelles (hors l'environnement des tests, qui coupe la cadence pour leurs faux API-Football)
    env = {k: v for k, v in os.environ.items() if k not in ("STATS_MIN_GAP", "STATS_RATE_WAIT", "APISTATS_SECONDS", "SOURCES_SECONDS")}
    out = subprocess.run([sys.executable, "-c", "from kelly import config as C; print(C.STATS_MIN_GAP, C.STATS_RATE_WAIT, C.APISTATS_SECONDS, "
                          "C.SOURCES_SECONDS)"], cwd=ROOT, env=env, capture_output=True, text=True, check=True).stdout.split()
    assert out == ["6.2", "60.0", "360", "720"], out


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    try:
        for t in tests:
            t()
            print(f"ok  {t.__name__}")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{len(tests)} tests lot J (sources) : OK")


if __name__ == "__main__":
    main()
